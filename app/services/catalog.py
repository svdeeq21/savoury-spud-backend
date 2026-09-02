# savoury-spud-backend/app/services/catalog.py
#
# All reads/writes against products, modifier_groups, modifiers, and
# categories. Availability *filtering* lives in availability.py (pure
# logic) — this module is purely the I/O boundary.

from __future__ import annotations
import json
from typing import Optional
from uuid import UUID

from app.core.supabase import get_supabase
from app.utils.distributed_state import cache_get, cache_set, cache_delete
from app.utils.logger import log

_CATALOG_CACHE_TTL = 300  # 5 minutes — long enough to matter, short enough that
                          # a menu edit in the dashboard shows up on WhatsApp soon after


def _catalog_cache_key(org_id: str) -> str:
    return f"catalog:{org_id}"


async def get_full_catalog(org_id: UUID, include_unavailable: bool = False) -> list[dict]:
    """
    Returns every product with its modifier groups and modifiers nested in,
    already excluding unavailable products/modifiers unless include_unavailable
    is True (dashboard editing needs to see everything; the ordering flow
    should never even be told a sold-out item exists).
    """
    cache_key = _catalog_cache_key(str(org_id))
    if not include_unavailable:
        cached = await cache_get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except Exception:
                pass

    db = await get_supabase()

    products_result = (
        await db.table("products")
        .select("id, category_id, name, description, base_price, available, sort_order")
        .eq("org_id", str(org_id))
        .order("sort_order")
        .execute()
    )
    products = products_result.data or []
    if not include_unavailable:
        products = [p for p in products if p.get("available")]

    if not products:
        return []

    product_ids = [p["id"] for p in products]

    links_result = (
        await db.table("product_modifier_groups")
        .select("product_id, modifier_group_id")
        .in_("product_id", product_ids)
        .execute()
    )
    links = links_result.data or []
    group_ids = list({l["modifier_group_id"] for l in links})

    groups_by_id: dict[str, dict] = {}
    if group_ids:
        groups_result = (
            await db.table("modifier_groups")
            .select("id, name, selection_type, required, sort_order")
            .in_("id", group_ids)
            .order("sort_order")
            .execute()
        )
        for g in (groups_result.data or []):
            g["modifiers"] = []
            groups_by_id[g["id"]] = g

        modifiers_result = (
            await db.table("modifiers")
            .select("id, group_id, name, price, available, sort_order")
            .in_("group_id", group_ids)
            .order("sort_order")
            .execute()
        )
        for m in (modifiers_result.data or []):
            if not include_unavailable and not m.get("available"):
                continue
            group = groups_by_id.get(m["group_id"])
            if group is not None:
                group["modifiers"].append(m)

    product_group_ids: dict[str, list[str]] = {}
    for l in links:
        product_group_ids.setdefault(l["product_id"], []).append(l["modifier_group_id"])

    for p in products:
        p["modifier_groups"] = [
            groups_by_id[gid] for gid in product_group_ids.get(p["id"], []) if gid in groups_by_id
        ]

    if not include_unavailable:
        await cache_set(cache_key, json.dumps(products, default=str), _CATALOG_CACHE_TTL)

    return products


async def invalidate_catalog_cache(org_id: UUID) -> None:
    await cache_delete(_catalog_cache_key(str(org_id)))


async def get_category_names(org_id: UUID) -> dict[str, str]:
    """category_id -> name, for grouping products into WhatsApp list sections.
    Kept separate from get_full_catalog rather than joined into it — nothing
    else needs the name (products are matched by name/id elsewhere), and this
    keeps that function's cached shape unchanged for existing callers."""
    db = await get_supabase()
    result = (
        await db.table("categories")
        .select("id, name, sort_order")
        .eq("org_id", str(org_id))
        .order("sort_order")
        .execute()
    )
    return {c["id"]: c["name"] for c in (result.data or [])}


async def get_product(product_id: UUID) -> Optional[dict]:
    db = await get_supabase()
    result = (
        await db.table("products")
        .select("id, org_id, category_id, name, description, base_price, available")
        .eq("id", str(product_id))
        .single()
        .execute()
    )
    return result.data


async def get_modifiers_by_ids(modifier_ids: list[UUID]) -> list[dict]:
    if not modifier_ids:
        return []
    db = await get_supabase()
    result = (
        await db.table("modifiers")
        .select("id, group_id, name, price, available")
        .in_("id", [str(m) for m in modifier_ids])
        .execute()
    )
    return result.data or []


async def find_product_by_name(org_id: UUID, name_fragment: str) -> Optional[dict]:
    """
    Case-insensitive partial match — used by admin_commands.py to resolve
    "chicken sold out" to the actual Chicken modifier/product without the
    owner having to type an exact, fully-cased name.
    """
    db = await get_supabase()
    result = (
        await db.table("products")
        .select("id, name, available")
        .eq("org_id", str(org_id))
        .ilike("name", f"%{name_fragment}%")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


async def find_modifier_by_name(org_id: UUID, name_fragment: str) -> Optional[dict]:
    db = await get_supabase()
    groups_result = await db.table("modifier_groups").select("id").eq("org_id", str(org_id)).execute()
    group_ids = [g["id"] for g in (groups_result.data or [])]
    if not group_ids:
        return None
    result = (
        await db.table("modifiers")
        .select("id, group_id, name, available")
        .in_("group_id", group_ids)
        .ilike("name", f"%{name_fragment}%")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


async def set_product_availability(product_id: UUID, available: bool, org_id: UUID) -> None:
    db = await get_supabase()
    await db.table("products").update({"available": available}).eq("id", str(product_id)).execute()
    await invalidate_catalog_cache(org_id)
    await log.info("PRODUCT_AVAILABILITY_CHANGED", ref_type="product", ref_id=product_id, metadata={"available": available})


async def set_modifier_availability(modifier_id: UUID, available: bool, org_id: UUID) -> None:
    db = await get_supabase()
    await db.table("modifiers").update({"available": available}).eq("id", str(modifier_id)).execute()
    await invalidate_catalog_cache(org_id)
    await log.info("MODIFIER_AVAILABILITY_CHANGED", ref_type="modifier", ref_id=modifier_id, metadata={"available": available})


async def create_product(org_id: UUID, data: dict) -> dict:
    db = await get_supabase()
    payload = {**data, "org_id": str(org_id)}
    result = await db.table("products").insert(payload).execute()
    await invalidate_catalog_cache(org_id)
    return (result.data or [{}])[0]


async def update_product(product_id: UUID, org_id: UUID, patch: dict) -> Optional[dict]:
    db = await get_supabase()
    result = (
        await db.table("products")
        .update({k: v for k, v in patch.items() if v is not None})
        .eq("id", str(product_id))
        .execute()
    )
    await invalidate_catalog_cache(org_id)
    rows = result.data or []
    return rows[0] if rows else None
