"""Deterministic validation engine: checks -> decision.

Every rule here is plain code over normalized values, PO match results and duplicate
results; no LLM is involved. Each check returns a CheckOutcome holding a name, status,
reason and evidence, and says whether it is mandatory and what a failure leads to.

Decision priority (ValidationReport.decide): REJECTED > NEEDS_REVIEW > APPROVED.
  * REJECTED      a confirmed duplicate of an approved invoice.
  * NEEDS_REVIEW  any other failed check, or a mandatory check that could not run.
  * APPROVED      only when nothing above applies, i.e. every mandatory check passed.

Mandatory:     required_fields, duplicate_invoice, po_match, po_status, vendor_match,
               currency_match, amount_within_tolerance
Conditional:   potential_duplicate, totals_arithmetic, line_items_total,
               line_item_arithmetic. These run only when the data they need is
               present; when they run and fail, the invoice goes to review.
"""

from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.config import get_settings
from app.schemas import (
    CheckOutcome,
    CheckResult,
    Decision,
    DuplicateCheckResult,
    MatchReasonCode,
    NormalizedInvoice,
    POMatchResult,
    ValidationReport,
)

PASS, WARN, FAIL, SKIPPED = CheckResult.PASS, CheckResult.WARN, CheckResult.FAIL, CheckResult.SKIPPED
Code = MatchReasonCode

REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "po_number", "currency", "total_amount")


def _money(amount: Decimal | None, currency: str | None = None) -> str:
    if amount is None:
        return "n/a"
    text = f"{amount:,.2f}"
    return f"{currency} {text}" if currency else text


def _check(name, category, status, reason, evidence=None, *, mandatory, failure_decision=Decision.NEEDS_REVIEW):
    return CheckOutcome(
        name=name,
        category=category,
        status=status,
        reason=reason,
        evidence=evidence or {},
        mandatory=mandatory,
        failure_decision=failure_decision,
    )


# --- fields ----------------------------------------------------------------------


def check_required_fields(invoice: NormalizedInvoice) -> CheckOutcome:
    values = {name: getattr(invoice, name) for name in REQUIRED_FIELDS}
    missing = [name for name, value in values.items() if value is None]
    evidence = {"required": list(REQUIRED_FIELDS), "missing": missing}
    if missing:
        return _check("required_fields", "fields", FAIL, f"Missing required fields: {', '.join(missing)}.", evidence, mandatory=True)
    return _check("required_fields", "fields", PASS, "All required fields are present.", evidence, mandatory=True)


# --- duplicates ----------------------------------------------------------------------


def _describe(matches) -> str:
    return ", ".join(f"run {m.run_id} ({m.decision.value}, {'/'.join(m.bases)})" for m in matches)


def check_duplicate_invoice(duplicates: DuplicateCheckResult) -> CheckOutcome:
    """Exact duplicates: same vendor + invoice number, or the identical file."""
    evidence: dict[str, Any] = {"vendor_key": duplicates.vendor_key, "invoice_key": duplicates.invoice_key}
    if duplicates.confirmed:
        evidence["matches"] = [m.model_dump(mode="json") for m in duplicates.confirmed]
        return _check(
            "duplicate_invoice", "duplicates", FAIL,
            f"Confirmed duplicate of an approved invoice: {_describe(duplicates.confirmed)}.",
            evidence, mandatory=True, failure_decision=Decision.REJECTED,
        )
    if duplicates.pending:
        evidence["matches"] = [m.model_dump(mode="json") for m in duplicates.pending]
        return _check(
            "duplicate_invoice", "duplicates", FAIL,
            f"The same invoice is already awaiting review: {_describe(duplicates.pending)}.",
            evidence, mandatory=True,
        )
    if not duplicates.has_primary_key:
        return _check(
            "duplicate_invoice", "duplicates", SKIPPED,
            "Cannot check for duplicates without both a vendor name and an invoice number.",
            evidence, mandatory=True,
        )
    return _check(
        "duplicate_invoice", "duplicates", PASS,
        "No earlier invoice has this vendor and invoice number.", evidence, mandatory=True,
    )


