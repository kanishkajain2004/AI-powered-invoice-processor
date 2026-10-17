"""API integration tests: real app, TestClient, temporary SQLite database per test."""

import re

import pytesseract
import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import create_db_engine, init_db
from app.db.models import ProcessingRun
from app.db.seed import seed_purchase_orders
from app.workflow import orchestrator
from tests import factories
from tests.helpers import api_client

API = "/api/v1"
PROCESS = f"{API}/invoices/process"
STAGES = ["RECEIVED", "EXTRACTION", "NORMALIZATION", "REQUIRED_FIELD_VALIDATION", "DUPLICATE_CHECK", "PO_MATCHING",
          "BUSINESS_VALIDATION", "DECISION", "PERSISTENCE", "COMPLETED"]
SECRET = "password=hunter2 at /var/lib/secret/db.sqlite"


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    path = tmp_path / "uploads"
    monkeypatch.setattr(get_settings(), "upload_dir", path)
    return path


@pytest.fixture
def api(db_engine):
    with Session(db_engine) as session:
        seed_purchase_orders(session)
    with api_client(db_engine) as client:
        yield client


def upload(api, content: bytes, name="invoice.pdf", content_type="application/pdf"):
    return api.post(PROCESS, files={"file": (name, content, content_type)})


def error_of(response) -> dict:
    body = response.json()
    assert set(body["error"]) >= {"code", "message"}
    return body["error"]


def assert_nothing_internal_leaks(response, upload_dir):
    text = response.text
    assert str(upload_dir) not in text and "uploads/" not in text
    assert "Traceback" not in text and 'File "' not in text
    assert "hunter2" not in text and "/var/lib/secret" not in text
    assert "file_path" not in text and "raw_text" not in text


# --- health -------------------------------------------------------------------------------------


def test_health(api):
    response = api.get(f"{API}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "database": "ok"}


def test_health_reports_database_outage(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path}")  # a directory: SQLite cannot open it
    with api_client(engine) as client:
        response = client.get(f"{API}/health")
    assert response.status_code == 503
    assert error_of(response) == {"code": "database_unavailable", "message": "The database is not reachable."}


# --- POST /invoices/process: business outcomes --------------------------------------------------


def test_process_valid_invoice(api, upload_dir):
    response = upload(api, factories.acme_invoice(), "acme.pdf")

    assert response.status_code == 201
    body = response.json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["run_id"])
    assert response.headers["location"] == f"{API}/runs/{body['run_id']}"
    assert (body["status"], body["decision"]) == ("COMPLETED", "APPROVED")
    assert body["decision_reason"] == "All mandatory checks passed."
    assert body["deciding_checks"] == [] and body["error"] is None
    invoice = body["invoice"]
    assert invoice["vendor_name"] == "Acme Office Supplies Pvt Ltd"
    assert (invoice["invoice_number"], invoice["invoice_date"], invoice["po_number"]) == ("INV-1001", "2026-09-15", "PO-5001")
    assert (invoice["currency"], invoice["total_amount"], invoice["tax_amount"]) == ("INR", "50000.00", "7627.12")
    assert len(invoice["line_items"]) == 3
    assert body["purchase_order"] == {"po_number": "PO-5001", "vendor_name": "Acme Office Supplies Pvt Ltd",
                                      "currency": "INR", "total_amount": "50000.00", "status": "APPROVED"}
    assert len(body["checks"]) == 11
    tolerance = next(c for c in body["checks"] if c["name"] == "amount_within_tolerance")
    assert tolerance["status"] == "PASS" and tolerance["evidence"]["tolerance_pct"] == "1.0"
    assert body["links"] == {"self": f"{API}/runs/{body['run_id']}", "result": f"{API}/runs/{body['run_id']}/result"}
    assert_nothing_internal_leaks(response, upload_dir)


def test_process_duplicate_invoice(api):
    first = upload(api, factories.acme_invoice()).json()
    response = upload(api, factories.acme_invoice())

    assert response.status_code == 201  # a business rejection is a successful request
    body = response.json()
    assert (body["status"], body["decision"]) == ("COMPLETED", "REJECTED")
    assert body["deciding_checks"] == ["duplicate_invoice"]
    assert first["run_id"] in body["decision_reason"]


def test_process_amount_mismatch(api):
    body = upload(api, factories.custom_invoice(total="60,000.00")).json()

    assert body["decision"] == "NEEDS_REVIEW"
    assert body["deciding_checks"] == ["amount_within_tolerance"]
    check = next(c for c in body["checks"] if c["name"] == "amount_within_tolerance")
    assert check["evidence"]["difference"] == "10000.00"


