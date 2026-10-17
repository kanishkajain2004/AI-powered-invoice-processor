"""Duplicate detection, the approval uniqueness guard and transaction-safe decision recording."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ProcessingRun
from app.db.seed import seed_purchase_orders
from app.duplicates import duplicate_keys, find_duplicates
from app.matching import match_purchase_order
from app.schemas import CheckResult, Decision, DuplicateBasis, NormalizedInvoice, RunStatus
from app.workflow import decision as decision_module
from app.workflow.decision import record_decision

D = Decimal
ACME = "Acme Office Supplies Pvt Ltd"
BLUEWAVE = "Bluewave Technologies Pvt Ltd"


def invoice(**overrides) -> NormalizedInvoice:
    fields = dict(vendor_name=ACME, invoice_number="INV-1001", invoice_date=date(2026, 9, 15), po_number="PO-5001",
                  currency="INR", total_amount=D("50000.00"))
    return NormalizedInvoice(**(fields | overrides))


def add_run(db, inv: NormalizedInvoice, decision=Decision.APPROVED, file_hash=None, file_name="earlier.pdf",
            status=None) -> ProcessingRun:
    keys = duplicate_keys(inv)
    if status is None:
        status = RunStatus.FAILED if decision is None else RunStatus.COMPLETED
    run = ProcessingRun(
        file_name=file_name, file_hash=file_hash, status=status, decision=decision,
        vendor_name=inv.vendor_name, invoice_number=inv.invoice_number, invoice_date=inv.invoice_date,
        total_amount=inv.total_amount, vendor_key=keys.vendor_key, vendor_core=keys.vendor_core,
        invoice_key=keys.invoice_key,
    )
    db.add(run)
    db.commit()
    return run


def new_run(db, file_hash=None, file_name="upload.pdf") -> ProcessingRun:
    run = ProcessingRun(file_name=file_name, file_hash=file_hash, status=RunStatus.PROCESSING)
    db.add(run)
    db.commit()
    return run


# --- keys ---------------------------------------------------------------------------------


def test_primary_key_is_normalized_vendor_identity_plus_invoice_number():
    a = duplicate_keys(invoice())
    b = duplicate_keys(invoice(vendor_name="M/s ACME OFFICE SUPPLIES PRIVATE LIMITED", invoice_number="inv 1001"))

    assert a == b
    assert (a.vendor_key, a.vendor_core, a.invoice_key) == ("acme office supplies pvt ltd", "acme office supplies", "INV-1001")


def test_keys_are_none_when_fields_missing():
    keys = duplicate_keys(NormalizedInvoice())
    assert (keys.vendor_key, keys.vendor_core, keys.invoice_key) == (None, None, None)


# --- exact duplicates -------------------------------------------------------------------------


def test_no_earlier_runs(db_session):
    result = find_duplicates(db_session, invoice())
    assert (result.confirmed, result.pending, result.potential) == ([], [], [])
    assert result.has_primary_key


def test_same_vendor_and_number_as_approved_run_is_confirmed(db_session):
    earlier = add_run(db_session, invoice())

    result = find_duplicates(db_session, invoice(vendor_name="ACME OFFICE SUPPLIES PRIVATE LIMITED", invoice_number="inv-1001"))

    assert [m.run_id for m in result.confirmed] == [earlier.run_id]
    assert DuplicateBasis.VENDOR_AND_INVOICE_NUMBER in result.confirmed[0].bases
    assert result.pending == [] and result.potential == []


def test_same_invoice_awaiting_review_is_pending(db_session):
    earlier = add_run(db_session, invoice(), decision=Decision.NEEDS_REVIEW)

    result = find_duplicates(db_session, invoice())

    assert [m.run_id for m in result.pending] == [earlier.run_id]
    assert result.confirmed == []


@pytest.mark.parametrize(
    "decision, status",
    [(Decision.REJECTED, RunStatus.COMPLETED), (None, RunStatus.FAILED), (None, RunStatus.PROCESSING), (None, RunStatus.RECEIVED)],
)
def test_runs_that_are_not_invoices_of_record_are_ignored(db_session, decision, status):
    add_run(db_session, invoice(), decision=decision, status=status)
    result = find_duplicates(db_session, invoice())
    assert (result.confirmed, result.pending, result.potential) == ([], [], [])


def test_same_number_from_a_different_vendor_is_not_a_duplicate(db_session):
    add_run(db_session, invoice(vendor_name=BLUEWAVE))
    result = find_duplicates(db_session, invoice(total_amount=D("1.00")))
    assert (result.confirmed, result.pending, result.potential) == ([], [], [])


def test_different_number_date_and_amount_is_not_a_duplicate(db_session):
    add_run(db_session, invoice(invoice_number="INV-1002", invoice_date=date(2026, 9, 16), total_amount=D("1.00")))
    result = find_duplicates(db_session, invoice())
    assert (result.confirmed, result.pending, result.potential) == ([], [], [])


def test_identical_file_is_confirmed_even_without_fields(db_session):
    earlier = add_run(db_session, invoice(), file_hash="abc123")

    result = find_duplicates(db_session, NormalizedInvoice(), file_hash="abc123")

    assert [m.run_id for m in result.confirmed] == [earlier.run_id]
    assert result.confirmed[0].bases == [DuplicateBasis.SAME_FILE]


def test_exclude_current_run(db_session):
    current = add_run(db_session, invoice(), decision=Decision.NEEDS_REVIEW)
    assert find_duplicates(db_session, invoice(), exclude_run_id=current.id).pending == []


def test_all_bases_for_one_run_are_merged(db_session):
    add_run(db_session, invoice(), file_hash="abc123")

    match = find_duplicates(db_session, invoice(), file_hash="abc123").confirmed[0]

    assert set(match.bases) == {DuplicateBasis.VENDOR_AND_INVOICE_NUMBER, DuplicateBasis.SAME_FILE,
                                DuplicateBasis.VENDOR_DATE_AMOUNT}


# --- potential duplicates --------------------------------------------------------------------------


@pytest.mark.parametrize("decision", [Decision.APPROVED, Decision.NEEDS_REVIEW])
def test_same_vendor_date_and_amount_is_potential(db_session, decision):
    earlier = add_run(db_session, invoice(invoice_number="INV-1001"), decision=decision)

    result = find_duplicates(db_session, invoice(invoice_number="INV-9999"))

    assert [m.run_id for m in result.potential] == [earlier.run_id]
    assert result.potential[0].bases == [DuplicateBasis.VENDOR_DATE_AMOUNT]
    assert result.confirmed == [] and result.pending == []


@pytest.mark.parametrize(
    "change", [{"total_amount": D("50000.01")}, {"invoice_date": date(2026, 9, 16)}, {"vendor_name": BLUEWAVE}]
)
def test_vendor_date_amount_must_all_match(db_session, change):
    add_run(db_session, invoice())
    assert find_duplicates(db_session, invoice(invoice_number="INV-9999", **change)).potential == []


def test_vendor_legal_form_variant_with_same_number_is_potential_not_confirmed(db_session):
    earlier = add_run(db_session, invoice(vendor_name="Acme Office Supplies"))

    result = find_duplicates(db_session, invoice(total_amount=D("1.00")))

    assert result.confirmed == []
    assert [m.run_id for m in result.potential] == [earlier.run_id]
    assert result.potential[0].bases == [DuplicateBasis.INVOICE_NUMBER_VENDOR_VARIANT]


# --- database guard ----------------------------------------------------------------------------------


def test_database_refuses_a_second_approval_of_the_same_invoice(db_session):
    add_run(db_session, invoice())
    with pytest.raises(IntegrityError):
        add_run(db_session, invoice(vendor_name="ACME OFFICE SUPPLIES PRIVATE LIMITED", invoice_number="inv 1001"))


@pytest.mark.parametrize("decision", [Decision.NEEDS_REVIEW, Decision.REJECTED, None])
def test_non_approved_runs_may_share_the_key(db_session, decision):
    add_run(db_session, invoice())
    add_run(db_session, invoice(), decision=decision)
    add_run(db_session, invoice(), decision=decision)


def test_approved_runs_without_keys_do_not_collide(db_session):
    add_run(db_session, NormalizedInvoice())
    add_run(db_session, NormalizedInvoice())


# --- record_decision ---------------------------------------------------------------------------------


@pytest.fixture
def db(db_session):
    seed_purchase_orders(db_session)
    return db_session


def decide(db, inv, file_hash=None):
    run = new_run(db, file_hash=file_hash)
    report = record_decision(db, run, inv, match_purchase_order(inv, db))
    return run, report


def test_first_submission_is_approved_and_recorded(db):
    run, report = decide(db, invoice())

    assert report.decision is Decision.APPROVED
    db.expire_all()
    stored = db.get(ProcessingRun, run.id)
    assert stored.decision is Decision.APPROVED
    assert (stored.vendor_key, stored.invoice_key) == ("acme office supplies pvt ltd", "INV-1001")
    assert stored.purchase_order.po_number == "PO-5001"
    assert [c.name for c in stored.checks] == [c.name for c in report.checks]
    tolerance = next(c for c in stored.checks if c.name == "amount_within_tolerance")
    assert tolerance.status is CheckResult.PASS and tolerance.mandatory
    assert tolerance.evidence["po_amount"] == "50000.00"  # stored as JSON
    assert stored.events[-1].event_type == "decision_recorded"
    assert stored.events[-1].payload["decision"] == "APPROVED"


def test_resubmitting_an_approved_invoice_is_rejected(db):
    first, _ = decide(db, invoice())

    second, report = decide(db, invoice(vendor_name="Acme Office Supplies Private Limited", invoice_number="inv-1001"))

    assert report.decision is Decision.REJECTED
    db.expire_all()
    stored = db.get(ProcessingRun, second.id)
    assert stored.decision is Decision.REJECTED
    duplicate = next(c for c in stored.checks if c.name == "duplicate_invoice")
    assert duplicate.effect is Decision.REJECTED
    assert duplicate.evidence["matches"][0]["run_id"] == first.run_id
    assert db.get(ProcessingRun, first.id).decision is Decision.APPROVED


def test_resubmitting_the_same_file_is_rejected(db):
    decide(db, invoice(), file_hash="f00d")
    _, report = decide(db, invoice(invoice_number="INV-1001-COPY"), file_hash="f00d")
    assert report.decision is Decision.REJECTED


def test_resubmitting_an_invoice_under_review_needs_review(db):
    first, first_report = decide(db, invoice(po_number="PO-5003"))  # closed PO -> review
    assert first_report.decision is Decision.NEEDS_REVIEW

    _, report = decide(db, invoice(po_number="PO-5003"))

    assert report.decision is Decision.NEEDS_REVIEW
    assert "duplicate_invoice" in report.deciding_checks


def test_potential_duplicate_goes_to_review_not_rejection(db):
    decide(db, invoice())
    _, report = decide(db, invoice(invoice_number="INV-2002"))

    assert report.decision is Decision.NEEDS_REVIEW
    assert report.deciding_checks == ["potential_duplicate"]


def test_rejected_resubmission_does_not_block_later_checks(db):
    decide(db, invoice())
    decide(db, invoice())  # rejected
    _, report = decide(db, invoice())  # still a duplicate of the approved one, not of the rejected one

    matches = report.check("duplicate_invoice").evidence["matches"]
    assert [m["decision"] for m in matches] == ["APPROVED"]


def test_record_decision_requires_a_committed_run(db):
    with pytest.raises(ValueError):
        record_decision(db, ProcessingRun(file_name="x.pdf"), invoice(), match_purchase_order(invoice(), db))


# --- concurrency ---------------------------------------------------------------------------------


def test_stale_duplicate_check_is_caught_by_the_database(db, monkeypatch):
    """Simulates a race: the second request's first duplicate check misses the first approval."""
    decide(db, invoice())

    real = decision_module.find_duplicates
    calls = []

    def stale_then_real(session, inv, **kwargs):
        calls.append(1)
        result = real(session, inv, **kwargs)
        if len(calls) == 1:
            result.confirmed.clear()  # what a check made before the other commit would have seen
        return result

    monkeypatch.setattr(decision_module, "find_duplicates", stale_then_real)
    second, report = decide(db, invoice())

    assert len(calls) == 2  # rolled back after the IntegrityError and re-checked
    assert report.decision is Decision.REJECTED
    db.expire_all()
    assert db.get(ProcessingRun, second.id).decision is Decision.REJECTED
    approved = db.scalars(select(ProcessingRun).where(ProcessingRun.decision == Decision.APPROVED)).all()
    assert len(approved) == 1


def test_concurrent_submissions_approve_exactly_once(db_engine, monkeypatch):
    """Two threads, two sessions, both checking for duplicates before either commits."""
    with Session(db_engine) as setup:
        seed_purchase_orders(setup)

    real = decision_module.find_duplicates
    barrier = threading.Barrier(2, timeout=10)
    first_check: set[int] = set()

    def synchronised(session, inv, **kwargs):
        result = real(session, inv, **kwargs)
        ident = threading.get_ident()
        if ident not in first_check:  # only the first check of each thread waits
            first_check.add(ident)
            barrier.wait()
        return result

    monkeypatch.setattr(decision_module, "find_duplicates", synchronised)
    decisions, errors = [], []

    def submit():
        try:
            with Session(db_engine, expire_on_commit=False) as session:
                run, report = decide(session, invoice())
                decisions.append(report.decision)
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert sorted(decisions) == [Decision.APPROVED, Decision.REJECTED]
    with Session(db_engine) as check:
        decisions_stored = sorted(r.decision for r in check.scalars(select(ProcessingRun)))
    assert decisions_stored == [Decision.APPROVED, Decision.REJECTED]