def check_potential_duplicates(duplicates: DuplicateCheckResult) -> CheckOutcome:
    """Similar invoices (same vendor, date and amount, or a vendor-name variant): review, never reject."""
    if duplicates.potential:
        return _check(
            "potential_duplicate", "duplicates", FAIL,
            f"Possible duplicate, flagged for human review: {_describe(duplicates.potential)}.",
            {"matches": [m.model_dump(mode="json") for m in duplicates.potential]},
            mandatory=False,
        )
    return _check("potential_duplicate", "duplicates", PASS, "No similar invoices found.", mandatory=False)


# --- purchase order ----------------------------------------------------------------------


def _reason(po_match: POMatchResult, *codes: MatchReasonCode):
    return next((r for r in po_match.reasons if r.code in codes), None)


def check_po_match(po_match: POMatchResult) -> CheckOutcome:
    evidence = {
        "match_status": po_match.status.value,
        "po_reference": po_match.po_reference,
        "candidates": [c.po_number for c in po_match.candidates],
    }
    if po_match.purchase_order is not None:
        return _check(
            "po_match", "purchase_order", PASS,
            f"Reference '{po_match.po_reference}' identifies PO {po_match.purchase_order.po_number}.",
            evidence, mandatory=True,
        )
    reason = po_match.reasons[0].message if po_match.reasons else f"PO match status {po_match.status.value}."
    return _check("po_match", "purchase_order", FAIL, reason, evidence, mandatory=True)


def _po_dependent(name: str, po_match: POMatchResult) -> CheckOutcome | None:
    if po_match.purchase_order is None:
        return _check(name, "purchase_order", SKIPPED, "No unique PO to check against.", mandatory=True)
    return None


def check_po_status(po_match: POMatchResult) -> CheckOutcome:
    if skipped := _po_dependent("po_status", po_match):
        return skipped
    po = po_match.purchase_order
    evidence = {"po_number": po.po_number, "po_status": po.status.value}
    if reason := _reason(po_match, Code.PO_CLOSED):
        return _check("po_status", "purchase_order", FAIL, reason.message, evidence, mandatory=True)
    return _check("po_status", "purchase_order", PASS, f"PO {po.po_number} is APPROVED.", evidence, mandatory=True)


def check_vendor_match(po_match: POMatchResult) -> CheckOutcome:
    if skipped := _po_dependent("vendor_match", po_match):
        return skipped
    reason = _reason(
        po_match, Code.VENDOR_MATCHED, Code.VENDOR_MATCHED_IGNORING_SUFFIX, Code.VENDOR_MISMATCH, Code.VENDOR_MISSING
    )
    status = {
        Code.VENDOR_MATCHED: PASS,
        Code.VENDOR_MATCHED_IGNORING_SUFFIX: WARN,
        Code.VENDOR_MISMATCH: FAIL,
        Code.VENDOR_MISSING: FAIL,
    }[reason.code]
    return _check("vendor_match", "purchase_order", status, reason.message, dict(reason.details), mandatory=True)


def check_currency_match(po_match: POMatchResult) -> CheckOutcome:
    if skipped := _po_dependent("currency_match", po_match):
        return skipped
    reason = _reason(po_match, Code.CURRENCY_MATCHED, Code.CURRENCY_MISMATCH, Code.CURRENCY_MISSING)
    status = PASS if reason.code is Code.CURRENCY_MATCHED else FAIL
    return _check("currency_match", "purchase_order", status, reason.message, dict(reason.details), mandatory=True)


# --- amounts -----------------------------------------------------------------------


