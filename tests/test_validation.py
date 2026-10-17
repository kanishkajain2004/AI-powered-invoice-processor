"""Validation engine: individual checks and decision priority (no database)."""

import itertools
from datetime import date
from decimal import Decimal

import pytest

from app.config import get_settings
from app.db.models import PurchaseOrder, Vendor
from app.duplicates import duplicate_keys
from app.matching import evaluate_po_match
from app.schemas import (
    CheckOutcome,
    CheckResult,
    Decision,
    DuplicateBasis,
    DuplicateCheckResult,
    DuplicateMatch,
    LineItem,
    NormalizedInvoice,
    POStatus,
    ValidationReport,
)
from app.validation import REQUIRED_FIELDS, validate_invoice

D = Decimal
ACME = "Acme Office Supplies Pvt Ltd"
BLUEWAVE = "Bluewave Technologies Pvt Ltd"
MANDATORY = {"required_fields", "duplicate_invoice", "po_match", "po_status", "vendor_match", "currency_match",
             "amount_within_tolerance"}
CONDITIONAL = {"potential_duplicate", "totals_arithmetic", "line_items_total", "line_item_arithmetic"}


def invoice(**overrides) -> NormalizedInvoice:
    fields = dict(
        vendor_name=ACME,
        invoice_number="INV-1001",
        invoice_date=date(2026, 9, 15),
        po_number="PO-5001",
        currency="INR",
        subtotal=D("42372.88"),
        tax_amount=D("7627.12"),
        total_amount=D("50000.00"),
        line_items=[
            LineItem(description="Paper", quantity=D("100"), unit_price=D("250.00"), amount=D("25000.00")),
            LineItem(description="Pens", quantity=D("40"), unit_price=D("300.00"), amount=D("12000.00")),
            LineItem(description="Stapler", quantity=D("12"), unit_price=D("447.74"), amount=D("5372.88")),
        ],
    )
    return NormalizedInvoice(**(fields | overrides))


def total_only(total: str, **overrides) -> NormalizedInvoice:
    """An invoice with no subtotal, tax or lines, so only the PO tolerance check sees the amount."""
    return invoice(subtotal=None, tax_amount=None, line_items=[], total_amount=D(total), **overrides)


def po(po_number="PO-5001", vendor=ACME, status=POStatus.APPROVED, total="50000.00", currency="INR"):
    return PurchaseOrder(po_number=po_number, vendor=Vendor(name=vendor), currency=currency,
                         total_amount=D(total), status=status)


def no_duplicates(inv: NormalizedInvoice) -> DuplicateCheckResult:
    keys = duplicate_keys(inv)
    return DuplicateCheckResult(vendor_key=keys.vendor_key, invoice_key=keys.invoice_key)


def earlier_run(decision=Decision.APPROVED, bases=(DuplicateBasis.VENDOR_AND_INVOICE_NUMBER,), run_id="run-7"):
    return DuplicateMatch(run_id=run_id, decision=decision, file_name="earlier.pdf", vendor_name=ACME,
                          invoice_number="INV-1001", invoice_date=date(2026, 9, 15), total_amount=D("50000.00"),
                          bases=list(bases))


def run(inv=None, pos=None, duplicates=None, **kwargs):
    inv = inv or invoice()
    match = evaluate_po_match(inv, [po()] if pos is None else pos)
    return validate_invoice(inv, match, duplicates or no_duplicates(inv), **kwargs)


def statuses(report) -> dict[str, CheckResult]:
    return {c.name: c.status for c in report.checks}


# --- happy path and check structure ---------------------------------------------------------


def test_clean_invoice_is_approved():
    report = run()

    assert report.decision is Decision.APPROVED
    assert report.reason == "All mandatory checks passed."
    assert report.deciding_checks == []
    assert all(status is CheckResult.PASS for status in statuses(report).values())


def test_every_check_has_name_status_reason_and_evidence():
    report = run()

    assert {c.name for c in report.checks} == MANDATORY | CONDITIONAL
    for check in report.checks:
        assert check.name and check.reason and isinstance(check.status, CheckResult)
        assert isinstance(check.evidence, dict)
        assert check.mandatory is (check.name in MANDATORY)
    assert report.check("amount_within_tolerance").evidence["tolerance_pct"] == D("1.0")
    assert report.check("required_fields").evidence["missing"] == []


def test_report_is_json_serialisable():
    payload = run().model_dump(mode="json")
    tolerance = next(c for c in payload["checks"] if c["name"] == "amount_within_tolerance")
    assert tolerance["evidence"]["po_amount"] == "50000.00"
    assert tolerance["effect"] == "APPROVED"


def test_engine_is_deterministic():
    assert run().model_dump() == run().model_dump()


