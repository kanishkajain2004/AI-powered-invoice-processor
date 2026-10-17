"""Map ProcessingRun rows to API response models.

This is the only place that decides what a client sees of a run. It never includes
the stored file path or raw document text, and it replaces the messages of internal
errors (which may contain anything an exception carried) with a generic one.
"""

from app.db.models import ProcessingRun
from app.schemas import (
    CheckView,
    Decision,
    EventView,
    ExtractionView,
    InvoiceFields,
    LineItem,
    Links,
    MatchedPOView,
    RunDetail,
    RunError,
    RunResult,
    RunStatus,
    RunSummary,
    StageView,
)

API_PREFIX = "/api/v1"
INTERNAL_ERROR_CODES = {"internal_error"}
INTERNAL_ERROR_MESSAGE = "An internal error occurred while processing the invoice. It has been logged."


def public_message(code: str | None, message: str | None) -> str | None:
    return INTERNAL_ERROR_MESSAGE if code in INTERNAL_ERROR_CODES else message


def links(run: ProcessingRun) -> Links:
    base = f"{API_PREFIX}/runs/{run.run_id}"
    return Links(self=base, result=f"{base}/result")


def run_error(run: ProcessingRun) -> RunError | None:
    if run.status is not RunStatus.FAILED:
        return None
    return RunError(stage=run.failed_stage, code=run.error_code, message=public_message(run.error_code, run.error_message))


def invoice_fields(run: ProcessingRun) -> InvoiceFields | None:
    if run.normalized_data is None:  # failed before normalization
        return None
    line_items = run.normalized_data.get("invoice", {}).get("line_items", [])
    return InvoiceFields(
        vendor_name=run.vendor_name,
        invoice_number=run.invoice_number,
        invoice_date=run.invoice_date,
        po_number=run.po_number,
        currency=run.currency,
        subtotal=run.subtotal,
        tax_amount=run.tax_amount,
        total_amount=run.total_amount,
        line_items=[LineItem.model_validate(item) for item in line_items],
    )


def matched_po(run: ProcessingRun) -> MatchedPOView | None:
    po = run.purchase_order
    if po is None:
        return None
    return MatchedPOView(po_number=po.po_number, vendor_name=po.vendor.name, currency=po.currency,
                         total_amount=po.total_amount, status=po.status)


def checks(run: ProcessingRun) -> list[CheckView]:
    return [
        CheckView(name=c.name, category=c.category, status=c.status, reason=c.reason, evidence=c.evidence,
                  mandatory=c.mandatory, effect=c.effect)
        for c in run.checks
    ]


def deciding_checks(run: ProcessingRun) -> list[str]:
    if run.decision is None or run.decision is Decision.APPROVED:
        return []
    return [c.name for c in run.checks if c.effect is run.decision]


def summary(run: ProcessingRun) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        file_name=run.file_name,
        status=run.status,
        current_stage=run.current_stage,
        decision=run.decision,
        decision_reason=run.decision_reason,
        error=run_error(run),
        vendor_name=run.vendor_name,
        invoice_number=run.invoice_number,
        total_amount=run.total_amount,
        currency=run.currency,
        created_at=run.created_at,
        completed_at=run.completed_at,
        duration_ms=run.duration_ms,
        links=links(run),
    )


def result(run: ProcessingRun) -> RunResult:
    return RunResult(
        run_id=run.run_id,
        status=run.status,
        decision=run.decision,
        decision_reason=run.decision_reason,
        deciding_checks=deciding_checks(run),
        error=run_error(run),
        invoice=invoice_fields(run),
        purchase_order=matched_po(run),
        checks=checks(run),
        links=links(run),
    )


def _extraction(run: ProcessingRun) -> ExtractionView | None:
    data = run.extracted_data
    if data is None:
        return None
    return ExtractionView(
        method=data.get("method"),
        page_count=data.get("page_count"),
        ocr_pages=data.get("ocr_pages", []),
        raw_fields=data.get("raw_fields", {}),
        warnings=data.get("warnings", []),
    )


def _stage(stage) -> StageView:
    return StageView(
        sequence=stage.sequence, stage=stage.stage, status=stage.status, started_at=stage.started_at,
        completed_at=stage.completed_at, duration_ms=stage.duration_ms, output=stage.output,
        error_code=stage.error_code, error_message=public_message(stage.error_code, stage.error_message),
    )


def _event(event) -> EventView:
    code = (event.payload or {}).get("error_code")
    return EventView(event_type=event.event_type, stage=event.stage, message=public_message(code, event.message),
                     payload=event.payload, created_at=event.created_at)


def detail(run: ProcessingRun) -> RunDetail:
    return RunDetail(
        **summary(run).model_dump(),
        file_size=run.file_size,
        file_hash=run.file_hash,
        invoice=invoice_fields(run),
        purchase_order=matched_po(run),
        extraction=_extraction(run),
        normalization_warnings=(run.normalized_data or {}).get("warnings", []),
        deciding_checks=deciding_checks(run),
        stages=[_stage(s) for s in run.stages],
        checks=checks(run),
        events=[_event(e) for e in run.events],
    )
