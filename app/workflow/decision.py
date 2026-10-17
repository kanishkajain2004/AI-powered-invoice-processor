"""Persist a decision on its run, safely under concurrency.

Two requests carrying the same invoice can both check for duplicates before either has
committed, and both see none. The partial unique index uq_approved_vendor_invoice lets
only one of them commit an APPROVED decision. The other's write raises IntegrityError;
it then rolls back, runs the duplicate check again (now seeing the approved run),
rebuilds its report and records REJECTED. An invoice is therefore never approved
twice, whatever the timing.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ProcessingRun, RunEvent, ValidationCheck
from app.duplicates import duplicate_keys, find_duplicates
from app.schemas import NormalizedInvoice, POMatchResult, ValidationReport
from app.validation import duplicate_checks, validate_invoice

MAX_ATTEMPTS = 2


def _apply(run: ProcessingRun, invoice: NormalizedInvoice, po_match: POMatchResult, report: ValidationReport) -> None:
    keys = duplicate_keys(invoice)
    run.vendor_name = invoice.vendor_name
    run.invoice_number = invoice.invoice_number
    run.invoice_date = invoice.invoice_date
    run.po_number = invoice.po_number
    run.currency = invoice.currency
    run.subtotal = invoice.subtotal
    run.tax_amount = invoice.tax_amount
    run.total_amount = invoice.total_amount
    run.vendor_key, run.vendor_core, run.invoice_key = keys.vendor_key, keys.vendor_core, keys.invoice_key
    run.purchase_order_id = po_match.purchase_order.id if po_match.purchase_order else None
    run.decision = report.decision
    run.decision_reason = report.reason

    run.checks = [
        ValidationCheck(
            name=c.name,
            category=c.category,
            status=c.status,
            reason=c.reason,
            evidence=c.model_dump(mode="json")["evidence"],
            mandatory=c.mandatory,
            effect=c.effect,
        )
        for c in report.checks
    ]
    run.events.append(
        RunEvent(
            event_type="decision_recorded",
            stage="PERSISTENCE",
            message=report.reason,
            payload={"decision": report.decision.value, "deciding_checks": report.deciding_checks},
        )
    )


def _with_fresh_duplicate_checks(db: Session, run: ProcessingRun, invoice: NormalizedInvoice, report: ValidationReport):
    duplicates = find_duplicates(db, invoice, file_hash=run.file_hash, exclude_run_id=run.id)
    fresh = {c.name: c for c in duplicate_checks(duplicates)}
    return ValidationReport.decide([fresh.get(c.name, c) for c in report.checks])


def save_decision(
    db: Session, run: ProcessingRun, invoice: NormalizedInvoice, po_match: POMatchResult, report: ValidationReport
) -> tuple[ValidationReport, int]:
    """Store the report's checks and decision on `run` and commit.

    Returns the report actually stored (it differs from `report` if a concurrent approval
    of the same invoice was detected) and the number of such conflicts. `run` must
    already be committed, since it is re-read after a rollback.
    """
    if run.id is None:
        raise ValueError("save_decision needs a run that has already been committed")

    conflicts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # Applying can autoflush (e.g. when replacing the checks collection), so the
            # unique-index violation may surface here as well as at commit.
            _apply(run, invoice, po_match, report)
            db.commit()
            return report, conflicts
        except IntegrityError:
            db.rollback()
            if attempt == MAX_ATTEMPTS:
                raise
            conflicts += 1
            previous = report.decision
            report = _with_fresh_duplicate_checks(db, run, invoice, report)
            run.events.append(
                RunEvent(
                    event_type="approval_conflict",
                    stage="PERSISTENCE",
                    message=(
                        "Another run approved the same vendor and invoice number first; "
                        f"duplicate check re-run, decision {previous.value} -> {report.decision.value}."
                    ),
                    payload={"previous_decision": previous.value, "new_decision": report.decision.value},
                )
            )
    raise AssertionError("unreachable")


def record_decision(
    db: Session, run: ProcessingRun, invoice: NormalizedInvoice, po_match: POMatchResult
) -> ValidationReport:
    """Check for duplicates, validate, and save the decision on an already committed run."""
    if run.id is None:
        raise ValueError("record_decision needs a run that has already been committed")
    duplicates = find_duplicates(db, invoice, file_hash=run.file_hash, exclude_run_id=run.id)
    report, _ = save_decision(db, run, invoice, po_match, validate_invoice(invoice, po_match, duplicates))
    return report
