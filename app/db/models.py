"""ORM models.

A ProcessingRun is one attempt to process an uploaded invoice. It holds the
extracted and normalized fields, one RunStage row per executed workflow stage, the
ValidationChecks that were evaluated, a timeline of RunEvents, and, if the workflow
completed, the business decision. Money columns use the Money type (Decimal stored
as integer minor units).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.database import Base
from app.db.types import Money
from app.normalization.values import po_reference_key
from app.schemas.enums import CheckResult, Decision, POStatus, RunStatus, StageStatus, WorkflowStage


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not store timezone offsets)."""
    return datetime.now(UTC).replace(tzinfo=None)


def new_run_id() -> str:
    return uuid.uuid4().hex


def _enum(enum_cls) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="vendor")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(64), unique=True)
    # Comparison key for matching invoice PO references ("PO-5001" -> "5001"). Not
    # unique: source systems can hold "PO-5001" and "PO5001", and matching must then
    # report the reference as ambiguous.
    po_key: Mapped[str | None] = mapped_column(String(64), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    total_amount: Mapped[Decimal] = mapped_column(Money())
    status: Mapped[POStatus] = mapped_column(_enum(POStatus))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    vendor: Mapped[Vendor] = relationship(back_populates="purchase_orders")

    @validates("po_number")
    def _set_po_key(self, _key: str, po_number: str) -> str:
        self.po_key = po_reference_key(po_number)
        return po_number


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        # The database-level duplicate guard: at most one APPROVED invoice per normalized
        # vendor identity + invoice number. If two requests approve the same invoice
        # concurrently, the second commit fails here instead of approving it twice.
        Index(
            "uq_approved_vendor_invoice",
            "vendor_key",
            "invoice_key",
            unique=True,
            sqlite_where=text("decision = 'APPROVED'"),
            postgresql_where=text("decision = 'APPROVED'"),
        ),
        Index("ix_runs_vendor_date_amount", "vendor_core", "invoice_date", "total_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, default=new_run_id)  # public, one per attempt

    # Lifecycle (processing) vs outcome (business decision) are kept apart: a FAILED run
    # has no decision; a COMPLETED run always has one.
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.RECEIVED)
    current_stage: Mapped[WorkflowStage | None] = mapped_column(_enum(WorkflowStage))
    decision: Mapped[Decision | None] = mapped_column(_enum(Decision))
    decision_reason: Mapped[str | None] = mapped_column(Text)

    # The uploaded file
    file_name: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    file_size: Mapped[int | None] = mapped_column(Integer)
    file_path: Mapped[str | None] = mapped_column(String(1024))

    # Normalized invoice fields
    invoice_number: Mapped[str | None] = mapped_column(String(64), index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    po_number: Mapped[str | None] = mapped_column(String(64), index=True)
    currency: Mapped[str | None] = mapped_column(String(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Money())
    tax_amount: Mapped[Decimal | None] = mapped_column(Money())
    total_amount: Mapped[Decimal | None] = mapped_column(Money())

    # Stage outputs
    raw_text: Mapped[str | None] = mapped_column(Text)
    extracted_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # ExtractionResult, minus raw_text
    normalized_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # NormalizationResult

    # Duplicate-detection keys (see app.duplicates)
    vendor_key: Mapped[str | None] = mapped_column(String(255))  # full vendor identity, incl. legal form
    vendor_core: Mapped[str | None] = mapped_column(String(255))  # vendor identity without legal form
    invoice_key: Mapped[str | None] = mapped_column(String(64), index=True)

    # Resolved references (set by PO matching)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))

    # Processing failure (status FAILED)
    failed_stage: Mapped[WorkflowStage | None] = mapped_column(_enum(WorkflowStage))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)

    vendor: Mapped[Vendor | None] = relationship()
    purchase_order: Mapped[PurchaseOrder | None] = relationship()
    stages: Mapped[list["RunStage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunStage.sequence"
    )
    checks: Mapped[list["ValidationCheck"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ValidationCheck.id"
    )
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.id"
    )


class RunStage(Base):
    """One executed workflow stage of a run, with its timing, outcome and any error."""

    __tablename__ = "run_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stage: Mapped[WorkflowStage] = mapped_column(_enum(WorkflowStage))
    status: Mapped[StageStatus] = mapped_column(_enum(StageStatus), default=StageStatus.STARTED)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # summary of what the stage produced
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ProcessingRun] = relationship(back_populates="stages")


class ValidationCheck(Base):
    """The stored outcome of one validation check for a run (see schemas.CheckOutcome)."""

    __tablename__ = "validation_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))  # e.g. "required_fields", "amount_within_tolerance"
    category: Mapped[str] = mapped_column(String(32))  # "fields" | "duplicates" | "purchase_order" | "amounts"
    status: Mapped[CheckResult] = mapped_column(_enum(CheckResult))
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    effect: Mapped[Decision] = mapped_column(_enum(Decision), default=Decision.APPROVED)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[ProcessingRun] = relationship(back_populates="checks")


class RunEvent(Base):
    """One entry in a run's audit timeline (stage started/finished, conflicts, decision)."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. "stage_completed"
    stage: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[ProcessingRun] = relationship(back_populates="events")