# --- missing fields ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_needs_review(field):
    report = run(invoice(**{field: None}))

    assert report.decision is Decision.NEEDS_REVIEW
    check = report.check("required_fields")
    assert check.status is CheckResult.FAIL
    assert check.evidence["missing"] == [field]
    assert field in check.reason
    assert "required_fields" in report.deciding_checks


def test_several_missing_fields_are_all_listed():
    report = run(invoice(vendor_name=None, invoice_date=None, currency=None))
    assert report.check("required_fields").evidence["missing"] == ["vendor_name", "invoice_date", "currency"]


def test_completely_empty_invoice_needs_review():
    report = run(NormalizedInvoice())

    assert report.decision is Decision.NEEDS_REVIEW
    assert report.check("po_match").status is CheckResult.FAIL
    assert report.check("duplicate_invoice").status is CheckResult.SKIPPED
    for name in ("po_status", "vendor_match", "currency_match", "amount_within_tolerance"):
        assert report.check(name).status is CheckResult.SKIPPED


def test_optional_fields_may_be_missing():
    report = run(total_only("50000.00"))

    assert report.decision is Decision.APPROVED
    for name in CONDITIONAL - {"potential_duplicate"}:
        assert report.check(name).status is CheckResult.SKIPPED
        assert report.check(name).effect is Decision.APPROVED


# --- amount tolerance (PO 50,000.00; 1% = 500.00) ---------------------------------------------


def test_default_tolerance_is_one_percent():
    assert get_settings().amount_tolerance_pct == D("1.0")


@pytest.mark.parametrize("total", ["50000.00", "50500.00", "49500.00", "50250.50", "49999.99"])
def test_amount_within_tolerance(total):
    report = run(total_only(total))

    assert report.check("amount_within_tolerance").status is CheckResult.PASS
    assert report.decision is Decision.APPROVED


@pytest.mark.parametrize("total", ["50500.01", "49499.99", "60000.00", "25000.00", "0.00"])
def test_amount_outside_tolerance_needs_review(total):
    report = run(total_only(total))

    check = report.check("amount_within_tolerance")
    assert check.status is CheckResult.FAIL
    assert report.decision is Decision.NEEDS_REVIEW
    assert check.evidence["invoice_total"] == D(total)
    assert check.evidence["po_amount"] == D("50000.00")
    assert check.evidence["difference"] == D(total) - D("50000.00")
    assert check.evidence["tolerance_amount"] == D("500.00")


def test_tolerance_scales_with_po_amount():
    inv = total_only("121200.00", po_number="PO-5002", vendor_name=BLUEWAVE)
    report = run(inv, pos=[po("PO-5002", BLUEWAVE, total="120000.00")])

    assert report.check("amount_within_tolerance").status is CheckResult.PASS  # 1% of 120,000 = 1,200
    assert run(total_only("121200.01", po_number="PO-5002", vendor_name=BLUEWAVE),
               pos=[po("PO-5002", BLUEWAVE, total="120000.00")]).decision is Decision.NEEDS_REVIEW


def test_tolerance_can_be_overridden():
    assert run(total_only("50000.01"), amount_tolerance_pct=D("0")).decision is Decision.NEEDS_REVIEW
    assert run(total_only("52500.00"), amount_tolerance_pct=D("5")).decision is Decision.APPROVED


def test_amount_not_compared_across_currencies():
    report = run(total_only("50000.00", currency="USD"))

    check = report.check("amount_within_tolerance")
    assert check.status is CheckResult.SKIPPED
    assert check.effect is Decision.NEEDS_REVIEW  # mandatory: approval needs the comparison


# --- arithmetic consistency -------------------------------------------------------------------


def test_subtotal_plus_tax_must_equal_total():
    report = run(invoice(tax_amount=D("7000.00")))

    check = report.check("totals_arithmetic")
    assert check.status is CheckResult.FAIL
    assert check.evidence["expected_total"] == D("49372.88")
    assert check.evidence["difference"] == D("627.12")
    assert report.decision is Decision.NEEDS_REVIEW


def test_rounding_difference_within_tolerance_passes():
    report = run(invoice(tax_amount=D("7627.13"), total_amount=D("50000.00")))
    assert report.check("totals_arithmetic").status is CheckResult.PASS


def test_line_items_must_add_up_to_subtotal():
    report = run(invoice(subtotal=D("43000.00"), tax_amount=D("7000.00"), total_amount=D("50000.00")))

    check = report.check("line_items_total")
    assert check.status is CheckResult.FAIL
    assert check.evidence["line_items_sum"] == D("42372.88")
    assert check.evidence["compared_to"] == "subtotal"
    assert report.decision is Decision.NEEDS_REVIEW


def test_line_items_compared_to_total_minus_tax_without_subtotal():
    report = run(invoice(subtotal=None))

    check = report.check("line_items_total")
    assert check.status is CheckResult.PASS
    assert check.evidence["compared_to"] == "total_amount - tax_amount"


