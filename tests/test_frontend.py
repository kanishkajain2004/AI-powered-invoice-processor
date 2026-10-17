"""Frontend integration tests: the real Streamlit app (AppTest) against a real backend.

A uvicorn process serves the API on a free port with its own temporary SQLite database.
The tests compare what the pages render with what the API returns, so no figure on
screen can be hardcoded or invented.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from tests import factories

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "frontend" / "streamlit_app.py")
TIMEOUT = 60

PAGE_HARNESS = """
import sys
sys.path.insert(0, {root!r})
from frontend import ui, {module}
from frontend.api_client import ApiClient
ui.inject_css()
{module}.render(ApiClient({url!r}))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_backend(work, **extra_env):
    """Seed a fresh database in `work` and serve the API on a free port; yield its URL."""
    env = os.environ | {"DATABASE_URL": f"sqlite:///{work / 'api.db'}", "UPLOAD_DIR": str(work / "uploads")} | extra_env
    subprocess.run([sys.executable, "-m", "app.db.seed"], cwd=ROOT, env=env, check=True, capture_output=True)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(f"{url}/api/v1/health", timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("backend did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    yield from _start_backend(tmp_path_factory.mktemp("frontend-backend"))


@pytest.fixture(scope="module")
def backend_without_ocr(tmp_path_factory):
    """A backend whose Tesseract cannot be found, whatever is installed on this machine."""
    yield from _start_backend(tmp_path_factory.mktemp("frontend-backend-no-ocr"),
                              TESSERACT_CMD="/nonexistent/tesseract")


def api(backend, path, **params):
    return requests.get(f"{backend}/api/v1{path}", params=params, timeout=10).json()


@pytest.fixture
def app(backend, monkeypatch):
    monkeypatch.setenv("API_BASE_URL", backend)

    def start() -> AppTest:
        return AppTest.from_file(APP, default_timeout=TIMEOUT).run()

    return start


def page(backend, module: str, **session) -> AppTest:
    at = AppTest.from_string(PAGE_HARNESS.format(root=str(ROOT), module=module, url=backend), default_timeout=TIMEOUT)
    for key, value in session.items():
        at.session_state[key] = value
    return at.run()


def html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def no_exceptions(at: AppTest) -> None:
    assert [e.value for e in at.exception] == []


_serial = iter(range(1, 28))


def unique_invoice(**fields) -> bytes:
    """An Acme invoice against PO-5001 with its own date and amount, so that tests in this
    module (which share one backend) never look like duplicates of each other by accident."""
    n = next(_serial)
    return factories.custom_invoice(**({"invoice_number": f"FE-{n:03d}", "invoice_date": f"{n:02d}/08/2026",
                                        "total": f"50,{n:03d}.00"} | fields))


def upload_and_process(at: AppTest, name: str, content: bytes) -> AppTest:
    at.file_uploader[0].set_value((name, content, "application/pdf")).run()
    button = next(b for b in at.button if b.label == "Process invoice")
    assert not button.disabled
    button.click().run()
    no_exceptions(at)
    return at


# --- Invoice processing -------------------------------------------------------------------------


def test_process_button_disabled_until_a_file_is_chosen(app):
    at = app()
    no_exceptions(at)
    assert at.title[0].value == "Invoice processing"
    assert next(b for b in at.button if b.label == "Process invoice").disabled


def test_process_valid_invoice_shows_decision_and_recorded_timeline(app, backend):
    at = upload_and_process(app(), "acme.pdf", unique_invoice())

    markup = html(at)
    assert "Selected file" in markup and "acme.pdf" in markup
    assert "Approved" in markup and "All mandatory checks passed." in markup
    run_id = api(backend, "/runs", page_size=1)["items"][0]["run_id"]
    assert run_id in markup  # the run id shown is the one the backend stored

    timeline = at.dataframe[-1].value
    assert list(timeline["Stage"]) == [
        "Received", "Extraction", "Normalization", "Required field validation", "Duplicate check",
        "PO matching", "Business validation", "Decision", "Persistence", "Completed"]
    assert any("not streamed live" in c.value for c in at.caption)


def test_process_duplicate_shows_rejection_and_reason(app):
    content = unique_invoice()
    upload_and_process(app(), "first.pdf", content)
    at = upload_and_process(app(), "again.pdf", content)

    markup = html(at)
    assert "Rejected" in markup and "Confirmed duplicate" in markup
    assert "Duplicate invoice" in markup  # the deciding check is listed


def test_process_amount_mismatch_needs_review(app):
    at = upload_and_process(app(), "over.pdf", unique_invoice(total="60,000.00"))

    markup = html(at)
    assert "Needs review" in markup and "Amount within tolerance" in markup


def test_rejected_upload_shows_error_and_creates_no_run(app, backend):
    before = api(backend, "/runs/stats")["total_runs"]
    at = upload_and_process(app(), "notes.pdf", b"these are meeting notes, not a PDF")

    assert "Upload rejected" in at.error[0].value and "not a PDF" in at.error[0].value
    assert any("invalid_pdf" in c.value for c in at.caption)
    assert api(backend, "/runs/stats")["total_runs"] == before


def test_processing_failure_is_shown_as_failure_not_decision(backend_without_ocr, monkeypatch):
    monkeypatch.setenv("API_BASE_URL", backend_without_ocr)
    at = upload_and_process(AppTest.from_file(APP, default_timeout=TIMEOUT).run(), "scan.pdf",
                            factories.rasterize(factories.acme_invoice()))

    markup = html(at)
    assert "Processing failed" in markup and "Extraction" in markup
    assert any("not a business decision" in c.value for c in at.caption)
    assert any(b.label == "Inspect the failed run →" for b in at.button)
    assert "brew install tesseract" in markup  # the actionable OCR message reaches the user


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_scanned_invoice_is_processed_with_ocr(app):
    at = upload_and_process(app(), "scan.pdf", factories.rasterize(unique_invoice()))

    markup = html(at)
    assert "Approved" in markup and "All mandatory checks passed." in markup


def test_result_links_to_run_details(app, backend):
    at = upload_and_process(app(), "nav.pdf", unique_invoice())
    next(b for b in at.button if b.label.startswith("Open full run details")).click().run()

    no_exceptions(at)
    assert at.title[0].value == "Run details"
    assert at.session_state["selected_run_id"] == api(backend, "/runs", page_size=1)["items"][0]["run_id"]


# --- Dashboard -------------------------------------------------------------------------------------


def test_dashboard_tiles_match_backend_stats(backend):
    stats = api(backend, "/runs/stats")
    at = page(backend, "page_dashboard")
    no_exceptions(at)

    markup = html(at)
    for label, key in [("Total runs", "total_runs"), ("Approved", "approved"), ("Needs review", "needs_review"),
                       ("Rejected", "rejected"), ("Processing failed", "failed")]:
        assert f'{label}</div><div class="kpi-value">{stats[key]:,}</div>' in markup, label
    assert stats["total_runs"] > 0


def test_dashboard_reflects_new_runs(app, backend):
    before = api(backend, "/runs/stats")["total_runs"]
    upload_and_process(app(), "new.pdf", unique_invoice())

    markup = html(page(backend, "page_dashboard"))
    assert f'Total runs</div><div class="kpi-value">{before + 1:,}</div>' in markup


def test_dashboard_history_table_is_the_backend_history(backend):
    expected = api(backend, "/runs", page_size=25)
    at = page(backend, "page_dashboard")

    table = at.dataframe[0].value
    assert len(table) == len(expected["items"])
    assert list(table["File"]) == [r["file_name"] for r in expected["items"]]
    assert any(f"of {expected['total']} run(s)" in c.value for c in at.caption)


@pytest.mark.parametrize("label, decision, shown", [("Rejected", "REJECTED", "✕ Rejected"),
                                                     ("Needs review", "NEEDS_REVIEW", "! Needs review")])
def test_dashboard_decision_filter(backend, label, decision, shown):
    at = page(backend, "page_dashboard")
    at.selectbox[1].select(label).run()

    table = at.dataframe[0].value
    assert len(table) == api(backend, "/runs", decision=decision)["total"] > 0
    assert set(table["Outcome"]) == {shown}


def test_dashboard_pagination(backend):
    total = api(backend, "/runs/stats")["total_runs"]
    at = page(backend, "page_dashboard")
    at.selectbox[2].select(10).run()

    assert len(at.dataframe[0].value) == min(10, total)


# --- Run details ------------------------------------------------------------------------------------


def test_run_details_for_a_historical_run(backend):
    history = api(backend, "/runs", page_size=100)["items"]
    oldest = history[-1]["run_id"]
    expected = api(backend, f"/runs/{oldest}")

    at = page(backend, "page_run", selected_run_id=oldest)
    no_exceptions(at)

    markup = html(at)
    assert oldest in markup and "Recorded timeline." in markup
    assert [t.label for t in at.tabs] == ["Execution timeline", "Invoice & PO", f"Validation ({len(expected['checks'])})",
                                          f"Audit events ({len(expected['events'])})"]
    timeline = at.dataframe[0].value
    assert list(timeline["#"]) == [s["sequence"] for s in expected["stages"]]
    assert list(timeline["Duration (ms)"]) == [round(s["duration_ms"], 3) for s in expected["stages"]]
    validation = next(d.value for d in at.dataframe if "Check" in d.value.columns)
    assert len(validation) == len(expected["checks"])
    assert "Matched purchase order" in markup or any("Matched purchase order" in m.value for m in at.markdown)


def test_run_details_picker_lists_history(backend):
    history = api(backend, "/runs", page_size=100)["items"]
    at = page(backend, "page_run")

    picker = at.selectbox[0]
    assert len(picker.options) == len(history)
    assert picker.value == history[0]["run_id"]  # newest first, selected by default
    assert any(history[0]["file_name"] in option for option in picker.options)


def test_run_details_unknown_id(backend):
    at = page(backend, "page_run")
    at.text_input[0].input("0" * 32).run()

    assert "Could not load run" in at.error[0].value and "No run with id" in at.error[0].value


# --- backend unavailable ------------------------------------------------------------------------


def test_backend_unavailable_is_reported(monkeypatch):
    dead = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv("API_BASE_URL", dead)
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()

    no_exceptions(at)
    assert "Backend unavailable" in html(at)
    dashboard = page(dead, "page_dashboard")
    assert "Could not load dashboard data" in dashboard.error[0].value
