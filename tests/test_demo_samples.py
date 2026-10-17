"""The demo set in data/samples, run through the real API workflow in the documented order."""

import os
import shutil
import subprocess
import sys

import pymupdf
import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.seed import seed_purchase_orders
from app.extraction import extract_invoice
from scripts.demo import _check
from scripts.generate_samples import SAMPLES_DIR, load_manifest, write_samples
from tests.helpers import api_client

API = "/api/v1"
MANIFEST = load_manifest()
HAS_OCR = shutil.which("tesseract") is not None
ROOT = SAMPLES_DIR.parent.parent


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_dir", tmp_path / "uploads")


@pytest.fixture
def api(db_engine):
    with Session(db_engine) as session:
        seed_purchase_orders(session)
    with api_client(db_engine) as client:
        yield client


def post(api, file_name: str):
    path = SAMPLES_DIR / file_name
    return api.post(f"{API}/invoices/process", files={"file": (path.name, path.read_bytes(), "application/pdf")})


def by_file(name_prefix: str) -> dict:
    return next(s for s in MANIFEST if s["file"].startswith(name_prefix))


def test_manifest_matches_files_on_disk():
    assert [s["order"] for s in MANIFEST] == [1, 2, 3, 4, 5]
    for sample in MANIFEST:
        assert (SAMPLES_DIR / sample["file"]).stat().st_size > 1000
    assert sorted(p.name for p in SAMPLES_DIR.glob("*.pdf")) == sorted(s["file"] for s in MANIFEST)


def test_demo_in_documented_order(api):
    run_ids = {}
    for sample in MANIFEST:
        response = post(api, sample["file"])
        body = response.json()
        run_ids[sample["file"]] = (body.get("run") or body)["run_id"]
        label, problems = _check(sample, response.status_code, body, run_ids)
        assert problems == [], f"{sample['file']}: {problems}"
        assert label == ("expected" if HAS_OCR or "expected_without_ocr" not in sample else "expected_without_ocr")


def test_duplicate_references_the_already_approved_invoice(api):
    first = post(api, by_file("01")["file"]).json()
    duplicate = post(api, by_file("02")["file"]).json()

    assert first["decision"] == "APPROVED"
    assert duplicate["decision"] == "REJECTED"
    match = next(c for c in duplicate["checks"] if c["name"] == "duplicate_invoice")["evidence"]["matches"][0]
    assert match["run_id"] == first["run_id"]
    assert match["decision"] == "APPROVED"
    # A different file: caught by vendor + invoice number, not by identical bytes.
    assert "vendor+invoice_number" in match["bases"] and "same_file" not in match["bases"]


def test_order_matters_and_outcomes_do_not_depend_on_file_names(api):
    """The re-sent copy processed first is simply the first INV-1001 seen: it is approved,
    and the 'happy path' file becomes the duplicate."""
    copy_first = post(api, by_file("02")["file"]).json()
    original_second = post(api, by_file("01")["file"]).json()

    assert copy_first["decision"] == "APPROVED"
    assert original_second["decision"] == "REJECTED"


def test_amount_mismatch_evidence(api):
    body = post(api, by_file("03")["file"]).json()

    check = next(c for c in body["checks"] if c["name"] == "amount_within_tolerance")
    assert check["evidence"]["invoice_total"] == "55000.00"
    assert check["evidence"]["po_amount"] == "50000.00"
    assert check["evidence"]["difference"] == "5000.00"
    assert check["evidence"]["tolerance_amount"] == "500.00"
    # Everything else about the invoice is consistent, so the amount is the only reason.
    assert {c["name"] for c in body["checks"] if c["status"] == "FAIL"} == {"amount_within_tolerance"}


def test_missing_invoice_number_sample(api):
    body = post(api, by_file("04")["file"]).json()

    assert body["invoice"]["invoice_number"] is None
    assert body["invoice"]["total_amount"] == "120000.00"
    required = next(c for c in body["checks"] if c["name"] == "required_fields")
    assert required["evidence"]["missing"] == ["invoice_number"]


def test_scanned_sample_is_image_only():
    with pymupdf.open(SAMPLES_DIR / by_file("05")["file"]) as doc:
        page = doc[0]
        assert page.get_text().strip() == ""
        assert page.get_images()