def test_line_items_not_compared_without_enough_information():
    report = run(invoice(subtotal=None, tax_amount=None))

    assert report.check("line_items_total").status is CheckResult.SKIPPED
    assert report.check("totals_arithmetic").status is CheckResult.SKIPPED
    assert report.decision is Decision.APPROVED


def test_line_quantity_times_price_must_equal_amount():
    lines = invoice().line_items
    lines[1] = LineItem(description="Pens", quantity=D("40"), unit_price=D("300.00"), amount=D("13000.00"))
    report = run(invoice(line_items=lines, subtotal=D("43372.88"), total_amount=D("51000.00")),
                 pos=[po(total="51000.00")])

    check = report.check("line_item_arithmetic")
    assert check.status is CheckResult.FAIL
    assert [m["line"] for m in check.evidence["mismatches"]] == [2]
    assert check.evidence["mismatches"][0]["expected"] == D("12000.00")
    assert report.decision is Decision.NEEDS_REVIEW


# --- vendor --------------------------------------------------------------------------------------


def test_vendor_mismatch_needs_review():
    report = run(invoice(vendor_name=BLUEWAVE))

    check = report.check("vendor_match")
    assert check.status is CheckResult.FAIL
    assert check.evidence == {"invoice_vendor": BLUEWAVE, "po_vendor": ACME}
    assert report.decision is Decision.NEEDS_REVIEW
    assert report.deciding_checks == ["vendor_match"]


def test_vendor_name_variant_is_approved():
    assert run(invoice(vendor_name="ACME OFFICE SUPPLIES PRIVATE LIMITED")).decision is Decision.APPROVED


def test_vendor_without_legal_suffix_is_a_warning_only():
    report = run(invoice(vendor_name="Acme Office Supplies"))

    assert report.check("vendor_match").status is CheckResult.WARN
    assert report.decision is Decision.APPROVED


def test_missing_vendor_needs_review():
    report = run(invoice(vendor_name=None))

    assert report.decision is Decision.NEEDS_REVIEW
    assert report.check("vendor_match").status is CheckResult.FAIL
    assert report.check("duplicate_invoice").status is CheckResult.SKIPPED


# --- PO problems ----------------------------------------------------------------------------------


def test_closed_po_needs_review():
    report = run(pos=[po(status=POStatus.CLOSED)])

    check = report.check("po_status")
    assert check.status is CheckResult.FAIL
    assert check.evidence == {"po_number": "PO-5001", "po_status": "CLOSED"}
    assert "CLOSED" in check.reason
    assert report.decision is Decision.NEEDS_REVIEW
    assert report.deciding_checks == ["po_status"]


def test_currency_mismatch_needs_review():
    report = run(invoice(currency="USD"))

    assert report.check("currency_match").status is CheckResult.FAIL
    assert report.decision is Decision.NEEDS_REVIEW


@pytest.mark.parametrize(
    "inv, pos, reason_fragment",
    [
        (invoice(po_number="PO-9999"), [po()], "No purchase order"),
        (invoice(), [po("PO-5001"), po("PO5001", BLUEWAVE)], "none was selected"),
        (invoice(po_number=None), [po()], "no PO reference"),
    ],
    ids=["not_found", "ambiguous", "no_reference"],
)
def test_unresolved_po_needs_review(inv, pos, reason_fragment):
    report = run(inv, pos=pos)

    assert report.decision is Decision.NEEDS_REVIEW
    assert reason_fragment in report.check("po_match").reason
    for name in ("po_status", "vendor_match", "currency_match", "amount_within_tolerance"):
        assert report.check(name).status is CheckResult.SKIPPED
        assert report.check(name).effect is Decision.NEEDS_REVIEW


def test_ambiguous_po_lists_candidates_as_evidence():
    report = run(pos=[po("PO-5001"), po("PO5001", BLUEWAVE)])
    assert report.check("po_match").evidence["candidates"] == ["PO-5001", "PO5001"]


# --- duplicates -------------------------------------------------------------------------------------


def test_confirmed_duplicate_is_rejected():
    inv = invoice()
    dups = no_duplicates(inv)
    dups.confirmed.append(earlier_run())

    report = run(inv, duplicates=dups)

    check = report.check("duplicate_invoice")
    assert check.status is CheckResult.FAIL
    assert check.failure_decision is Decision.REJECTED
    assert check.evidence["matches"][0]["run_id"] == "run-7"
    assert report.decision is Decision.REJECTED
    assert report.deciding_checks == ["duplicate_invoice"]


def test_same_file_as_approved_invoice_is_rejected():
    inv = invoice()
    dups = no_duplicates(inv)
    dups.confirmed.append(earlier_run(bases=[DuplicateBasis.SAME_FILE]))

    assert run(inv, duplicates=dups).decision is Decision.REJECTED


