from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.db.models import PurchaseOrder
from app.schemas import PurchaseOrderRead

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])


@router.get("", response_model=list[PurchaseOrderRead])
def list_purchase_orders(db: Session = Depends(get_db)) -> list[PurchaseOrderRead]:
    pos = db.scalars(
        select(PurchaseOrder).options(joinedload(PurchaseOrder.vendor)).order_by(PurchaseOrder.po_number)
    )
    return [
        PurchaseOrderRead(
            po_number=po.po_number,
            vendor_name=po.vendor.name,
            currency=po.currency,
            total_amount=po.total_amount,
            status=po.status,
        )
        for po in pos
    ]
