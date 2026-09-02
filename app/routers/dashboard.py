# savoury-spud-backend/app/routers/dashboard.py
#
# Every route here requires X-Admin-Key (see app/core/security.py). No user
# accounts, no login page, no public signup surface — same posture as the
# real-estate CRM's dashboard, deliberately: one secret, one deployment,
# nothing to socially-engineer into resetting.

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.core.security import require_admin_key
from app.core.config import get_settings
from app.core.supabase import get_supabase
from app.models.schemas import (
    ProductIn, ProductPatch, ModifierPatch, AvailabilityPatch,
    OperatingHoursPatch, OrderStatusPatch, DeliveryFeeIn,
)
from app.services import orders, catalog, availability, availability_store, message_pipeline

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
settings = get_settings()


async def _org_id() -> UUID:
    db = await get_supabase()
    result = await db.table("organizations").select("id").eq("slug", settings.org_slug).single().execute()
    return result.data["id"]


# ── Orders ──────────────────────────────────────────────────────

@router.get("/orders")
async def list_orders(
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _: str = Depends(require_admin_key),
):
    """The 'show me everything ordered between Aug 20 and Aug 30' endpoint — filter by ?date_from=&date_to=."""
    org_id = await _org_id()
    rows, total = await orders.list_orders(org_id, status=status, date_from=date_from, date_to=date_to, page=page, limit=limit)
    return {"orders": rows, "total": total, "page": page, "limit": limit}


@router.get("/orders/{order_id}")
async def get_order(order_id: UUID, _: str = Depends(require_admin_key)):
    detail = await orders.get_cart_detail(order_id)
    if not detail or not detail.get("id"):
        raise HTTPException(status_code=404, detail="Order not found")
    return detail


@router.patch("/orders/{order_id}/status")
async def update_order_status(order_id: UUID, patch: OrderStatusPatch, background: BackgroundTasks, _: str = Depends(require_admin_key)):
    try:
        updated = await orders.update_status(order_id, patch.status)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    if patch.status == "COMPLETED":
        # Fire-and-forget, same pattern as the webhook handlers — the star-rating
        # prompt is a nice-to-have, never something that should hold up or fail
        # this status change if WhatsApp/Evolution is briefly unavailable.
        background.add_task(message_pipeline.send_feedback_prompt, order_id)

    return updated


@router.patch("/orders/{order_id}/delivery-fee")
async def set_order_delivery_fee(order_id: UUID, patch: DeliveryFeeIn, _: str = Depends(require_admin_key)):
    """
    Records the delivery fee she's quoted the customer directly — normally
    used AFTER the order is already PAID, once she's worked out the cost
    for that address. Purely for record-keeping on the dashboard; it does
    not trigger any further charge, since delivery is arranged and paid for
    however she and the customer agree, outside Paystack.
    """
    return await orders.set_delivery_fee(order_id, patch.delivery_fee)


@router.get("/metrics")
async def get_metrics(days: int = Query(7, ge=1, le=90), _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    return await orders.get_order_metrics(org_id, days=days)


# ── Availability ────────────────────────────────────────────────

@router.get("/availability")
async def get_availability(_: str = Depends(require_admin_key)):
    org_id = await _org_id()
    status_row = await availability_store.get_availability_settings(org_id)
    hours = await availability_store.get_operating_hours(org_id)
    now_local = availability.to_business_time(datetime.now(timezone.utc), settings.business_utc_offset_hours)
    today_hours = next((h for h in hours if h["day_of_week"] == now_local.weekday()), None)
    is_open, reason = availability.resolve_business_open(status_row, today_hours, now_local)
    return {
        "status": status_row.get("status"),
        "pause_reason": status_row.get("pause_reason"),
        "pause_message": status_row.get("pause_message"),
        "is_accepting_orders": is_open,
        "closed_reason": reason,
        "operating_hours": hours,
    }


@router.patch("/availability")
async def patch_availability(patch: AvailabilityPatch, _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    return await availability_store.set_status(org_id, patch.status, patch.pause_reason, patch.pause_message)


@router.put("/availability/hours")
async def put_operating_hours(patch: OperatingHoursPatch, _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    open_str = patch.open_time.isoformat() if patch.open_time else None
    close_str = patch.close_time.isoformat() if patch.close_time else None
    return await availability_store.upsert_operating_hours(org_id, patch.day_of_week, open_str, close_str, patch.is_closed)


# ── Catalog ─────────────────────────────────────────────────────

@router.get("/catalog")
async def get_catalog(include_unavailable: bool = True, _: str = Depends(require_admin_key)):
    """Dashboard defaults to seeing everything, including sold-out items — the ordering flow never does."""
    org_id = await _org_id()
    return await catalog.get_full_catalog(org_id, include_unavailable=include_unavailable)


@router.post("/catalog/products")
async def create_product(product: ProductIn, _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    return await catalog.create_product(org_id, product.model_dump(exclude_none=True))


@router.patch("/catalog/products/{product_id}")
async def patch_product(product_id: UUID, patch: ProductPatch, _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    updated = await catalog.update_product(product_id, org_id, patch.model_dump(exclude_none=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return updated


@router.patch("/catalog/products/{product_id}/availability")
async def set_product_availability(product_id: UUID, available: bool, _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    await catalog.set_product_availability(product_id, available, org_id)
    return {"product_id": str(product_id), "available": available}


@router.patch("/catalog/modifiers/{modifier_id}")
async def patch_modifier(modifier_id: UUID, patch: ModifierPatch, _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    db = await get_supabase()
    result = (
        await db.table("modifiers")
        .update(patch.model_dump(exclude_none=True))
        .eq("id", str(modifier_id))
        .execute()
    )
    await catalog.invalidate_catalog_cache(org_id)
    if not result.data:
        raise HTTPException(status_code=404, detail="Modifier not found")
    return result.data[0]


# ── Customers ───────────────────────────────────────────────────

@router.get("/customers")
async def list_customers(page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200), _: str = Depends(require_admin_key)):
    org_id = await _org_id()
    db = await get_supabase()
    offset = (page - 1) * limit
    result = (
        await db.table("customers")
        .select("*")
        .eq("org_id", str(org_id))
        .order("last_order_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    count_result = await db.table("customers").select("id", count="exact").eq("org_id", str(org_id)).execute()
    return {"customers": result.data or [], "total": count_result.count or 0, "page": page, "limit": limit}
