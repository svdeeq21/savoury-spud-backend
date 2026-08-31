"""
Shared test setup.

Required settings with no defaults (supabase_url, gemini_api_key, etc.) get
harmless placeholder values before any app module is imported, since several
modules build a client at import time. No test in this suite makes a real
network call — pure functions are tested directly, and anything touching
"the database" uses the FakeSupabase fixtures below.
"""
import os
from datetime import datetime, timezone

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("EVOLUTION_API_URL", "https://test-evolution.example.com")
os.environ.setdefault("EVOLUTION_API_KEY", "test-evolution-key")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")

import pytest


class FakeQueryBuilder:
    """Minimal stand-in for supabase-py's AsyncQueryRequestBuilder — enough
    chainable .table().select().eq()....execute() surface for these tests."""

    def __init__(self, store: "FakeSupabase", table_name: str):
        self._store = store
        self._table = table_name
        self._filters: list[tuple[str, str, object]] = []
        self._op = None
        self._payload = None
        self._limit = None
        self._order = None
        self._single = False
        self._on_conflict = None

    def select(self, cols="*"):
        self._op = self._op or "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, "eq", val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, "in", vals))
        return self

    def gte(self, col, val):
        self._filters.append((col, "gte", val))
        return self

    def lte(self, col, val):
        self._filters.append((col, "lte", val))
        return self

    def lt(self, col, val):
        self._filters.append((col, "lt", val))
        return self

    def ilike(self, col, pattern):
        self._filters.append((col, "ilike", pattern.strip("%").lower()))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._limit = ("range", start, end)
        return self

    def single(self):
        self._single = True
        return self

    def _matches(self, row):
        for col, op, val in self._filters:
            if op == "eq" and row.get(col) != val:
                return False
            if op == "in" and row.get(col) not in val:
                return False
            if op == "gte" and (row.get(col) is None or str(row.get(col)) < str(val)):
                return False
            if op == "lte" and (row.get(col) is None or str(row.get(col)) > str(val)):
                return False
            if op == "lt" and (row.get(col) is None or str(row.get(col)) >= str(val)):
                return False
            if op == "ilike" and val not in str(row.get(col, "")).lower():
                return False
        return True

    async def execute(self):
        table = self._store.tables.setdefault(self._table, [])

        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted = []
            for r in rows:
                row = dict(r)
                row.setdefault("id", self._store.next_id())
                # Mimic Postgres's `created_at timestamptz default now()` — every table in
                # the real schema has this default, and app code (correctly) relies on it
                # being populated even when it doesn't set created_at explicitly itself.
                row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                if self._store.has_unique_constraint(self._table, row):
                    raise Exception(f"duplicate key violates unique constraint on {self._table}")
                table.append(row)
                inserted.append(row)
            return FakeResult(inserted)

        if self._op in ("update", "upsert"):
            updated = []
            matched_any = False
            for row in table:
                if self._matches(row):
                    row.update(self._payload)
                    updated.append(dict(row))
                    matched_any = True
            if self._op == "upsert" and not matched_any:
                new_row = dict(self._payload)
                new_row.setdefault("id", self._store.next_id())
                table.append(new_row)
                updated.append(new_row)
            return FakeResult(updated)

        if self._op == "delete":
            remaining = [r for r in table if not self._matches(r)]
            removed = [r for r in table if self._matches(r)]
            table[:] = remaining
            return FakeResult(removed)

        # select
        rows = [dict(r) for r in table if self._matches(r)]
        if self._order:
            col, desc = self._order
            rows.sort(key=lambda r: r.get(col) or "", reverse=desc)
        if isinstance(self._limit, tuple) and self._limit[0] == "range":
            _, start, end = self._limit
            rows = rows[start: end + 1]
        elif isinstance(self._limit, int):
            rows = rows[: self._limit]
        if self._single:
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows, count=len(rows))


class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeSupabase:
    """Fake async Supabase client backed by in-memory dict-of-lists. Simple
    unique-constraint emulation for the tables that need it in tests
    (payments.reference, conversation_messages.wa_message_id)."""

    _UNIQUE_COLS = {
        "payments": ["reference"],
        "conversation_messages": ["org_id", "wa_message_id"],
    }

    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self._id_counter = 0

    def next_id(self) -> str:
        self._id_counter += 1
        return f"fake-id-{self._id_counter}"

    def has_unique_constraint(self, table_name: str, new_row: dict) -> bool:
        cols = self._UNIQUE_COLS.get(table_name)
        if not cols:
            return False
        existing = self.tables.get(table_name, [])
        for row in existing:
            if all(row.get(c) == new_row.get(c) for c in cols) and new_row.get(cols[-1]) is not None:
                return True
        return False

    def table(self, name):
        return FakeQueryBuilder(self, name)


@pytest.fixture
def fake_db():
    return FakeSupabase()
