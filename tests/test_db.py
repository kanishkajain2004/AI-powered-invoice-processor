from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.database import create_db_engine, init_db
from app.db.models import ProcessingRun, PurchaseOrder, RunEvent, ValidationCheck, Vendor
from app.schemas import CheckResult, Decision, POStatus, RunStatus

EXPECTED_TABLES = {"vendors", "purchase_orders", "processing_runs", "run_stages", "validation_checks", "run_events"}


def _make_run(**overrides) -> ProcessingRun:
    fields = dict(file_name="inv.pdf", invoice_number="INV-1", total_amount=Decimal("100.00"))
    return ProcessingRun(**(fields | overrides))


# --- initialization -----------------------------------------------------------


def test_init_db_creates_all_tables(db_engine):
    assert EXPECTED_TABLES <= set(inspect(db_engine).get_table_names())


def test_init_db_creates_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "app.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    init_db(engine)
    engine.dispose()
    assert db_path.exists()


def test_init_db_is_idempotent_and_keeps_rows(db_engine, db_session):
    db_session.add(Vendor(name="Acme"))
    db_session.commit()

    init_db(db_engine)
    init_db(db_engine)

    assert db_session.scalar(select(Vendor.name)) == "Acme"


def test_sqlite_foreign_keys_enforced(db_engine, db_session):
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    db_session.add(ValidationCheck(processing_run_id=12345, name="x", category="fields", status=CheckResult.PASS))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_po_number_is_unique(db_session):
    vendor = Vendor(name="Acme")
    db_session.add_all(
        [
            PurchaseOrder(po_number="PO-1", vendor=vendor, currency="INR", total_amount=Decimal("1"), status=POStatus.APPROVED),
            PurchaseOrder(po_number="PO-1", vendor=vendor, currency="INR", total_amount=Decimal("2"), status=POStatus.APPROVED),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_invalid_enum_value_rejected(db_session):
    db_session.add(_make_run(status="NOT_A_STATUS"))
    with pytest.raises(StatementError):
        db_session.commit()


# --- money ---------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["0.01", "50000.00", "120000", "99999999999.99", "-15.50"])
def test_money_round_trips_exactly(db_session, amount):
    db_session.add(_make_run(total_amount=Decimal(amount)))
    db_session.commit()
    db_session.expire_all()

    stored = db_session.scalar(select(ProcessingRun.total_amount))
    assert isinstance(stored, Decimal)
    assert stored == Decimal(amount)


def test_money_is_stored_as_integer_minor_units(db_session):
    db_session.add(_make_run(total_amount=Decimal("1234.56")))
    db_session.commit()
    raw = db_session.execute(text("SELECT total_amount, typeof(total_amount) FROM processing_runs")).one()
    assert tuple(raw) == (123456, "integer")


@pytest.mark.parametrize("bad", [1.1, Decimal("1.005"), Decimal("NaN"), Decimal("Infinity")])
def test_money_rejects_floats_and_excess_precision(db_session, bad):
    db_session.add(_make_run(total_amount=bad))
    with pytest.raises(StatementError):
        db_session.commit()


# --- persistence -----------------------------------------------------------------


def test_processing_history_survives_restart(db_url):
    engine = create_db_engine(db_url)
    init_db(engine)
    with Session(engine) as db:
        run = _make_run(status=RunStatus.COMPLETED, decision=Decision.NEEDS_REVIEW, extracted_data={"po_number": "PO-5001"})
        run.checks.append(ValidationCheck(name="po_match", category="purchase_order", status=CheckResult.PASS))
        run.checks.append(
            ValidationCheck(name="amount_within_tolerance", category="amounts", status=CheckResult.FAIL, evidence={"diff": "10.00"})
        )
        run.events.append(RunEvent(event_type="run_started"))
        run.events.append(RunEvent(event_type="status_changed", payload={"to": "NEEDS_REVIEW"}))
        db.add(run)
        db.commit()
        run_id = run.id
    engine.dispose()

    # "Restart": a brand-new engine on the same file, with startup init_db.
    engine = create_db_engine(db_url)
    init_db(engine)
    with Session(engine) as db:
        run = db.get(ProcessingRun, run_id)
        assert run is not None
        assert run.status is RunStatus.COMPLETED
        assert run.decision is Decision.NEEDS_REVIEW
        assert run.total_amount == Decimal("100.00")
        assert run.extracted_data == {"po_number": "PO-5001"}
        assert [(c.name, c.status) for c in run.checks] == [
            ("po_match", CheckResult.PASS),
            ("amount_within_tolerance", CheckResult.FAIL),
        ]
        assert run.checks[1].evidence == {"diff": "10.00"}
        assert [e.event_type for e in run.events] == ["run_started", "status_changed"]
        assert all(e.created_at is not None for e in run.events)
    engine.dispose()


def test_deleting_run_cascades_to_checks_and_events(db_session):
    run = _make_run()
    run.checks.append(ValidationCheck(name="x", category="fields", status=CheckResult.PASS))
    run.events.append(RunEvent(event_type="run_started"))
    db_session.add(run)
    db_session.commit()

    db_session.delete(run)
    db_session.commit()

    assert db_session.scalar(select(ValidationCheck)) is None
    assert db_session.scalar(select(RunEvent)) is None


def test_stale_schema_is_refused_at_startup(tmp_path):
    import sqlite3

    from app.db.database import SchemaMismatchError

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # a processing_runs table from an older version
        conn.execute("CREATE TABLE processing_runs (id INTEGER PRIMARY KEY, file_name TEXT, status TEXT)")

    engine = create_db_engine(f"sqlite:///{path}")
    with pytest.raises(SchemaMismatchError, match=r"processing_runs lacks run_id.*delete it, then run"):
        init_db(engine)
    engine.dispose()
