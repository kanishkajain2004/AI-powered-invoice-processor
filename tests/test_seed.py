import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.database import create_db_engine
from app.db.models import PurchaseOrder, Vendor
from app.db.seed import SEED_PURCHASE_ORDERS, seed_purchase_orders
from app.schemas import POStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXPECTED = {
    "PO-5001": ("Acme Office Supplies Pvt Ltd", "INR", Decimal("50000.00"), POStatus.APPROVED),
    "PO-5002": ("Bluewave Technologies Pvt Ltd", "INR", Decimal("120000.00"), POStatus.APPROVED),
    "PO-5003": ("Northstar Services Pvt Ltd", "INR", Decimal("75000.00"), POStatus.CLOSED),
    "PO-5004": ("Acme Office Supplies Pvt Ltd", "INR", Decimal("80000.00"), POStatus.APPROVED),
}


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _snapshot(db: Session) -> dict:
    return {
        po.po_number: (po.vendor.name, po.currency, po.total_amount, po.status)
        for po in db.scalars(select(PurchaseOrder))
    }


def test_seed_creates_expected_purchase_orders(db_session):
    result = seed_purchase_orders(db_session)

    assert sorted(result.pos_created) == sorted(EXPECTED)
    assert result.pos_existing == []
    assert _snapshot(db_session) == EXPECTED
    assert all(isinstance(v[2], Decimal) for v in _snapshot(db_session).values())


def test_seed_shares_vendor_across_pos(db_session):
    result = seed_purchase_orders(db_session)

    assert len(result.vendors_created) == 3
    assert _count(db_session, Vendor) == 3
    acme = db_session.scalar(select(Vendor).where(Vendor.name == "Acme Office Supplies Pvt Ltd"))
    assert sorted(po.po_number for po in acme.purchase_orders) == ["PO-5001", "PO-5004"]


def test_seed_is_repeatable_without_duplicates(db_session):
    seed_purchase_orders(db_session)
    second = seed_purchase_orders(db_session)
    third = seed_purchase_orders(db_session)

    for result in (second, third):
        assert result.pos_created == []
        assert result.vendors_created == []
        assert sorted(result.pos_existing) == sorted(EXPECTED)
    assert _count(db_session, PurchaseOrder) == 4
    assert _count(db_session, Vendor) == 3


def test_seed_does_not_overwrite_existing_changes(db_session):
    seed_purchase_orders(db_session)
    po = db_session.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-5001"))
    po.status = POStatus.CLOSED
    db_session.commit()

    seed_purchase_orders(db_session)

    db_session.expire_all()
    assert db_session.scalar(select(PurchaseOrder.status).where(PurchaseOrder.po_number == "PO-5001")) is POStatus.CLOSED


def test_seed_fills_in_missing_records_only(db_session):
    seed_purchase_orders(db_session, SEED_PURCHASE_ORDERS[:2])
    result = seed_purchase_orders(db_session)

    assert sorted(result.pos_created) == ["PO-5003", "PO-5004"]
    assert sorted(result.pos_existing) == ["PO-5001", "PO-5002"]
    assert result.vendors_created == ["Northstar Services Pvt Ltd"]
    assert _snapshot(db_session) == EXPECTED


def test_seed_command_is_repeatable_and_persists(db_url):
    env = os.environ | {"DATABASE_URL": db_url}
    cmd = [sys.executable, "-m", "app.db.seed"]

    first = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=True)
    second = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=True)

    assert "POs created:     4" in first.stdout
    assert "POs created:     0" in second.stdout
    assert "POs existing:    4" in second.stdout

    # Read back from this process: the data outlived the processes that wrote it.
    engine = create_db_engine(db_url)
    with Session(engine) as db:
        assert _snapshot(db) == EXPECTED
    engine.dispose()