def test_process_missing_fields(api):
    body = upload(api, factories.northstar_invoice_missing_fields()).json()

    assert body["decision"] == "NEEDS_REVIEW"
    assert "required_fields" in body["deciding_checks"]
    assert body["invoice"]["po_number"] is None and body["purchase_order"] is None


def test_octet_stream_upload_is_accepted(api):
    assert upload(api, factories.acme_invoice(), content_type="application/octet-stream").status_code == 201


# --- POST /invoices/process: rejected uploads (no run created) ------------------------------------


def _no_runs(api):
    return api.get(f"{API}/runs").json()["total"] == 0


def test_missing_file_field(api):
    response = api.post(PROCESS, data={"something": "else"})
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["body", "file"]


def test_empty_file(api):
    response = upload(api, b"")
    assert response.status_code == 400
    assert error_of(response)["code"] == "empty_file"
    assert _no_runs(api)


def test_file_too_large(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    response = upload(api, b"%PDF-1.7\n" + b"0" * (1024 * 1024))

    assert response.status_code == 413
    error = error_of(response)
    assert error["code"] == "file_too_large"
    assert error["details"] == {"max_bytes": 1024 * 1024}
    assert _no_runs(api)


def test_wrong_media_type(api):
    response = upload(api, factories.acme_invoice(), name="photo.png", content_type="image/png")
    assert response.status_code == 415
    assert error_of(response)["code"] == "unsupported_media_type"
    assert _no_runs(api)


@pytest.mark.parametrize(
    "content, code, fragment",
    [
        (b"hello, definitely not a pdf", "invalid_pdf", "not a PDF"),
        (b"%PDF-1.7\n" + bytes(range(256)) * 8, "invalid_pdf", "could not be opened"),
        (factories.encrypted(factories.acme_invoice()), "invalid_pdf", "password-protected"),
    ],
    ids=["not_pdf", "corrupt", "encrypted"],
)
def test_unreadable_pdf_is_rejected_before_processing(api, content, code, fragment):
    response = upload(api, content)

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == code and fragment in error["message"]
    assert "run" not in response.json()
    assert _no_runs(api)


def test_too_many_pages(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_pages", 1)
    response = upload(api, factories.blank_pdf(2))
    assert response.status_code == 422
    assert error_of(response)["code"] == "too_many_pages"


# --- safe file handling -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_name, shown_as",
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
        ("in<voi>ce|?*.pdf", "invoice.pdf"),
        ("...", "upload.pdf"),
    ],
)
def test_client_file_name_is_sanitized_and_never_used_for_storage(api, upload_dir, client_name, shown_as):
    body = upload(api, factories.acme_invoice(), name=client_name).json()

    run = api.get(f"{API}/runs/{body['run_id']}").json()
    assert run["file_name"] == shown_as
    assert [p.name for p in upload_dir.iterdir()] == [f"{body['run_id']}.pdf"]
    assert not any(upload_dir.parent.glob("**/passwd.pdf"))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("invoice\x00\n\t.pdf", "invoice.pdf"),
        ("C:\\Users\\me\\inv.pdf", "inv.pdf"),
        ("  .hidden.pdf. ", "hidden.pdf"),
        ("a" * 300 + ".pdf", "a" * 200),
        ("", "upload.pdf"),
        (None, "upload.pdf"),
        ("/", "upload.pdf"),
    ],
)
def test_safe_display_name(raw, expected):
    from app.api.uploads import safe_display_name

    assert safe_display_name(raw) == expected


# --- POST /invoices/process: processing failures (run created, FAILED) -------------------------


def test_ocr_unavailable_returns_503_with_run(api, monkeypatch):
    def missing():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    response = upload(api, factories.rasterize(factories.acme_invoice()))

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "ocr_unavailable"
    assert "brew install tesseract" in body["error"]["message"]
    assert body["error"]["details"] == {"failed_stage": "EXTRACTION"}
    run = body["run"]
    assert (run["status"], run["decision"]) == ("FAILED", None)
    assert run["error"]["stage"] == "EXTRACTION"

    result = api.get(f"{API}/runs/{run['run_id']}/result")
    assert result.status_code == 200
    assert (result.json()["status"], result.json()["decision"], result.json()["invoice"]) == ("FAILED", None, None)


