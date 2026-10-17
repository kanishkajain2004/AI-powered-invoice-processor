"""PO matching: find the purchase order an invoice refers to and check it can be billed.

Steps:
1. Normalize the invoice's PO reference to a comparison key ("PO 5001" -> "5001").
2. Collect every PO with that key. None means NOT_FOUND. More than one means
   AMBIGUOUS, and no PO is chosen, even if only one of them looks plausible.
3. For a unique PO, run every check (PO status, vendor identity, currency) and report
   all results, so a reviewer sees each problem at once.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models import PurchaseOrder
from app.normalization.values import normalize_reference, po_reference_key, vendor_identity
from app.schemas import (
    MatchedPO,
    MatchReason,
    MatchReasonCode,
    NormalizedInvoice,
    POMatchResult,
    POMatchStatus,
    POStatus,
    Severity,
)

Code = MatchReasonCode


def _summary(po: PurchaseOrder) -> MatchedPO:
    return MatchedPO(
        id=po.id,
        po_number=po.po_number,
        vendor_id=po.vendor_id,
        vendor_name=po.vendor.name,
        currency=po.currency,
        total_amount=po.total_amount,
        status=po.status,
    )


def _status_check(po: PurchaseOrder) -> MatchReason:
    if po.status is POStatus.APPROVED:
        return MatchReason(code=Code.PO_APPROVED, severity=Severity.INFO, message=f"PO {po.po_number} is APPROVED.")
    return MatchReason(  # POStatus has only APPROVED and CLOSED
        code=Code.PO_CLOSED,
        severity=Severity.ERROR,
        message=f"PO {po.po_number} is CLOSED; no further invoices can be booked against it.",
        details={"po_status": po.status.value},
    )


def _vendor_check(invoice: NormalizedInvoice, po: PurchaseOrder) -> MatchReason:
    po_vendor = po.vendor.name
    details = {"invoice_vendor": invoice.vendor_name, "po_vendor": po_vendor}
    invoice_id, po_id = vendor_identity(invoice.vendor_name), vendor_identity(po_vendor)

    if invoice_id is None:
        return MatchReason(
            code=Code.VENDOR_MISSING,
            severity=Severity.ERROR,
            message=f"The invoice has no vendor name, so it cannot be checked against PO vendor '{po_vendor}'.",
            details=details,
        )
    if po_id is not None and invoice_id.key == po_id.key:
        return MatchReason(
            code=Code.VENDOR_MATCHED, severity=Severity.INFO, message=f"Vendor matches '{po_vendor}'.", details=details
        )
    if (
        po_id is not None
        and invoice_id.core == po_id.core
        and (invoice_id.legal_suffix is None or po_id.legal_suffix is None)
    ):
        return MatchReason(
            code=Code.VENDOR_MATCHED_IGNORING_SUFFIX,
            severity=Severity.WARNING,
            message=(
                f"Vendor '{invoice.vendor_name}' matches PO vendor '{po_vendor}' "
                "only when the legal-form suffix is ignored."
            ),
            details=details,
        )
    return MatchReason(
        code=Code.VENDOR_MISMATCH,
        severity=Severity.ERROR,
        message=f"Invoice vendor '{invoice.vendor_name}' does not match PO vendor '{po_vendor}'.",
        details=details,
    )


def _currency_check(invoice: NormalizedInvoice, po: PurchaseOrder) -> MatchReason:
    po_currency = po.currency.upper()
    details = {"invoice_currency": invoice.currency, "po_currency": po_currency}
    if invoice.currency is None:
        return MatchReason(
            code=Code.CURRENCY_MISSING,
            severity=Severity.ERROR,
            message=f"The invoice currency is unknown, so it cannot be checked against PO currency {po_currency}.",
            details=details,
        )
    if invoice.currency != po_currency:
        return MatchReason(
            code=Code.CURRENCY_MISMATCH,
            severity=Severity.ERROR,
            message=f"Invoice currency {invoice.currency} does not match PO currency {po_currency}.",
            details=details,
        )
    return MatchReason(
        code=Code.CURRENCY_MATCHED, severity=Severity.INFO, message=f"Currency {po_currency} matches.", details=details
    )


def evaluate_po_match(invoice: NormalizedInvoice, candidates: Sequence[PurchaseOrder]) -> POMatchResult:
    """Decide the match from candidate POs (with vendors loaded). Pure: no database access.

    Candidates whose PO number does not share the invoice's key are ignored, so the
    caller may pass a superset.
    """
    reference = normalize_reference(invoice.po_number)
    key = po_reference_key(invoice.po_number)
    base = {"po_reference": reference, "po_key": key}

    if key is None:
        message = (
            "The invoice has no PO reference."
            if reference is None
            else f"The invoice PO reference '{reference}' contains no PO number."
        )
        return POMatchResult(
            status=POMatchStatus.NO_PO_REFERENCE,
            reasons=[MatchReason(code=Code.MISSING_PO_REFERENCE, severity=Severity.ERROR, message=message)],
            **base,
        )

    matches = sorted((po for po in candidates if po_reference_key(po.po_number) == key), key=lambda po: po.po_number)
    summaries = [_summary(po) for po in matches]

    if not matches:
        return POMatchResult(
            status=POMatchStatus.NOT_FOUND,
            reasons=[
                MatchReason(
                    code=Code.PO_NOT_FOUND,
                    severity=Severity.ERROR,
                    message=f"No purchase order matches reference '{reference}'.",
                    details={"po_key": key},
                )
            ],
            **base,
        )

    if len(matches) > 1:
        numbers = [po.po_number for po in matches]
        return POMatchResult(
            status=POMatchStatus.AMBIGUOUS,
            candidates=summaries,
            reasons=[
                MatchReason(
                    code=Code.AMBIGUOUS_PO,
                    severity=Severity.ERROR,
                    message=(
                        f"{len(matches)} purchase orders match reference '{reference}' "
                        f"({', '.join(numbers)}); none was selected."
                    ),
                    details={"po_key": key, "candidates": numbers},
                )
            ],
            **base,
        )

    po = matches[0]
    found = MatchReason(
        code=Code.PO_FOUND,
        severity=Severity.INFO,
        message=f"Reference '{reference}' matches PO {po.po_number}.",
        details={"po_number": po.po_number},
    )
    reasons = [found, _status_check(po), _vendor_check(invoice, po), _currency_check(invoice, po)]
    failed = any(r.severity is Severity.ERROR for r in reasons)
    return POMatchResult(
        status=POMatchStatus.CHECKS_FAILED if failed else POMatchStatus.MATCHED,
        purchase_order=summaries[0],
        candidates=summaries,
        reasons=reasons,
        **base,
    )


def match_purchase_order(invoice: NormalizedInvoice, db: Session) -> POMatchResult:
    """Look up candidate POs by normalized reference and evaluate the match."""
    key = po_reference_key(invoice.po_number)
    candidates: list[PurchaseOrder] = []
    if key is not None:
        candidates = list(
            db.scalars(
                select(PurchaseOrder).options(joinedload(PurchaseOrder.vendor)).where(PurchaseOrder.po_key == key)
            )
        )
    return evaluate_po_match(invoice, candidates)
