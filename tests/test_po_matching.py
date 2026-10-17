"""PO matching: the normal case and every failure scenario, against the seeded POs.

Seeded POs: PO-5001 Acme (APPROVED), PO-5002 Bluewave (APPROVED),
            PO-5003 Northstar (CLOSED), PO-5004 Acme (APPROVED); all INR.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import PurchaseOrder, Vendor
from app.db.seed import seed_purchase_orders
from app.matching import evaluate_po_match, match_purchase_order
from app.schemas import MatchReasonCode as Code
from app.schemas import NormalizedInvoice, POMatchStatus, POStatus, Severity

ACME = "Acme Office Supplies Pvt Ltd"
BLUEWAVE = "Bluewave Technologies Pvt Ltd"
NORTHSTAR = "Northstar Services Pvt Ltd"


@pytest.fixture
def db(db_session):
    seed_purchase_orders(db_session)
    return db_session


def invoice(po_number="PO-5001", vendor_name=ACME, currency="INR", **extra) -> NormalizedInvoice:
    return NormalizedInvoice(po_number=po_number, vendor_name=vendor_name, currency=currency, **extra)


def codes(result) -> list[Code]:
    return [r.code for r in result.reasons]


def error_codes(result) -> set[Code]:
    return {r.code for r in result.errors}


# --- normal matching -----------------------------------------------------------------


def test_exact_match(db):
    result = match_purchase_order(invoice(), db)

    assert result.status is POMatchStatus.MATCHED
    assert result.is_matched
    assert result.errors == []
    assert codes(result) == [Code.PO_FOUND, Code.PO_APPROVED, Code.VENDOR_MATCHED, Code.CURRENCY_MATCHED]
    po = result.purchase_order
    assert (po.po_number, po.vendor_name, po.currency, po.total_amount, po.status) == (
        "PO-5001", ACME, "INR", Decimal("50000.00"), POStatus.APPROVED,
    )
    assert po.id is not None and po.vendor_id is not None
    assert result.po_reference == "PO-5001"
    assert result.po_key == "5001"


@pytest.mark.parametrize(
    "reference", ["PO-5001", "po-5001", "PO 5001", "PO5001", "PO#5001", "P.O. No. 5001", "PO – 5001", "5001", " PO-5001. "]
)
def test_reference_variants_find_the_same_po(db, reference):
    result = match_purchase_order(invoice(po_number=reference), db)

    assert result.status is POMatchStatus.MATCHED
    assert result.purchase_order.po_number == "PO-5001"


@pytest.mark.parametrize(
    "vendor",
    ["ACME OFFICE SUPPLIES PRIVATE LIMITED", "M/s Acme Office Supplies Pvt. Ltd.", "Acme  Office  Supplies (P) Ltd"],
)
def test_vendor_name_variants_match(db, vendor):
    result = match_purchase_order(invoice(vendor_name=vendor), db)

    assert result.status is POMatchStatus.MATCHED
    assert Code.VENDOR_MATCHED in codes(result)


def test_vendor_without_legal_suffix_matches_with_warning(db):
    result = match_purchase_order(invoice(vendor_name="Acme Office Supplies"), db)

    assert result.status is POMatchStatus.MATCHED
    warning = next(r for r in result.reasons if r.code is Code.VENDOR_MATCHED_IGNORING_SUFFIX)
    assert warning.severity is Severity.WARNING


def test_same_vendor_other_po(db):
    result = match_purchase_order(invoice(po_number="PO-5004"), db)

    assert result.status is POMatchStatus.MATCHED
    assert result.purchase_order.po_number == "PO-5004"


def test_result_serialises_to_json(db):
    payload = match_purchase_order(invoice(), db).model_dump(mode="json")

    assert payload["status"] == "MATCHED"
    assert payload["is_matched"] is True
    assert payload["purchase_order"]["total_amount"] == "50000.00"
    assert payload["reasons"][0]["code"] == "PO_FOUND"


# --- missing reference / missing PO ------------------------------------------------------


@pytest.mark.parametrize("reference", [None, "", "   "])
def test_no_po_reference(db, reference):
    result = match_purchase_order(invoice(po_number=reference), db)

    assert result.status is POMatchStatus.NO_PO_REFERENCE
    assert result.purchase_order is None
    assert codes(result) == [Code.MISSING_PO_REFERENCE]
    assert result.reasons[0].message == "The invoice has no PO reference."


def test_po_reference_without_number(db):
    result = match_purchase_order(invoice(po_number="PO"), db)

    assert result.status is POMatchStatus.NO_PO_REFERENCE
    assert "contains no PO number" in result.reasons[0].message


@pytest.mark.parametrize("reference", ["PO-9999", "PO-50011", "PO-500", "PO-05001", "INV-5001"])
def test_po_not_found(db, reference):
    result = match_purchase_order(invoice(po_number=reference), db)

    assert result.status is POMatchStatus.NOT_FOUND
    assert result.purchase_order is None
    assert result.candidates == []
    assert codes(result) == [Code.PO_NOT_FOUND]
    assert reference.strip() in result.reasons[0].message


# --- ambiguous ---------------------------------------------------------------------------


def _add_po(db, po_number, vendor_name=BLUEWAVE, status=POStatus.APPROVED):
    vendor = db.scalar(select(Vendor).where(Vendor.name == vendor_name)) or Vendor(name=vendor_name)
    db.add(PurchaseOrder(po_number=po_number, vendor=vendor, currency="INR", total_amount=Decimal("1.00"), status=status))
    db.commit()


def test_ambiguous_reference_selects_no_po(db):
    _add_po(db, "PO5001")  # a different PO whose number normalizes to the same key as PO-5001

    result = match_purchase_order(invoice(po_number="PO 5001"), db)

    assert result.status is POMatchStatus.AMBIGUOUS
    assert not result.is_matched
    assert result.purchase_order is None
    assert [c.po_number for c in result.candidates] == ["PO-5001", "PO5001"]
    assert codes(result) == [Code.AMBIGUOUS_PO]
    assert result.reasons[0].details["candidates"] == ["PO-5001", "PO5001"]
    assert "none was selected" in result.reasons[0].message


def test_ambiguous_even_when_the_exact_spelling_exists(db):
    _add_po(db, "PO/5001")

    result = match_purchase_order(invoice(po_number="PO-5001"), db)

    assert result.status is POMatchStatus.AMBIGUOUS
    assert result.purchase_order is None


def test_ambiguous_even_when_only_one_candidate_fits_vendor(db):
    _add_po(db, "PO5001", vendor_name=BLUEWAVE, status=POStatus.CLOSED)

    result = match_purchase_order(invoice(po_number="PO-5001", vendor_name=ACME), db)

    assert result.status is POMatchStatus.AMBIGUOUS
    assert result.purchase_order is None


# --- closed PO ---------------------------------------------------------------------------


def test_closed_po(db):
    result = match_purchase_order(invoice(po_number="PO-5003", vendor_name=NORTHSTAR), db)

    assert result.status is POMatchStatus.CHECKS_FAILED
    assert result.purchase_order.po_number == "PO-5003"  # identified, but not billable
    assert error_codes(result) == {Code.PO_CLOSED}
    closed = next(r for r in result.reasons if r.code is Code.PO_CLOSED)
    assert closed.details == {"po_status": "CLOSED"}
    assert Code.VENDOR_MATCHED in codes(result) and Code.CURRENCY_MATCHED in codes(result)


# --- vendor mismatch -----------------------------------------------------------------------


def test_vendor_mismatch(db):
    result = match_purchase_order(invoice(vendor_name=BLUEWAVE), db)

    assert result.status is POMatchStatus.CHECKS_FAILED
    assert error_codes(result) == {Code.VENDOR_MISMATCH}
    reason = result.errors[0]
    assert reason.details == {"invoice_vendor": BLUEWAVE, "po_vendor": ACME}


def test_different_legal_form_is_a_mismatch(db):
    result = match_purchase_order(invoice(vendor_name="Acme Office Supplies LLP"), db)

    assert error_codes(result) == {Code.VENDOR_MISMATCH}


def test_similar_but_different_vendor_is_a_mismatch(db):
    result = match_purchase_order(invoice(vendor_name="Acme Office Supply Pvt Ltd"), db)

    assert error_codes(result) == {Code.VENDOR_MISMATCH}


@pytest.mark.parametrize("vendor", [None, "", "..."])
def test_missing_vendor(db, vendor):
    result = match_purchase_order(invoice(vendor_name=vendor), db)

    assert result.status is POMatchStatus.CHECKS_FAILED
    assert error_codes(result) == {Code.VENDOR_MISSING}


# --- currency mismatch ---------------------------------------------------------------------


def test_currency_mismatch(db):
    result = match_purchase_order(invoice(currency="USD"), db)

    assert result.status is POMatchStatus.CHECKS_FAILED
    assert error_codes(result) == {Code.CURRENCY_MISMATCH}
    assert result.errors[0].details == {"invoice_currency": "USD", "po_currency": "INR"}


def test_missing_currency(db):
    result = match_purchase_order(invoice(currency=None), db)

    assert result.status is POMatchStatus.CHECKS_FAILED
    assert error_codes(result) == {Code.CURRENCY_MISSING}


# --- several failures at once --------------------------------------------------------------


def test_all_failures_are_reported_together(db):
    result = match_purchase_order(invoice(po_number="PO-5003", vendor_name=ACME, currency="USD"), db)

    assert result.status is POMatchStatus.CHECKS_FAILED
    assert error_codes(result) == {Code.PO_CLOSED, Code.VENDOR_MISMATCH, Code.CURRENCY_MISMATCH}


# --- pure evaluation and the lookup key ---------------------------------------------------


def _transient_po(po_number, vendor_name=ACME, status=POStatus.APPROVED, currency="INR"):
    return PurchaseOrder(po_number=po_number, vendor=Vendor(name=vendor_name), currency=currency,
                         total_amount=Decimal("10.00"), status=status)


def test_evaluate_ignores_candidates_with_other_keys():
    candidates = [_transient_po("PO-5001"), _transient_po("PO-5002"), _transient_po("PO-50011")]

    result = evaluate_po_match(invoice(), candidates)

    assert result.status is POMatchStatus.MATCHED
    assert result.purchase_order.po_number == "PO-5001"


def test_evaluate_with_no_candidates():
    assert evaluate_po_match(invoice(), []).status is POMatchStatus.NOT_FOUND


def test_po_key_is_stored_and_kept_in_sync(db):
    po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-5002"))
    assert po.po_key == "5002"

    po.po_number = "PO-7002"
    db.commit()

    assert db.scalar(select(PurchaseOrder.po_key).where(PurchaseOrder.id == po.id)) == "7002"
    assert match_purchase_order(invoice(po_number="PO-7002", vendor_name=BLUEWAVE), db).is_matched
    assert match_purchase_order(invoice(po_number="PO-5002", vendor_name=BLUEWAVE), db).status is POMatchStatus.NOT_FOUND


# --- end to end with the sample PDFs ------------------------------------------------------


@pytest.mark.parametrize(
    "build, expected_status, expected_po",
    [
        ("acme_invoice", POMatchStatus.MATCHED, "PO-5001"),
        ("bluewave_invoice", POMatchStatus.MATCHED, "PO-5002"),
        ("northstar_invoice_missing_fields", POMatchStatus.NO_PO_REFERENCE, None),
    ],
)
def test_extract_normalize_match_samples(db, build, expected_status, expected_po):
    from app.extraction import extract_invoice
    from app.normalization import normalize_invoice
    from tests import factories

    extracted = extract_invoice(getattr(factories, build)())
    normalized = normalize_invoice(extracted.invoice)
    result = match_purchase_order(normalized.invoice, db)

    assert result.status is expected_status
    assert (result.purchase_order.po_number if result.purchase_order else None) == expected_po