@pytest.mark.skipif(not HAS_OCR, reason="Tesseract is not installed")
def test_scanned_sample_with_ocr(api):
    body = post(api, by_file("05")["file"]).json()
    assert (body["status"], body["decision"]) == ("COMPLETED", "APPROVED")


def test_regenerated_samples_extract_identically(tmp_path):
    write_samples(tmp_path)
    for sample in MANIFEST:
        if "expected_without_ocr" in sample:
            continue  # needs OCR
        committed = extract_invoice(SAMPLES_DIR / sample["file"]).invoice
        regenerated = extract_invoice(tmp_path / sample["file"]).invoice
        assert committed == regenerated, sample["file"]


# --- scripts/demo.py reset -------------------------------------------------------------------------


def _reset(tmp_path, db_name: str, upload_name: str = "demo_uploads"):
    env = os.environ | {"DATABASE_URL": f"sqlite:///{tmp_path / db_name}", "UPLOAD_DIR": str(tmp_path / upload_name)}
    return subprocess.run([sys.executable, "-m", "scripts.demo", "reset"], cwd=ROOT, env=env,
                          capture_output=True, text=True)


def test_reset_refuses_a_non_demo_database(tmp_path):
    (tmp_path / "invoices.db").write_bytes(b"precious")

    result = _reset(tmp_path, "invoices.db")

    assert result.returncode == 1
    assert "refusing to reset invoices.db" in result.stderr
    assert (tmp_path / "invoices.db").read_bytes() == b"precious"


def test_reset_refuses_a_non_demo_upload_dir(tmp_path):
    result = _reset(tmp_path, "demo.db", upload_name="uploads")
    assert result.returncode == 1 and "demo upload directory" in result.stderr


def test_reset_creates_a_seeded_demo_database(tmp_path):
    (tmp_path / "demo_uploads").mkdir()
    (tmp_path / "demo_uploads" / "old.pdf").write_bytes(b"x")

    result = _reset(tmp_path, "demo.db")

    assert result.returncode == 0, result.stderr
    assert "Seeded 4 purchase orders" in result.stdout
    assert (tmp_path / "demo.db").exists()
    assert list((tmp_path / "demo_uploads").iterdir()) == []


# --- edge-case set (data/samples/extra) ------------------------------------------------------------

from scripts.generate_samples import EXTRA, EXTRA_DIR  # noqa: E402

EXTRA_MANIFEST = load_manifest(EXTRA_DIR)


def test_extra_manifest_matches_files_on_disk():
    assert [s["file"] for s in EXTRA_MANIFEST] == [s["file"] for s in EXTRA]
    assert sorted(p.name for p in EXTRA_DIR.glob("*.pdf")) == sorted(s["file"] for s in EXTRA_MANIFEST)


@pytest.mark.parametrize("sample", EXTRA_MANIFEST, ids=lambda s: s["file"])
def test_each_edge_case_on_a_fresh_database(api, sample):
    path = EXTRA_DIR / sample["file"]
    response = api.post(f"{API}/invoices/process", files={"file": (path.name, path.read_bytes(), "application/pdf")})

    _, problems = _check(sample, response.status_code, response.json(), {})
    assert problems == []


def test_edge_cases_do_not_interfere_with_the_demo_set(api):
    for directory, manifest in ((EXTRA_DIR, EXTRA_MANIFEST), (SAMPLES_DIR, MANIFEST)):
        run_ids = {}
        for sample in manifest:
            path = directory / sample["file"]
            response = api.post(f"{API}/invoices/process",
                                files={"file": (path.name, path.read_bytes(), "application/pdf")})
            body = response.json()
            run_ids[sample["file"]] = (body.get("run") or body).get("run_id")
            _, problems = _check(sample, response.status_code, body, run_ids)
            assert problems == [], f"{sample['file']}: {problems}"


def test_regenerated_edge_cases_extract_identically(tmp_path):
    write_samples(tmp_path, EXTRA)
    for sample in EXTRA_MANIFEST:
        if sample["expected"].get("error_code"):
            continue  # not a PDF
        assert extract_invoice(EXTRA_DIR / sample["file"]).invoice == extract_invoice(tmp_path / sample["file"]).invoice
