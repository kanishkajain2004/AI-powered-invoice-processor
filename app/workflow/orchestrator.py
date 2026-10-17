"""Workflow orchestrator: run one uploaded invoice through every stage, synchronously.

    RECEIVED -> EXTRACTION -> NORMALIZATION -> REQUIRED_FIELD_VALIDATION
    -> DUPLICATE_CHECK -> PO_MATCHING -> BUSINESS_VALIDATION -> DECISION
    -> PERSISTENCE -> COMPLETED | FAILED

Each call creates a new ProcessingRun with a unique public run_id. Every stage that
actually runs gets a RunStage row (status, start, end, duration, output summary or
error) and stage_started / stage_completed / stage_failed events. Rows are committed
as the stage starts and as it ends, so the stored history matches what really
happened, including partial progress before a crash. Stages that never ran have no row.

Two kinds of outcome are kept apart:
  * Processing failure: a stage raised (unreadable PDF, OCR unavailable, a bug). The run
    is FAILED, records the failed stage and error, and has no business decision.
  * Business decision: every stage ran. The run is COMPLETED, and `decision` is
    APPROVED, NEEDS_REVIEW (human review) or REJECTED (confirmed duplicate).
"""

import hashlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ProcessingRun, RunEvent, RunStage, utcnow
from app.duplicates import duplicate_keys, find_duplicates
from app.extraction import ExtractionError, extract_invoice
from app.matching import match_purchase_order
from app.normalization import normalize_invoice
from app.schemas import CheckOutcome, RunStatus, StageStatus, ValidationReport, WorkflowStage
from app.validation import business_checks, duplicate_checks, po_match_checks, required_field_checks
from app.workflow.decision import save_decision

logger = logging.getLogger(__name__)
Stage = WorkflowStage

INTERNAL_ERROR = "internal_error"


class StageFailed(Exception):
    def __init__(self, stage: WorkflowStage, error: BaseException, code: str):
        super().__init__(f"{stage.value} failed: {error}")
        self.stage, self.error, self.code = stage, error, code


@dataclass
class _StageOutput:
    """Filled in by a stage body; stored as the stage's output summary."""

    data: dict[str, Any] = field(default_factory=dict)


def _error_code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, ExtractionError) else INTERNAL_ERROR


def _check_summary(checks: list[CheckOutcome]) -> dict[str, str]:
    return {c.name: c.status.value for c in checks}