def check_amount_tolerance(invoice: NormalizedInvoice, po_match: POMatchResult, tolerance_pct: Decimal) -> CheckOutcome:
    """|invoice total - PO amount| must be at most tolerance_pct % of the PO amount."""
    name = "amount_within_tolerance"
    po = po_match.purchase_order
    if po is None:
        return _check(name, "amounts", SKIPPED, "No unique PO to compare the amount against.", mandatory=True)
    if invoice.total_amount is None:
        return _check(name, "amounts", SKIPPED, "The invoice total is missing.", mandatory=True)
    if invoice.currency != po.currency:
        return _check(
            name, "amounts", SKIPPED,
            f"Amounts not compared: invoice currency {invoice.currency or 'unknown'} differs from PO currency {po.currency}.",
            {"invoice_currency": invoice.currency, "po_currency": po.currency}, mandatory=True,
        )

    # Floored to whole minor units: differences are whole minor units, so the comparison is
    # unchanged, the evidence reads cleanly, and the allowance is never above tolerance_pct.
    tolerance = (po.total_amount * tolerance_pct / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    difference = invoice.total_amount - po.total_amount
    evidence = {
        "invoice_total": invoice.total_amount,
        "po_amount": po.total_amount,
        "difference": difference,
        "tolerance_pct": tolerance_pct,
        "tolerance_amount": tolerance,
    }
    cur = po.currency
    if abs(difference) <= tolerance:
        return _check(
            name, "amounts", PASS,
            f"Invoice total {_money(invoice.total_amount, cur)} is within {tolerance_pct}% "
            f"(±{_money(tolerance, cur)}) of PO amount {_money(po.total_amount, cur)}.",
            evidence, mandatory=True,
        )
    return _check(
        name, "amounts", FAIL,
        f"Invoice total {_money(invoice.total_amount, cur)} differs from PO amount {_money(po.total_amount, cur)} "
        f"by {_money(difference, cur)}, more than the {tolerance_pct}% tolerance (±{_money(tolerance, cur)}).",
        evidence, mandatory=True,
    )


def check_totals_arithmetic(invoice: NormalizedInvoice, tolerance: Decimal) -> CheckOutcome:
    """subtotal + tax = total, when all three are known."""
    name = "totals_arithmetic"
    parts = {"subtotal": invoice.subtotal, "tax_amount": invoice.tax_amount, "total_amount": invoice.total_amount}
    if missing := [k for k, v in parts.items() if v is None]:
        return _check(
            name, "amounts", SKIPPED, f"Not enough information (missing {', '.join(missing)}).", mandatory=False
        )
    expected = invoice.subtotal + invoice.tax_amount
    difference = invoice.total_amount - expected
    evidence = {**parts, "expected_total": expected, "difference": difference, "rounding_tolerance": tolerance}
    if abs(difference) <= tolerance:
        return _check(name, "amounts", PASS, "Subtotal + tax equals the total.", evidence, mandatory=False)
    return _check(
        name, "amounts", FAIL,
        f"Subtotal {_money(invoice.subtotal)} + tax {_money(invoice.tax_amount)} = {_money(expected)}, "
        f"but the total is {_money(invoice.total_amount)}.",
        evidence, mandatory=False,
    )


def check_line_items_total(invoice: NormalizedInvoice, tolerance: Decimal) -> CheckOutcome:
    """Line amounts add up to the subtotal (or, with no subtotal, to total - tax)."""
    name = "line_items_total"
    items = invoice.line_items
    if not items:
        return _check(name, "amounts", SKIPPED, "The invoice has no line items.", mandatory=False)
    if any(item.amount is None for item in items):
        return _check(name, "amounts", SKIPPED, "Some line items have no amount.", mandatory=False)

    lines_sum = sum((item.amount for item in items), Decimal("0.00"))
    if invoice.subtotal is not None:
        target_name, target = "subtotal", invoice.subtotal
    elif invoice.tax_amount is not None and invoice.total_amount is not None:
        target_name, target = "total_amount - tax_amount", invoice.total_amount - invoice.tax_amount
    else:
        return _check(
            name, "amounts", SKIPPED,
            "Not enough information: needs a subtotal, or both tax and total, to compare line items against.",
            {"line_items_sum": lines_sum}, mandatory=False,
        )
    difference = target - lines_sum
    evidence = {"line_count": len(items), "line_items_sum": lines_sum, "compared_to": target_name,
                "expected": target, "difference": difference, "rounding_tolerance": tolerance}
    if abs(difference) <= tolerance:
        return _check(name, "amounts", PASS, f"Line items add up to the {target_name}.", evidence, mandatory=False)
    return _check(
        name, "amounts", FAIL,
        f"Line items add up to {_money(lines_sum)}, but the {target_name} is {_money(target)}.",
        evidence, mandatory=False,
    )


def check_line_item_arithmetic(invoice: NormalizedInvoice, tolerance: Decimal) -> CheckOutcome:
    """quantity x unit price = amount, for every line where all three are known."""
    name = "line_item_arithmetic"
    complete = [
        (index, item) for index, item in enumerate(invoice.line_items, start=1)
        if item.quantity is not None and item.unit_price is not None and item.amount is not None
    ]
    if not complete:
        return _check(name, "amounts", SKIPPED, "No line item has quantity, unit price and amount.", mandatory=False)
    wrong = []
    for index, item in complete:
        expected = item.quantity * item.unit_price
        if abs(expected - item.amount) > tolerance:
            wrong.append({"line": index, "description": item.description, "quantity": item.quantity,
                          "unit_price": item.unit_price, "expected": expected, "amount": item.amount})
    evidence = {"lines_checked": len(complete), "mismatches": wrong, "rounding_tolerance": tolerance}
    if wrong:
        lines = ", ".join(str(w["line"]) for w in wrong)
        return _check(name, "amounts", FAIL, f"Quantity x unit price does not equal the amount on line(s) {lines}.",
                      evidence, mandatory=False)
    return _check(name, "amounts", PASS, f"Quantity x unit price equals the amount on all {len(complete)} line(s).",
                  evidence, mandatory=False)


# --- engine ------------------------------------------------------------------------
# Checks grouped by the workflow stage that produces them.


def required_field_checks(invoice: NormalizedInvoice) -> list[CheckOutcome]:
    return [check_required_fields(invoice)]


def duplicate_checks(duplicates: DuplicateCheckResult) -> list[CheckOutcome]:
    return [check_duplicate_invoice(duplicates), check_potential_duplicates(duplicates)]


def po_match_checks(po_match: POMatchResult) -> list[CheckOutcome]:
    return [check_po_match(po_match)]


def business_checks(
    invoice: NormalizedInvoice,
    po_match: POMatchResult,
    *,
    amount_tolerance_pct: Decimal | None = None,
    arithmetic_tolerance: Decimal | None = None,
) -> list[CheckOutcome]:
    settings = get_settings()
    tolerance_pct = settings.amount_tolerance_pct if amount_tolerance_pct is None else amount_tolerance_pct
    rounding = settings.arithmetic_tolerance if arithmetic_tolerance is None else arithmetic_tolerance
    return [
        check_po_status(po_match),
        check_vendor_match(po_match),
        check_currency_match(po_match),
        check_amount_tolerance(invoice, po_match, tolerance_pct),
        check_totals_arithmetic(invoice, rounding),
        check_line_items_total(invoice, rounding),
        check_line_item_arithmetic(invoice, rounding),
    ]


def validate_invoice(
    invoice: NormalizedInvoice,
    po_match: POMatchResult,
    duplicates: DuplicateCheckResult,
    *,
    amount_tolerance_pct: Decimal | None = None,
    arithmetic_tolerance: Decimal | None = None,
) -> ValidationReport:
    """Run every check and decide. The workflow runs the same groups stage by stage."""
    checks = (
        required_field_checks(invoice)
        + duplicate_checks(duplicates)
        + po_match_checks(po_match)
        + business_checks(
            invoice, po_match, amount_tolerance_pct=amount_tolerance_pct, arithmetic_tolerance=arithmetic_tolerance
        )
    )
    return ValidationReport.decide(checks)
