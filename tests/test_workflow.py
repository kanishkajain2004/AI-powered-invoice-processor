"""End-to-end workflow tests: PDF bytes in, persisted run, stages, checks and decision out."""

import re
from decimal import Decimal
from pathlib import Path

import pytesseract
import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ProcessingRun
from app.schemas import CheckResult, Decision, RunStatus, StageStatus, WorkflowStage
from app.workflow import orchestrator, process_invoice
from tests import factories

S = WorkflowStage
ALL_STAGES = [S.RECEIVED, S.EXTRACTION, S.NORMALIZATION, S.REQUIRED_FIELD_VALIDATION, S.DUPLICATE_CHECK,
              S.PO_MATCHING, S.BUSINESS_VALIDATION, S.DECISION, S.PERSISTENCE, S.COMPLETED]
CHECK_NAMES = ["required_fields", "duplicate_invoice", "potential_duplicate", "po_match", "po_status", "vendor_match",
               "currency_match", "amount_within_tolerance", "totals_arithmetic", "line_items_total",
               "line_item_arithmetic"]


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    path = tmp_path / "uploads"
    monkeypatch.setattr(get_settings(), "upload_dir", path)
    return path


@pytest.fixture
def db(seeded_db):
    return seeded_db


def process(db, content: bytes, file_name="invoice.pdf") -> ProcessingRun:
    return process_invoice(db, file_name=file_name, content=content)


def stage_names(run) -> list[WorkflowStage]:
    return [s.stage for s in run.stages]


def stage(run, name: WorkflowStage):
    return next(s for s in run.stages if s.stage is name)


def check(run, name: str):
    return next(c for c in run.checks if c.name == name)


def assert_completed_cleanly(run):
    assert run.status is RunStatus.COMPLETED
    assert run.current_stage is S.COMPLETED
    assert stage_names(run) == ALL_STAGES
    assert [s.sequence for s in run.stages] == list(range(1, len(ALL_STAGES) + 1))
    assert all(s.status is StageStatus.COMPLETED for s in run.stages)
    assert run.failed_stage is None and run.error_code is None
    assert [c.name for c in run.checks] == CHECK_NAMES


# --- valid invoice ---------------------------------------------------------------------------


def test_valid_invoice_is_approved(db, upload_dir):
    content = factories.acme_invoice()
    run = process(db, content, "acme.pdf")

    assert_completed_cleanly(run)
    assert run.decision is Decision.APPROVED
    assert run.decision_reason == "All mandatory checks passed."
    assert re.fullmatch(r"[0-9a-f]{32}", run.run_id)

    # Extracted and normalized data are persisted.
    assert run.extracted_data["invoice"]["invoice_number"] == "INV-1001"
    assert run.extracted_data["method"] == "text"
    assert "raw_text" not in run.extracted_data and "INV-1001" in run.raw_text
    assert run.normalized_data["invoice"]["total_amount"] == "50000.00"
    assert (run.vendor_name, run.invoice_number, run.po_number, run.total_amount) == (
        "Acme Office Supplies Pvt Ltd", "INV-1001", "PO-5001", Decimal("50000.00"))
    assert run.purchase_order.po_number == "PO-5001"

    # The upload is stored under the run id, not the client's file name.
    assert Path(run.file_path) == upload_dir / f"{run.run_id}.pdf"
    assert Path(run.file_path).read_bytes() == content
    assert run.file_size == len(content)

    assert all(c.status is CheckResult.PASS for c in run.checks)


def test_stage_timings_are_real_and_ordered(db):
    run = process(db, factories.acme_invoice())

    previous_end = None
    for s in run.stages:
        assert s.started_at is not None and s.completed_at is not None
        assert s.completed_at >= s.started_at
        assert s.duration_ms is not None and s.duration_ms >= 0
        if previous_end is not None:
            assert s.started_at >= previous_end
        previous_end = s.completed_at
    assert run.completed_at >= run.created_at
    assert run.duration_ms >= sum(s.duration_ms for s in run.stages[:-1]) * 0.99