def test_duplicate_of_invoice_awaiting_review_needs_review_not_rejection():
    inv = invoice()
    dups = no_duplicates(inv)
    dups.pending.append(earlier_run(decision=Decision.NEEDS_REVIEW))

    report = run(inv, duplicates=dups)

    assert report.decision is Decision.NEEDS_REVIEW
    assert report.check("duplicate_invoice").status is CheckResult.FAIL
    assert "awaiting review" in report.check("duplicate_invoice").reason


def test_potential_duplicate_is_flagged_for_review_not_rejected():
    inv = invoice(invoice_number="INV-1001-B")
    dups = no_duplicates(inv)
    dups.potential.append(earlier_run(bases=[DuplicateBasis.VENDOR_DATE_AMOUNT]))

    report = run(inv, duplicates=dups)

    assert report.decision is Decision.NEEDS_REVIEW
    assert report.check("potential_duplicate").status is CheckResult.FAIL
    assert report.check("duplicate_invoice").status is CheckResult.PASS
    assert report.deciding_checks == ["potential_duplicate"]


# --- decision priority --------------------------------------------------------------------------------


def test_passing_checks_never_override_a_confirmed_duplicate():
    inv = invoice()
    dups = no_duplicates(inv)
    dups.confirmed.append(earlier_run())

    report = run(inv, duplicates=dups)

    passed = [c for c in report.checks if c.status is CheckResult.PASS]
    assert len(passed) == len(report.checks) - 1
    assert report.decision is Decision.REJECTED


def test_duplicate_outranks_every_review_reason():
    inv = invoice(vendor_name=BLUEWAVE, currency="USD", total_amount=D("99999.00"))
    dups = no_duplicates(inv)
    dups.confirmed.append(earlier_run())

    report = run(inv, pos=[po(status=POStatus.CLOSED)], duplicates=dups)

    assert report.decision is Decision.REJECTED
    assert report.deciding_checks == ["duplicate_invoice"]
    # The other failures are still reported for the record.
    failed = {c.name for c in report.checks if c.status is CheckResult.FAIL}
    assert {"po_status", "vendor_match", "currency_match", "totals_arithmetic"} <= failed


def test_passing_checks_never_override_a_failed_mandatory_check():
    report = run(pos=[po(status=POStatus.CLOSED)])

    assert sum(c.status is CheckResult.PASS for c in report.checks) == len(report.checks) - 1
    assert report.decision is Decision.NEEDS_REVIEW


def test_all_review_reasons_are_listed():
    report = run(invoice(vendor_name=BLUEWAVE, currency="USD"), pos=[po(status=POStatus.CLOSED)])

    assert report.decision is Decision.NEEDS_REVIEW
    assert set(report.deciding_checks) == {"po_status", "vendor_match", "currency_match", "amount_within_tolerance"}
    for name in report.deciding_checks:
        assert name in report.reason


def _outcome(status, mandatory=True, failure_decision=Decision.NEEDS_REVIEW, name="c"):
    return CheckOutcome(name=name, category="test", status=status, reason="r", mandatory=mandatory,
                        failure_decision=failure_decision)


@pytest.mark.parametrize(
    "status, mandatory, failure_decision, effect",
    [
        (CheckResult.PASS, True, Decision.NEEDS_REVIEW, Decision.APPROVED),
        (CheckResult.WARN, True, Decision.NEEDS_REVIEW, Decision.APPROVED),
        (CheckResult.SKIPPED, False, Decision.NEEDS_REVIEW, Decision.APPROVED),
        (CheckResult.SKIPPED, True, Decision.NEEDS_REVIEW, Decision.NEEDS_REVIEW),
        (CheckResult.FAIL, False, Decision.NEEDS_REVIEW, Decision.NEEDS_REVIEW),
        (CheckResult.FAIL, True, Decision.NEEDS_REVIEW, Decision.NEEDS_REVIEW),
        (CheckResult.FAIL, True, Decision.REJECTED, Decision.REJECTED),
    ],
)
def test_check_effect(status, mandatory, failure_decision, effect):
    assert _outcome(status, mandatory, failure_decision).effect is effect


def test_decision_is_independent_of_check_order():
    checks = [
        _outcome(CheckResult.PASS, name="a"),
        _outcome(CheckResult.FAIL, name="b"),
        _outcome(CheckResult.FAIL, failure_decision=Decision.REJECTED, name="c"),
        _outcome(CheckResult.WARN, name="d"),
    ]
    for order in itertools.permutations(checks):
        assert ValidationReport.decide(list(order)).decision is Decision.REJECTED


def test_no_checks_is_never_approved():
    assert ValidationReport.decide([]).decision is Decision.NEEDS_REVIEW
