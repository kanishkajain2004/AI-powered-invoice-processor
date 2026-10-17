"""Invariants that must hold for every stored run, checked over a varied, randomized batch.

The batch mixes valid invoices, wrong amounts, wrong vendors, closed and unknown POs,
missing fields, exact and near duplicates, and unreadable files, all processed by the
real workflow. Every resulting run is then checked against the rules that tie status,
decision, checks, stages and money together.
"""

import random
from collections import Counter
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ProcessingRun
from app.schemas import DECISION_PRIORITY, CheckResult, Decision, RunStatus, StageStatus, WorkflowStage
from app.workflow import process_invoice
from tests import factories

S = WorkflowStage
ORDER = [S.RECEIVED, S.EXTRACTION, S.NORMALIZATION, S.REQUIRED_FIELD_VALIDATION, S.DUPLICATE_CHECK,
         S.PO_MATCHING, S.BUSINESS_VALIDATION, S.DECISION, S.PERSISTENCE]
VENDORS = {"PO-5001": "Acme Office Supplies Pvt Ltd", "PO-5002": "Bluewave Technologies Pvt Ltd",
           "PO-5003": "Northstar Services Pvt Ltd", "PO-5004": "Acme Office Supplies Pvt Ltd"}
PO_AMOUNT = {"PO-5001": 50_000, "PO-5002": 120_000, "PO-5003": 75_000, "PO-5004": 80_000}


def _random_document(rng: random.Random, history: list[bytes]) -> bytes:
    kind = rng.choices(["valid", "amount", "vendor", "missing", "unknown_po", "repeat", "garbage"],
                       weights=[5, 3, 2, 2, 1, 3, 1])[0]
    if kind == "repeat" and history:
        return rng.choice(history)
    if kind == "garbage":
        return rng.choice([b"not a pdf", b"%PDF-1.7\n" + bytes(rng.randrange(256) for _ in range(300))])
    po = rng.choice(list(PO_AMOUNT))
    total = Decimal(PO_AMOUNT[po]) * (Decimal(rng.choice(["0.9", "1.2", "1.05"])) if kind == "amount" else
                                      Decimal(1) + Decimal(rng.randrange(-80, 81)) / 10_000)
    fields = dict(
        vendor=rng.choice(list(VENDORS.values())) if kind == "vendor" else VENDORS[po],
        invoice_number=f"INV-{rng.randrange(1, 40)}",  # small range: genuine number collisions happen
        invoice_date=f"{rng.randrange(1, 28):02d}/{rng.randrange(1, 13):02d}/2026",
        po_number="PO-9999" if kind == "unknown_po" else po,
        total=f"{total.quantize(Decimal('0.01')):,}",
    )
    if kind == "missing":
        fields[rng.choice(["vendor", "invoice_number", "invoice_date", "po_number", "currency", "total"])] = None
    return factories.custom_invoice(**fields)


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    from sqlalchemy.orm import Session

    from app.db.database import create_db_engine, init_db
    from app.db.seed import seed_purchase_orders

    settings = get_settings()
    original_upload_dir = settings.upload_dir
    settings.upload_dir = tmp_path_factory.mktemp("uploads")
    engine = create_db_engine(f"sqlite:///{tmp_path_factory.mktemp('db') / 'batch.db'}")
    init_db(engine)
    rng = random.Random(20261017)
    with Session(engine, expire_on_commit=False) as db:
        seed_purchase_orders(db)
        history: list[bytes] = []
        for n in range(60):
            content = _random_document(rng, history)
            history.append(content)
            process_invoice(db, file_name=f"doc-{n}.pdf", content=content)
        runs = list(db.scalars(select(ProcessingRun).order_by(ProcessingRun.id)))
        for run in runs:  # load relationships while the session is open
            _ = run.stages, run.checks
        yield runs
    settings.upload_dir = original_upload_dir
    engine.dispose()


def test_batch_covers_every_outcome(batch):
    outcomes = Counter(r.decision.value if r.decision else r.status.value for r in batch)
    assert len(batch) == 60
    assert {"APPROVED", "NEEDS_REVIEW", "REJECTED", "FAILED"} <= set(outcomes), outcomes


def test_status_and_decision_are_consistent(batch):
    for run in batch:
        assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED), run.run_id
        if run.status is RunStatus.COMPLETED:
            assert run.decision is not None and run.decision_reason
            assert run.failed_stage is None and run.error_code is None
        else:
            assert run.decision is None and run.decision_reason is None
            assert run.failed_stage is not None and run.error_code
            assert run.checks == []


def test_decision_equals_highest_priority_check_effect(batch):
    for run in (r for r in batch if r.status is RunStatus.COMPLETED):
        assert len(run.checks) == 11
        strongest = max((c.effect for c in run.checks), key=DECISION_PRIORITY.__getitem__)
        assert run.decision is strongest, run.run_id
        if run.decision is Decision.APPROVED:
            assert all(c.status is not CheckResult.FAIL for c in run.checks)
            assert all(c.status is not CheckResult.SKIPPED for c in run.checks if c.mandatory)
        for check in run.checks:  # effect is a pure function of status, mandatory and failure decision
            if check.status in (CheckResult.PASS, CheckResult.WARN):
                assert check.effect is Decision.APPROVED


def test_rejections_are_only_confirmed_duplicates(batch):
    for run in (r for r in batch if r.decision is Decision.REJECTED):
        rejecting = [c.name for c in run.checks if c.effect is Decision.REJECTED]
        assert rejecting == ["duplicate_invoice"], run.run_id
        earlier = run.checks[1].evidence["matches"]
        assert earlier and all(m["decision"] == "APPROVED" for m in earlier)


def test_no_invoice_is_approved_twice(batch):
    approved = [(r.vendor_key, r.invoice_key) for r in batch if r.decision is Decision.APPROVED]
    assert len(approved) == len(set(approved))


def test_stages_are_a_prefix_of_the_workflow(batch):
    for run in batch:
        names = [s.stage for s in run.stages]
        body, terminal = names[:-1], names[-1]
        assert body == ORDER[: len(body)], run.run_id
        assert terminal is (S.COMPLETED if run.status is RunStatus.COMPLETED else S.FAILED)
        assert [s.sequence for s in run.stages] == list(range(1, len(names) + 1))
        if run.status is RunStatus.FAILED:
            assert run.stages[-2].status is StageStatus.FAILED and run.stages[-2].stage is run.failed_stage
        else:
            assert all(s.status is StageStatus.COMPLETED for s in run.stages)
        assert all(s.duration_ms is not None and s.duration_ms >= 0 for s in run.stages)


def test_money_is_exact_two_place_decimal(batch):
    for run in batch:
        for value in (run.subtotal, run.tax_amount, run.total_amount):
            if value is not None:
                assert isinstance(value, Decimal) and value.as_tuple().exponent == -2
        tolerance = next((c for c in run.checks if c.name == "amount_within_tolerance"), None)
        if tolerance is not None and tolerance.status is not CheckResult.SKIPPED:
            ev = tolerance.evidence
            assert Decimal(ev["difference"]) == Decimal(ev["invoice_total"]) - Decimal(ev["po_amount"])
            assert Decimal(ev["tolerance_amount"]) <= Decimal(ev["po_amount"]) * Decimal(ev["tolerance_pct"]) / 100
            assert (tolerance.status is CheckResult.PASS) == (abs(Decimal(ev["difference"])) <= Decimal(ev["tolerance_amount"]))
