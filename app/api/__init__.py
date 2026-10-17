from fastapi import APIRouter

from app.api.routes import health, invoices, purchase_orders, runs

API_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_PREFIX)
api_router.include_router(health.router)
api_router.include_router(invoices.router)
api_router.include_router(runs.router)
api_router.include_router(purchase_orders.router)

__all__ = ["API_PREFIX", "api_router"]
