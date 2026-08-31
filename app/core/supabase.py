# savoury-spud-backend/app/core/supabase.py
#
# Single Supabase client using the service_role key.
# This bypasses RLS — it is ONLY used server-side in FastAPI.
# Never pass this client or its key to the frontend.
#
# Carried over unchanged from the real-estate backend's audit-fixed version:
# a cached *async* client (create_async_client), built once behind an
# asyncio.Lock so concurrent early callers don't race to build two. Every
# call site does `db = await get_supabase()` then `await db.table(...).execute()`.

import asyncio
from typing import Optional

from supabase import create_async_client, AsyncClient
from app.core.config import get_settings

_client: Optional[AsyncClient] = None
_client_lock = asyncio.Lock()


async def get_supabase() -> AsyncClient:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            settings = get_settings()
            _client = await create_async_client(
                settings.supabase_url,
                settings.supabase_service_role_key,
            )
        return _client
