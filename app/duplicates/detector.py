"""Duplicate detection against earlier processing runs.

Primary key: normalized vendor identity + invoice-number key. "Acme Office Supplies
Private Limited / inv-1001" and "ACME OFFICE SUPPLIES PVT. LTD. / INV 1001" are the
same invoice.

Only earlier runs whose decision is APPROVED or NEEDS_REVIEW are compared: rejected
runs and failed runs (which have no decision) never became invoices of record. The
database's partial unique index on (vendor_key, invoice_key) WHERE decision =
'APPROVED' is the final guard against two concurrent approvals; see
app.workflow.decision.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.orm import Session

from app.db.models import ProcessingRun
from app.normalization.values import reference_key, vendor_identity
from app.schemas import Decision, DuplicateBasis, DuplicateCheckResult, DuplicateMatch, NormalizedInvoice

COMPARABLE_DECISIONS = (Decision.APPROVED, Decision.NEEDS_REVIEW)


@dataclass(frozen=True)
class DuplicateKeys:
    vendor_key: str | None  # identity incl. canonical legal form: "acme office supplies pvt ltd"
    vendor_core: str | None  # identity without legal form: "acme office supplies"
    invoice_key: str | None  # reference key of the invoice number: "INV-1001"


def duplicate_keys(invoice: NormalizedInvoice) -> DuplicateKeys:
    identity = vendor_identity(invoice.vendor_name)
    return DuplicateKeys(
        vendor_key=identity.key if identity else None,
        vendor_core=identity.core if identity else None,
        invoice_key=reference_key(invoice.invoice_number),
    )


def _criteria(keys: DuplicateKeys, invoice: NormalizedInvoice, file_hash: str | None) -> Iterable[tuple[str, ColumnElement]]:
    Run = ProcessingRun
    if keys.vendor_key and keys.invoice_key:
        yield DuplicateBasis.VENDOR_AND_INVOICE_NUMBER, and_(Run.vendor_key == keys.vendor_key, Run.invoice_key == keys.invoice_key)
    if file_hash:
        yield DuplicateBasis.SAME_FILE, Run.file_hash == file_hash
    if keys.vendor_core and keys.invoice_key and keys.vendor_key:
        yield DuplicateBasis.INVOICE_NUMBER_VENDOR_VARIANT, and_(
            Run.vendor_core == keys.vendor_core, Run.invoice_key == keys.invoice_key, Run.vendor_key != keys.vendor_key
        )
    if keys.vendor_core and invoice.invoice_date is not None and invoice.total_amount is not None:
        yield DuplicateBasis.VENDOR_DATE_AMOUNT, and_(
            Run.vendor_core == keys.vendor_core,
            Run.invoice_date == invoice.invoice_date,
            Run.total_amount == invoice.total_amount,
        )


def find_duplicates(
    db: Session,
    invoice: NormalizedInvoice,
    *,
    file_hash: str | None = None,
    exclude_run_id: int | None = None,
) -> DuplicateCheckResult:
    """Classify earlier runs that this invoice may duplicate (see DuplicateCheckResult)."""
    keys = duplicate_keys(invoice)
    runs: dict[int, ProcessingRun] = {}
    bases: dict[int, list[str]] = {}

    for basis, criterion in _criteria(keys, invoice, file_hash):
        stmt = select(ProcessingRun).where(criterion, ProcessingRun.decision.in_(COMPARABLE_DECISIONS))
        if exclude_run_id is not None:
            stmt = stmt.where(ProcessingRun.id != exclude_run_id)
        for run in db.scalars(stmt):
            runs[run.id] = run
            bases.setdefault(run.id, []).append(basis)

    result = DuplicateCheckResult(vendor_key=keys.vendor_key, invoice_key=keys.invoice_key)
    for run_id in sorted(runs):
        run = runs[run_id]
        match = DuplicateMatch(
            run_id=run.run_id,
            decision=run.decision,
            file_name=run.file_name,
            vendor_name=run.vendor_name,
            invoice_number=run.invoice_number,
            invoice_date=run.invoice_date,
            total_amount=run.total_amount,
            bases=bases[run_id],
        )
        if match.is_exact and run.decision is Decision.APPROVED:
            result.confirmed.append(match)
        elif match.is_exact:
            result.pending.append(match)
        else:
            result.potential.append(match)
    return result