def test_stage_outputs_reflect_what_each_stage_produced(db):
    run = process(db, factories.acme_invoice())

    assert stage(run, S.RECEIVED).output["file_hash"] == run.file_hash
    assert stage(run, S.EXTRACTION).output["line_items"] == 3
    assert "po_number" in stage(run, S.EXTRACTION).output["fields_found"]
    assert stage(run, S.NORMALIZATION).output["invoice_key"] == "INV-1001"
    assert stage(run, S.REQUIRED_FIELD_VALIDATION).output == {"checks": {"required_fields": "PASS"}, "missing": []}
    assert stage(run, S.DUPLICATE_CHECK).output["confirmed"] == []
    assert stage(run, S.PO_MATCHING).output["match_status"] == "MATCHED"
    assert stage(run, S.BUSINESS_VALIDATION).output["checks"]["amount_within_tolerance"] == "PASS"
    assert stage(run, S.DECISION).output == {"decision": "APPROVED", "deciding_checks": []}
    assert stage(run, S.PERSISTENCE).output == {"decision": "APPROVED", "checks_stored": 11, "approval_conflicts": 0}
    assert stage(run, S.COMPLETED).output == {"decision": "APPROVED"}


def test_events_record_each_stage_start_and_finish(db):
    run = process(db, factories.acme_invoice())

    timeline = [(e.event_type, e.stage) for e in run.events]
    for name in ALL_STAGES[:-1]:
        assert ("stage_started", name.value) in timeline
        assert ("stage_completed", name.value) in timeline
        assert timeline.index(("stage_started", name.value)) < timeline.index(("stage_completed", name.value))
    assert ("decision_recorded", "PERSISTENCE") in timeline
    assert timeline[-1] == ("workflow_completed", "COMPLETED")


def test_every_attempt_gets_a_new_run_id(db):
    runs = [process(db, factories.acme_invoice()) for _ in range(3)]
    assert len({r.run_id for r in runs}) == 3


def test_history_is_readable_from_a_new_session(db):
    run = process(db, factories.acme_invoice())

    with Session(db.get_bind()) as fresh:
        stored = fresh.query(ProcessingRun).filter_by(run_id=run.run_id).one()
        assert stored.decision is Decision.APPROVED
        assert stage_names(stored) == ALL_STAGES
        assert len(stored.checks) == 11
        assert check(stored, "amount_within_tolerance").evidence["po_amount"] == "50000.00"


# --- duplicate invoice -------------------------------------------------------------------------


def test_duplicate_invoice_is_rejected(db):
    first = process(db, factories.acme_invoice())
    second = process(db, factories.acme_invoice())

    assert first.decision is Decision.APPROVED
    assert_completed_cleanly(second)  # a business rejection is not a processing failure
    assert second.decision is Decision.REJECTED
    duplicate = check(second, "duplicate_invoice")
    assert duplicate.status is CheckResult.FAIL and duplicate.effect is Decision.REJECTED
    assert duplicate.evidence["matches"][0]["run_id"] == first.run_id
    assert stage(second, S.DUPLICATE_CHECK).output["confirmed"] == [first.run_id]
    assert stage(second, S.DECISION).output["deciding_checks"] == ["duplicate_invoice"]
    db.refresh(first)
    assert first.decision is Decision.APPROVED


def test_re_exported_copy_is_rejected_by_vendor_and_invoice_number(db):
    original = factories.acme_invoice()
    copy = factories.resaved(original)
    assert copy != original

    process(db, original)
    second = process(db, copy)

    assert second.decision is Decision.REJECTED
    bases = check(second, "duplicate_invoice").evidence["matches"][0]["bases"]
    assert "vendor+invoice_number" in bases and "same_file" not in bases


