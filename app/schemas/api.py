"""Response models for the public HTTP API (/api/v1).

These define exactly what leaves the server. Internal details (stored file paths,
exception types, raw messages of internal errors, raw document text) are deliberately
not part of them.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.enums import CheckResult, Decision, POStatus, RunStatus, StageStatus, WorkflowStage
from app.schemas.invoice import LineItem


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable code, e.g. invalid_pdf, run_not_found")
    message: str = Field(description="Human-readable explanation")
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    run: "RunSummary | None" = Field(default=None, description="Set when the error concerns a run that was created")


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    ocr: str = Field(description="'available' if scanned PDFs can be read, otherwise 'unavailable'")
    ocr_engine: str | None = Field(default=None, description="e.g. 'tesseract 5.5.3'")


class Links(BaseModel):
    self: str
    result: str


class RunError(BaseModel):
    stage: WorkflowStage | None
    code: str | None
    message: str | None


class RunSummary(BaseModel):
    run_id: str
    file_name: str
    status: RunStatus = Field(description="Processing lifecycle; COMPLETED or FAILED once finished")
    current_stage: WorkflowStage | None
    decision: Decision | None = Field(description="Business outcome; set only when status is COMPLETED")
    decision_reason: str | None
    error: RunError | None = Field(description="Set only when status is FAILED")
    vendor_name: str | None
    invoice_number: str | None
    total_amount: Decimal | None
    currency: str | None
    created_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    links: Links


class InvoiceFields(BaseModel):
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    po_number: str | None
    currency: str | None
    subtotal: Decimal | None
    tax_amount: Decimal | None
    total_amount: Decimal | None
    line_items: list[LineItem] = Field(default_factory=list)


class CheckView(BaseModel):
    name: str
    category: str
    status: CheckResult
    reason: str
    evidence: dict[str, Any] | None
    mandatory: bool
    effect: Decision


class MatchedPOView(BaseModel):
    po_number: str
    vendor_name: str
    currency: str
    total_amount: Decimal = Field(description="The PO's approved amount")
    status: POStatus


class RunResult(BaseModel):
    """The business result of a run: decision, reasons, extracted fields and checks."""

    run_id: str
    status: RunStatus
    decision: Decision | None
    decision_reason: str | None
    deciding_checks: list[str] = Field(description="Checks that determined a non-APPROVED decision")
    error: RunError | None
    invoice: InvoiceFields | None = Field(description="Normalized fields; null if processing failed before normalization")
    purchase_order: MatchedPOView | None
    checks: list[CheckView]
    links: Links


class ExtractionView(BaseModel):
    method: str | None
    page_count: int | None
    ocr_pages: list[int] = Field(default_factory=list)
    raw_fields: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class StageView(BaseModel):
    sequence: int
    stage: WorkflowStage
    status: StageStatus
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    output: dict[str, Any] | None
    error_code: str | None
    error_message: str | None


class EventView(BaseModel):
    event_type: str
    stage: str | None
    message: str | None
    payload: dict[str, Any] | None
    created_at: datetime


class RunDetail(RunSummary):
    file_size: int | None
    file_hash: str | None
    invoice: InvoiceFields | None
    purchase_order: MatchedPOView | None
    extraction: ExtractionView | None
    normalization_warnings: list[str] = Field(default_factory=list)
    deciding_checks: list[str] = Field(default_factory=list)
    stages: list[StageView]
    checks: list[CheckView]
    events: list[EventView]


class RunStats(BaseModel):
    """Aggregates over every stored run, computed in the database."""

    total_runs: int
    completed: int = Field(description="Runs that finished every stage (they all have a decision)")
    failed: int = Field(description="Processing failures; these have no decision")
    in_progress: int = Field(description="Runs still RECEIVED or PROCESSING")
    approved: int
    needs_review: int
    rejected: int
    average_duration_ms: float | None = Field(description="Mean end-to-end duration of finished runs")
    last_run_at: datetime | None


class RunPage(BaseModel):
    items: list[RunSummary]
    total: int
    page: int
    page_size: int
    has_next: bool