def _store_upload(run_id: str, content: bytes) -> Path:
    """Save the upload as <UPLOAD_DIR>/<run_id>.pdf (or .bin if it is not a PDF).

    The name comes from the server-generated run id and the content, never from the
    client's file name, so it cannot be used for path traversal or overwriting.
    """
    suffix = ".pdf" if b"%PDF-" in content[:1024] else ".bin"
    path = get_settings().upload_dir / f"{run_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class _Workflow:
    def __init__(self, db: Session, run: ProcessingRun):
        self.db = db
        self.run = run
        self.sequence = 0
        self.started = time.perf_counter()

    def _event(self, event_type: str, stage: WorkflowStage, message: str | None = None, payload: dict | None = None):
        self.run.events.append(RunEvent(event_type=event_type, stage=stage.value, message=message, payload=payload))

    def _new_stage(self, stage: WorkflowStage, status: StageStatus) -> RunStage:
        self.sequence += 1
        record = RunStage(sequence=self.sequence, stage=stage, status=status, started_at=utcnow())
        self.run.stages.append(record)
        self.run.current_stage = stage
        return record

    @contextmanager
    def stage(self, stage: WorkflowStage) -> Iterator[_StageOutput]:
        record = self._new_stage(stage, StageStatus.STARTED)
        self._event("stage_started", stage)
        self.db.commit()

        output = _StageOutput()
        start = time.perf_counter()
        try:
            yield output
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            self.db.rollback()  # discard the stage's partial work; its STARTED row is already committed
            code = _error_code(exc)
            if code == INTERNAL_ERROR:
                logger.exception("Run %s: stage %s raised", self.run.run_id, stage.value)
            record.status = StageStatus.FAILED
            record.completed_at = utcnow()
            record.duration_ms = elapsed
            record.error_code, record.error_type, record.error_message = code, type(exc).__name__, str(exc)
            self._event("stage_failed", stage, str(exc), {"error_code": code, "error_type": type(exc).__name__})
            self.db.commit()
            raise StageFailed(stage, exc, code) from exc

        record.status = StageStatus.COMPLETED
        record.completed_at = utcnow()
        record.duration_ms = (time.perf_counter() - start) * 1000
        record.output = to_jsonable_python(output.data)
        self._event("stage_completed", stage, payload={"duration_ms": round(record.duration_ms, 3)})
        self.db.commit()

    def _finish(self, stage: WorkflowStage, status: RunStatus) -> RunStage:
        record = self._new_stage(stage, StageStatus.COMPLETED if status is RunStatus.COMPLETED else StageStatus.FAILED)
        record.completed_at = record.started_at
        record.duration_ms = 0.0
        self.run.status = status
        self.run.completed_at = utcnow()
        self.run.duration_ms = (time.perf_counter() - self.started) * 1000
        return record

    def complete(self, report: ValidationReport) -> None:
        record = self._finish(Stage.COMPLETED, RunStatus.COMPLETED)
        record.output = {"decision": report.decision.value}
        self._event("workflow_completed", Stage.COMPLETED, report.reason, {"decision": report.decision.value})
        self.db.commit()

    def fail(self, failure: StageFailed) -> None:
        # Invariant: a FAILED run carries no business decision.
        self.run.decision = None
        self.run.decision_reason = None
        self.run.failed_stage = failure.stage
        self.run.error_code = failure.code
        self.run.error_message = str(failure.error)
        record = self._finish(Stage.FAILED, RunStatus.FAILED)
        record.error_code, record.error_type = failure.code, type(failure.error).__name__
        record.error_message = str(failure.error)
        self._event("workflow_failed", Stage.FAILED, str(failure.error),
                    {"failed_stage": failure.stage.value, "error_code": failure.code})
        self.db.commit()

    def execute(self, content: bytes) -> ValidationReport:
        run, db = self.run, self.db

        with self.stage(Stage.RECEIVED) as out:
            run.file_hash = hashlib.sha256(content).hexdigest()
            run.file_size = len(content)
            run.file_path = str(_store_upload(run.run_id, content))
            out.data = {"file_name": run.file_name, "file_size": run.file_size, "file_hash": run.file_hash}
        run.status = RunStatus.PROCESSING

        with self.stage(Stage.EXTRACTION) as out:
            extraction = extract_invoice(content)
            run.raw_text = extraction.raw_text
            run.extracted_data = extraction.model_dump(mode="json", exclude={"raw_text"})
            found = [k for k, v in extraction.invoice.model_dump(exclude={"line_items"}).items() if v is not None]
            out.data = {"method": extraction.method.value, "page_count": extraction.page_count,
                        "ocr_pages": extraction.ocr_pages, "fields_found": found,
                        "line_items": len(extraction.invoice.line_items), "warnings": extraction.warnings}

        with self.stage(Stage.NORMALIZATION) as out:
            normalized = normalize_invoice(extraction.invoice)
            invoice = normalized.invoice
            keys = duplicate_keys(invoice)
            for name in ("vendor_name", "invoice_number", "invoice_date", "po_number", "currency",
                         "subtotal", "tax_amount", "total_amount"):
                setattr(run, name, getattr(invoice, name))
            run.vendor_key, run.vendor_core, run.invoice_key = keys.vendor_key, keys.vendor_core, keys.invoice_key
            run.normalized_data = normalized.model_dump(mode="json")
            out.data = {"warnings": normalized.warnings, "vendor_key": keys.vendor_key, "invoice_key": keys.invoice_key}

        checks: list[CheckOutcome] = []

        with self.stage(Stage.REQUIRED_FIELD_VALIDATION) as out:
            stage_checks = required_field_checks(invoice)
            checks += stage_checks
            out.data = {"checks": _check_summary(stage_checks), "missing": stage_checks[0].evidence["missing"]}

        with self.stage(Stage.DUPLICATE_CHECK) as out:
            duplicates = find_duplicates(db, invoice, file_hash=run.file_hash, exclude_run_id=run.id)
            stage_checks = duplicate_checks(duplicates)
            checks += stage_checks
            out.data = {"checks": _check_summary(stage_checks),
                        "confirmed": [m.run_id for m in duplicates.confirmed],
                        "pending": [m.run_id for m in duplicates.pending],
                        "potential": [m.run_id for m in duplicates.potential]}

        with self.stage(Stage.PO_MATCHING) as out:
            po_match = match_purchase_order(invoice, db)
            stage_checks = po_match_checks(po_match)
            checks += stage_checks
            run.purchase_order_id = po_match.purchase_order.id if po_match.purchase_order else None
            out.data = {"checks": _check_summary(stage_checks), "match_status": po_match.status.value,
                        "po_number": po_match.purchase_order.po_number if po_match.purchase_order else None,
                        "candidates": [c.po_number for c in po_match.candidates]}

        with self.stage(Stage.BUSINESS_VALIDATION) as out:
            stage_checks = business_checks(invoice, po_match)
            checks += stage_checks
            out.data = {"checks": _check_summary(stage_checks)}

        with self.stage(Stage.DECISION) as out:
            report = ValidationReport.decide(checks)
            out.data = {"decision": report.decision.value, "deciding_checks": report.deciding_checks}

        with self.stage(Stage.PERSISTENCE) as out:
            report, conflicts = save_decision(db, run, invoice, po_match, report)
            out.data = {"decision": report.decision.value, "checks_stored": len(report.checks),
                        "approval_conflicts": conflicts}

        return report


def process_invoice(db: Session, *, file_name: str, content: bytes) -> ProcessingRun:
    """Process one uploaded document and return its ProcessingRun (COMPLETED or FAILED).

    Never raises for a document problem; that is recorded as a FAILED run. Raises only
    if the database itself is unusable.
    """
    run = ProcessingRun(file_name=file_name, status=RunStatus.RECEIVED)
    db.add(run)
    workflow = _Workflow(db, run)
    try:
        report = workflow.execute(content)
    except StageFailed as failure:
        workflow.fail(failure)
    else:
        workflow.complete(report)
    return run
