# savoury-spud-backend/app/routers/health.py

from fastapi import APIRouter
from app.core.supabase import get_supabase

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    db_ok = True
    try:
        db = await get_supabase()
        await db.table("organizations").select("id").limit(1).execute()
    except Exception:
        db_ok = False

    return {"status": "ok" if db_ok else "degraded", "database": "connected" if db_ok else "unreachable"}