def test_failed_attempt_does_not_count_as_an_earlier_invoice(db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("PO service unavailable")

    monkeypatch.setattr(orchestrator, "match_purchase_order", broken)
    failed = process(db, factories.acme_invoice())
    monkeypatch.undo()

    retry = process(db, factories.acme_invoice())

    assert failed.status is RunStatus.FAILED
    assert retry.decision is Decision.APPROVED


# --- amount mismatch ---------------------------------------------------------------------------


def test_amount_mismatch_needs_review(db):
    run = process(db, factories.custom_invoice(total="60,000.00"))  # PO-5001 is 50,000.00

    assert_completed_cleanly(run)
    assert run.decision is Decision.NEEDS_REVIEW
    amount = check(run, "amount_within_tolerance")
    assert amount.status is CheckResult.FAIL
    assert amount.evidence["difference"] == "10000.00"
    assert amount.evidence["tolerance_amount"] == "500.00"
    assert stage(run, S.DECISION).output["deciding_checks"] == ["amount_within_tolerance"]


def test_amount_within_one_percent_is_approved(db):
    run = process(db, factories.custom_invoice(total="50,400.00"))
    assert run.decision is Decision.APPROVED


def test_sample_bluewave_invoice_is_outside_tolerance(db):
    run = process(db, factories.bluewave_invoice())  # 118,000.00 against a 120,000.00 PO

    assert run.decision is Decision.NEEDS_REVIEW
    assert stage(run, S.DECISION).output["deciding_checks"] == ["amount_within_tolerance"]


# --- missing critical fields ------------------------------------------------------------------


def test_missing_critical_fields_need_review(db):
    run = process(db, factories.northstar_invoice_missing_fields())

    assert_completed_cleanly(run)
    assert run.decision is Decision.NEEDS_REVIEW
    assert check(run, "required_fields").evidence["missing"] == ["invoice_date", "po_number", "currency"]
    assert stage(run, S.REQUIRED_FIELD_VALIDATION).output["missing"] == ["invoice_date", "po_number", "currency"]
    assert stage(run, S.PO_MATCHING).output["match_status"] == "NO_PO_REFERENCE"
    assert check(run, "amount_within_tolerance").status is CheckResult.SKIPPED
    assert "required_fields" in run.decision_reason


def test_missing_invoice_number_and_vendor(db):
    run = process(db, factories.custom_invoice(vendor=None, invoice_number=None))

    assert run.decision is Decision.NEEDS_REVIEW
    assert check(run, "required_fields").evidence["missing"] == ["vendor_name", "invoice_number"]
    assert check(run, "duplicate_invoice").status is CheckResult.SKIPPED


def test_closed_po_needs_review(db):
    run = process(db, factories.custom_invoice(vendor="Northstar Services Pvt Ltd", po_number="PO-5003",
                                               total="75,000.00"))

    assert run.decision is Decision.NEEDS_REVIEW
    assert stage(run, S.DECISION).output["deciding_checks"] == ["po_status"]


# --- processing failures ------------------------------------------------------------------------


def test_extraction_failure_is_recorded_and_has_no_decision(db):
    run = process(db, b"this is not a pdf", "broken.pdf")

    assert run.status is RunStatus.FAILED
    assert run.decision is None and run.decision_reason is None
    assert run.failed_stage is S.EXTRACTION
    assert run.error_code == "invalid_pdf"
    assert "not a PDF" in run.error_message
    assert stage_names(run) == [S.RECEIVED, S.EXTRACTION, S.FAILED]  # later stages never ran
    assert [s.status for s in run.stages] == [StageStatus.COMPLETED, StageStatus.FAILED, StageStatus.FAILED]
    failed = stage(run, S.EXTRACTION)
    assert (failed.error_code, failed.error_type) == ("invalid_pdf", "InvalidPDFError")
    assert failed.completed_at is not None and failed.duration_ms >= 0
    assert run.checks == []
    assert run.extracted_data is None
    assert ("stage_failed", "EXTRACTION") in [(e.event_type, e.stage) for e in run.events]
    assert run.events[-1].event_type == "workflow_failed"


def test_scanned_pdf_without_ocr_fails_with_actionable_error(db, monkeypatch):
    def missing():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)
    run = process(db, factories.rasterize(factories.acme_invoice()))

    assert run.status is RunStatus.FAILED
    assert run.decision is None
    assert run.error_code == "ocr_unavailable"
    assert "brew install tesseract" in run.error_message


def test_unexpected_error_in_a_later_stage(db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("PO service unavailable")

    monkeypatch.setattr(orchestrator, "match_purchase_order", broken)
    run = process(db, factories.acme_invoice())

    assert run.status is RunStatus.FAILED
    assert run.decision is None
    assert run.failed_stage is S.PO_MATCHING
    assert run.error_code == "internal_error"
    assert stage_names(run) == ALL_STAGES[:6] + [S.FAILED]
    assert stage(run, S.PO_MATCHING).error_type == "RuntimeError"
    assert all(s.status is StageStatus.COMPLETED for s in run.stages[:5])
    # Work done before the failure is kept; nothing after it is invented.
    assert run.extracted_data is not None
    assert stage(run, S.DUPLICATE_CHECK).output["confirmed"] == []
    assert run.checks == []


def test_failure_rolls_back_partial_stage_work(db, monkeypatch):
    def broken(invoice, db_session):
        db_session.add(ProcessingRun(file_name="stray.pdf"))  # partial work that must not survive
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "match_purchase_order", broken)
    process(db, factories.acme_invoice())

    assert db.query(ProcessingRun).filter_by(file_name="stray.pdf").count() == 0
