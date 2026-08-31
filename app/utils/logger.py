# savoury-spud-backend/app/utils/logger.py
#
# Structured logger that writes to two places simultaneously:
#   1. stdout        (for platform log streaming)
#   2. audit_logs table in Supabase (for dashboard observability)
#
# Usage:
#   from app.utils.logger import log
#   await log.info("ORDER_PAID", ref_type="order", ref_id=order_id, metadata={"total": 4500})
#
# Adapted from the real-estate backend's AuditLogger: same shape, but
# ref_type/ref_id instead of a hardcoded lead_id column, since events here
# point at orders, customers, or nothing in particular (a webhook parse
# failure has no natural "owner" row).

import logging
import sys
from uuid import UUID
from typing import Optional
from app.core.supabase import get_supabase

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
_logger = logging.getLogger("savoury-spud")


class AuditLogger:
    async def _write(
        self,
        event_type: str,
        severity: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[UUID] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        # Always log to stdout first — if the DB write fails we still have logs.
        msg = f"[{event_type}]"
        if ref_type and ref_id:
            msg += f" {ref_type}={ref_id}"
        if metadata:
            msg += f" {metadata}"

        level = getattr(logging, severity, logging.INFO)
        _logger.log(level, msg)

        # Best-effort write to Supabase audit_logs — never raises, since a
        # logging failure should never be the thing that breaks an order.
        try:
            db = await get_supabase()
            payload = {
                "event":    event_type,
                "severity": severity,
                "metadata": metadata or {},
            }
            if ref_type:
                payload["ref_type"] = ref_type
            if ref_id:
                payload["ref_id"] = str(ref_id)

            await db.table("audit_logs").insert(payload).execute()
        except Exception as e:
            _logger.warning(f"Failed to write audit log to DB: {e}")

    async def debug(self, event_type: str, ref_type: Optional[str] = None, ref_id: Optional[UUID] = None, metadata: Optional[dict] = None):
        await self._write(event_type, "DEBUG", ref_type, ref_id, metadata)

    async def info(self, event_type: str, ref_type: Optional[str] = None, ref_id: Optional[UUID] = None, metadata: Optional[dict] = None):
        await self._write(event_type, "INFO", ref_type, ref_id, metadata)

    async def warn(self, event_type: str, ref_type: Optional[str] = None, ref_id: Optional[UUID] = None, metadata: Optional[dict] = None):
        await self._write(event_type, "WARN", ref_type, ref_id, metadata)

    async def error(self, event_type: str, ref_type: Optional[str] = None, ref_id: Optional[UUID] = None, metadata: Optional[dict] = None):
        await self._write(event_type, "ERROR", ref_type, ref_id, metadata)

    async def critical(self, event_type: str, ref_type: Optional[str] = None, ref_id: Optional[UUID] = None, metadata: Optional[dict] = None):
        await self._write(event_type, "CRITICAL", ref_type, ref_id, metadata)


log = AuditLogger()
