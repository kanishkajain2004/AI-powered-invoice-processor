"""Regression tests on the externally supplied demo PDFs (tests/fixtures/external_demo).

These PDFs use a layout different from data/samples: a three-column header row with the
headings FROM | BILL TO | INVOICE DETAILS and the vendor name *below* the FROM heading.
The vendor used to be missed entirely, which sent even the happy path to NEEDS_REVIEW.
Expected results come from the business rules, not from the file names.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.seed import seed_purchase_orders
from app.extraction import extract_invoice
from tests.helpers import api_client

FIXTURES = Path(__file__).parent / "fixtures" / "external_demo"
API = "/api/v1"
D = Decimal


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_dir", tmp_path / "uploads")


@pytest.fixture
def api(db_engine):
    with Session(db_engine) as session:
        seed_purchase_orders(session)
    with api_client(db_engine) as client:
        yield client


def process(api, name: str) -> dict:
    path = FIXTURES / name
    response = api.post(f"{API}/invoices/process", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()


def failed(body: dict) -> set[str]:
    return {c["name"] for c in body["checks"] if c["status"] == "FAIL"}


# --- extraction ---------------------------------------------------------------------------------


def test_happy_path_extracts_every_field():
    invoice = extract_invoice(FIXTURES / "01_happy_path_INV-1001.pdf").invoice

    assert invoice.vendor_name == "Acme Office Supplies Pvt Ltd"
    assert invoice.invoice_number == "INV-1001"
    assert invoice.po_number == "PO-5001"
    assert invoice.currency == "INR"
    assert (invoice.subtotal, invoice.tax_amount, invoice.total_amount) == (D("45000.00"), D("5000.00"), D("50000.00"))


def test_vendor_is_not_taken_from_the_bill_to_column():
    vendors = {extract_invoice(p).invoice.vendor_name for p in FIXTURES.glob("0[1-3]*.pdf")}
    assert vendors == {"Acme Office Supplies Pvt Ltd"}


# --- the three dashboard runs, processed through the API -----------------------------------------


def test_happy_path_is_approved(api):
    body = process(api, "01_happy_path_INV-1001.pdf")

    assert (body["status"], body["decision"]) == ("COMPLETED", "APPROVED")
    assert body["invoice"]["vendor_name"] == "Acme Office Supplies Pvt Ltd"
    assert body["purchase_order"]["po_number"] == "PO-5001"
    assert failed(body) == set()


def test_amount_mismatch_needs_review_for_the_amount_only(api):
    body = process(api, "02_amount_mismatch_INV-1002.pdf")

    assert body["decision"] == "NEEDS_REVIEW"
    assert body["deciding_checks"] == ["amount_within_tolerance"]
    amount = next(c for c in body["checks"] if c["name"] == "amount_within_tolerance")
    assert (amount["evidence"]["invoice_total"], amount["evidence"]["po_amount"]) == ("55000.00", "50000.00")


def test_missing_invoice_number_needs_review(api):
    body = process(api, "03_missing_invoice_number.pdf")

    assert body["decision"] == "NEEDS_REVIEW"
    assert body["invoice"]["invoice_number"] is None
    assert body["invoice"]["vendor_name"] == "Acme Office Supplies Pvt Ltd"
    required = next(c for c in body["checks"] if c["name"] == "required_fields")
    assert required["evidence"]["missing"] == ["invoice_number"]
    assert set(body["deciding_checks"]) == {"required_fields", "duplicate_invoice"}


def test_dashboard_history_shows_the_persisted_vendor(api):
    for name in ("01_happy_path_INV-1001.pdf", "02_amount_mismatch_INV-1002.pdf", "03_missing_invoice_number.pdf"):
        process(api, name)

    items = api.get(f"{API}/runs").json()["items"]
    assert [r["vendor_name"] for r in items] == ["Acme Office Supplies Pvt Ltd"] * 3
    assert [r["decision"] for r in reversed(items)] == ["APPROVED", "NEEDS_REVIEW", "NEEDS_REVIEW"]


def test_currency_sentence_in_notes_does_not_hide_the_currency_label(api):
    """'Currency intentionally differs…' in the notes must not override 'Currency: USD'."""
    assert extract_invoice(FIXTURES / "08_currency_mismatch_INV-1008.pdf").invoice.currency == "USD"

    body = process(api, "08_currency_mismatch_INV-1008.pdf")
    currency = next(c for c in body["checks"] if c["name"] == "currency_match")
    assert body["decision"] == "NEEDS_REVIEW"
    assert currency["status"] == "FAIL"
    assert currency["evidence"] == {"invoice_currency": "USD", "po_currency": "INR"}
    assert "required_fields" not in body["deciding_checks"]