def test_internal_error_is_masked(api, db_engine, monkeypatch, upload_dir):
    def broken(*args, **kwargs):
        raise RuntimeError(f"cannot connect: {SECRET}")

    monkeypatch.setattr(orchestrator, "match_purchase_order", broken)
    response = upload(api, factories.acme_invoice())

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "An internal error occurred while processing the invoice. It has been logged."
    assert body["run"]["status"] == "FAILED"
    assert_nothing_internal_leaks(response, upload_dir)

    detail = api.get(f"{API}/runs/{body['run']['run_id']}")
    assert detail.status_code == 200
    assert_nothing_internal_leaks(detail, upload_dir)
    failed_stage = next(s for s in detail.json()["stages"] if s["stage"] == "PO_MATCHING")
    assert failed_stage["status"] == "FAILED" and failed_stage["error_code"] == "internal_error"
    assert "error_type" not in failed_stage

    # The real message is kept server-side for operators.
    with Session(db_engine) as db:
        stored = db.query(ProcessingRun).filter_by(run_id=body["run"]["run_id"]).one()
        assert "hunter2" in stored.error_message


def test_unhandled_exception_returns_generic_500(db_engine, monkeypatch):
    from app.api.routes import invoices

    def explode(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(invoices, "process_invoice", explode)
    with api_client(db_engine, raise_server_exceptions=False) as client:
        response = upload(client, factories.acme_invoice())

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"
    assert "hunter2" not in response.text and "Traceback" not in response.text


# --- GET /runs ----------------------------------------------------------------------------------


@pytest.fixture
def history(api, monkeypatch):
    """Five runs: APPROVED, 2 x REJECTED, NEEDS_REVIEW, and one FAILED; created in that order."""
    ids = [upload(api, factories.acme_invoice()).json()["run_id"]]
    ids += [upload(api, factories.acme_invoice()).json()["run_id"] for _ in range(2)]
    ids.append(upload(api, factories.northstar_invoice_missing_fields()).json()["run_id"])

    def missing():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    ids.append(upload(api, factories.rasterize(factories.acme_invoice())).json()["run"]["run_id"])
    return ids


def test_list_runs_newest_first(api, history):
    body = api.get(f"{API}/runs").json()

    assert body["total"] == 5 and body["page"] == 1 and body["page_size"] == 20 and body["has_next"] is False
    assert [r["run_id"] for r in body["items"]] == list(reversed(history))
    first = body["items"][-1]
    assert (first["status"], first["decision"], first["invoice_number"]) == ("COMPLETED", "APPROVED", "INV-1001")


def test_list_runs_pagination(api, history):
    pages = [api.get(f"{API}/runs", params={"page": p, "page_size": 2}).json() for p in (1, 2, 3, 4)]

    assert [len(p["items"]) for p in pages] == [2, 2, 1, 0]
    assert [p["has_next"] for p in pages] == [True, True, False, False]
    assert all(p["total"] == 5 for p in pages)
    seen = [r["run_id"] for p in pages for r in p["items"]]
    assert seen == list(reversed(history))


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"decision": "APPROVED"}, 1),
        ({"decision": "REJECTED"}, 2),
        ({"decision": "NEEDS_REVIEW"}, 1),
        ({"status": "COMPLETED"}, 4),
        ({"status": "FAILED"}, 1),
        ({"status": "COMPLETED", "decision": "REJECTED"}, 2),
        ({"status": "FAILED", "decision": "APPROVED"}, 0),
    ],
)
def test_list_runs_filters(api, history, params, expected):
    body = api.get(f"{API}/runs", params=params).json()

    assert body["total"] == expected == len(body["items"])
    for item in body["items"]:
        for key, value in params.items():
            assert item[key] == value


@pytest.mark.parametrize(
    "params, loc",
    [
        ({"status": "DONE"}, ["query", "status"]),
        ({"decision": "MAYBE"}, ["query", "decision"]),
        ({"page": 0}, ["query", "page"]),
        ({"page_size": 101}, ["query", "page_size"]),
        ({"page": "two"}, ["query", "page"]),
    ],
)
def test_list_runs_invalid_parameters(api, params, loc):
    response = api.get(f"{API}/runs", params=params)

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == loc


# --- GET /runs/stats -------------------------------------------------------------------------------


def test_stats_on_empty_database(api):
    body = api.get(f"{API}/runs/stats").json()
    assert body == {"total_runs": 0, "completed": 0, "failed": 0, "in_progress": 0, "approved": 0,
                    "needs_review": 0, "rejected": 0, "average_duration_ms": None, "last_run_at": None}


