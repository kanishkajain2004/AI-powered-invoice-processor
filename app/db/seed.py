"""Reference data: vendors and purchase orders.

Run with:  python -m app.db.seed

The seed can be re-run safely. Vendors are matched by name and POs by po_number;
anything that already exists is left unchanged, so edits made in the app (for
example, closing a PO) are never overwritten.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PurchaseOrder, Vendor
from app.schemas.enums import POStatus


@dataclass(frozen=True)
class SeedPO:
    po_number: str
    vendor_name: str
    currency: str
    total_amount: Decimal
    status: POStatus


SEED_PURCHASE_ORDERS: tuple[SeedPO, ...] = (
    SeedPO("PO-5001", "Acme Office Supplies Pvt Ltd", "INR", Decimal("50000.00"), POStatus.APPROVED),
    SeedPO("PO-5002", "Bluewave Technologies Pvt Ltd", "INR", Decimal("120000.00"), POStatus.APPROVED),
    SeedPO("PO-5003", "Northstar Services Pvt Ltd", "INR", Decimal("75000.00"), POStatus.CLOSED),
    SeedPO("PO-5004", "Acme Office Supplies Pvt Ltd", "INR", Decimal("80000.00"), POStatus.APPROVED),
)


@dataclass
class SeedResult:
    vendors_created: list[str] = field(default_factory=list)
    pos_created: list[str] = field(default_factory=list)
    pos_existing: list[str] = field(default_factory=list)


def _get_or_create_vendor(db: Session, name: str, result: SeedResult) -> Vendor:
    vendor = db.scalar(select(Vendor).where(Vendor.name == name))
    if vendor is None:
        vendor = Vendor(name=name)
        db.add(vendor)
        db.flush()
        result.vendors_created.append(name)
    return vendor


def seed_purchase_orders(
    db: Session, purchase_orders: tuple[SeedPO, ...] = SEED_PURCHASE_ORDERS
) -> SeedResult:
    """Insert any missing vendors and POs in a single transaction."""
    result = SeedResult()
    try:
        for spec in purchase_orders:
            if db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == spec.po_number)):
                result.pos_existing.append(spec.po_number)
                continue
            vendor = _get_or_create_vendor(db, spec.vendor_name, result)
            db.add(
                PurchaseOrder(
                    po_number=spec.po_number,
                    vendor=vendor,
                    currency=spec.currency,
                    total_amount=spec.total_amount,
                    status=spec.status,
                )
            )
            db.flush()
            result.pos_created.append(spec.po_number)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def main() -> None:
    from app.db.database import SessionLocal, engine, init_db

    init_db()
    with SessionLocal() as db:
        result = seed_purchase_orders(db)
    print(f"Database: {engine.url}")
    print(f"Vendors created: {len(result.vendors_created)} {result.vendors_created}")
    print(f"POs created:     {len(result.pos_created)} {result.pos_created}")
    print(f"POs existing:    {len(result.pos_existing)} {result.pos_existing}")


if __name__ == "__main__":
    main()
