# savoury-spud-backend/app/services/availability_store.py
#
# I/O boundary for availability_settings + operating_hours. Pure decision
# logic (is the business actually open right now) lives in availability.py
# and takes the dicts this module fetches — same split as
# catalog.py / pricing_engine.py.

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from app.core.supabase import get_supabase
from app.utils.logger import log


async def get_availability_settings(org_id: UUID) -> dict:
    db = await get_supabase()
    result = await db.table("availability_settings").select("*").eq("org_id", str(org_id)).single().execute()
    return result.data or {"org_id": str(org_id), "status": "OPEN", "pause_reason": None, "pause_message": None}


async def get_operating_hours(org_id: UUID) -> list[dict]:
    db = await get_supabase()
    result = (
        await db.table("operating_hours")
        .select("*")
        .eq("org_id", str(org_id))
        .order("day_of_week")
        .execute()
    )
    return result.data or []


async def get_operating_hours_for_day(org_id: UUID, day_of_week: int) -> Optional[dict]:
    db = await get_supabase()
    result = (
        await db.table("operating_hours")
        .select("*")
        .eq("org_id", str(org_id))
        .eq("day_of_week", day_of_week)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


async def set_status(org_id: UUID, status: str, pause_reason: Optional[str] = None, pause_message: Optional[str] = None) -> dict:
    db = await get_supabase()
    payload = {
        "status": status,
        "pause_reason": pause_reason,
        "pause_message": pause_message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.table("availability_settings").upsert({**payload, "org_id": str(org_id)}, on_conflict="org_id").execute()
    await log.info("AVAILABILITY_STATUS_CHANGED", ref_type="org", ref_id=org_id, metadata={"status": status, "reason": pause_reason})
    return result.data[0] if result.data else payload


async def upsert_operating_hours(org_id: UUID, day_of_week: int, open_time: Optional[str], close_time: Optional[str], is_closed: bool) -> dict:
    db = await get_supabase()
    payload = {
        "org_id": str(org_id),
        "day_of_week": day_of_week,
        "open_time": open_time,
        "close_time": close_time,
        "is_closed": is_closed,
    }
    result = await db.table("operating_hours").upsert(payload, on_conflict="org_id,day_of_week").execute()
    return result.data[0] if result.data else payload