def test_stats_reflect_stored_runs(api, history, db_engine):
    with Session(db_engine) as db:
        db.add(ProcessingRun(file_name="in-flight.pdf", status="PROCESSING"))
        db.commit()
        finished = [r.duration_ms for r in db.query(ProcessingRun) if r.status in ("COMPLETED", "FAILED")]

    body = api.get(f"{API}/runs/stats").json()

    assert (body["total_runs"], body["completed"], body["failed"], body["in_progress"]) == (6, 4, 1, 1)
    assert (body["approved"], body["needs_review"], body["rejected"]) == (1, 1, 2)
    assert body["average_duration_ms"] == pytest.approx(sum(finished) / len(finished))
    assert body["last_run_at"] is not None


# --- GET /runs/{run_id} and /result ------------------------------------------------------------------


def test_run_detail(api, upload_dir):
    run_id = upload(api, factories.acme_invoice()).json()["run_id"]
    response = api.get(f"{API}/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert [s["stage"] for s in body["stages"]] == STAGES
    assert all(s["status"] == "COMPLETED" and s["duration_ms"] >= 0 for s in body["stages"])
    assert len(body["checks"]) == 11
    assert body["extraction"]["method"] == "text"
    assert body["extraction"]["raw_fields"]["po_number"] == "PO Number: PO-5001"
    assert body["invoice"]["invoice_number"] == "INV-1001"
    assert body["purchase_order"]["po_number"] == "PO-5001"
    assert body["events"][-1]["event_type"] == "workflow_completed"
    assert re.fullmatch(r"[0-9a-f]{64}", body["file_hash"])
    assert_nothing_internal_leaks(response, upload_dir)


def test_run_result(api):
    run_id = upload(api, factories.custom_invoice(total="60,000.00")).json()["run_id"]
    body = api.get(f"{API}/runs/{run_id}/result").json()

    assert (body["status"], body["decision"]) == ("COMPLETED", "NEEDS_REVIEW")
    assert body["deciding_checks"] == ["amount_within_tolerance"]
    assert "amount_within_tolerance" in body["decision_reason"]
    assert body["invoice"]["total_amount"] == "60000.00"


@pytest.mark.parametrize("suffix", ["", "/result"])
def test_unknown_run_is_404(api, suffix):
    response = api.get(f"{API}/runs/{'0' * 32}{suffix}")
    assert response.status_code == 404
    assert error_of(response)["code"] == "run_not_found"


@pytest.mark.parametrize("suffix", ["", "/result"])
def test_malformed_run_id_is_422(api, suffix):
    response = api.get(f"{API}/runs/not-a-run-id{suffix}")
    assert response.status_code == 422
    assert error_of(response)["details"][0]["loc"] == ["path", "run_id"]


def test_result_of_unfinished_run_is_409(api, db_engine):
    with Session(db_engine) as db:
        run = ProcessingRun(file_name="in-flight.pdf", status="PROCESSING", current_stage="PO_MATCHING")
        db.add(run)
        db.commit()
        run_id = run.run_id

    response = api.get(f"{API}/runs/{run_id}/result")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "run_not_finished"
    assert body["run"]["current_stage"] == "PO_MATCHING"


def test_unknown_route_uses_error_envelope(api):
    response = api.get(f"{API}/nope")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


# --- persistence ---------------------------------------------------------------------------------------


def test_history_persists_across_restart(db_url, monkeypatch):
    engine = create_db_engine(db_url)
    init_db(engine)
    with Session(engine) as session:
        seed_purchase_orders(session)
    with api_client(engine) as client:
        approved = upload(client, factories.acme_invoice()).json()["run_id"]
        rejected = upload(client, factories.acme_invoice()).json()["run_id"]
    engine.dispose()

    # "Restart": a new engine and a new app client on the same database file.
    engine = create_db_engine(db_url)
    with api_client(engine) as client:
        body = client.get(f"{API}/runs").json()
        assert [r["run_id"] for r in body["items"]] == [rejected, approved]
        assert client.get(f"{API}/runs/{approved}/result").json()["decision"] == "APPROVED"
        detail = client.get(f"{API}/runs/{rejected}").json()
        assert detail["decision"] == "REJECTED" and len(detail["stages"]) == 10
        # Duplicate detection still sees the approved invoice after the restart.
        assert upload(client, factories.acme_invoice()).json()["decision"] == "REJECTED"
    engine.dispose()
