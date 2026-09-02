# savoury-spud-backend/app/core/config.py
#
# Settings pulled straight from env vars (.env locally, real env vars in
# deployment). Anything without a default is required — a missing key
# should fail loudly at startup, not fall back to something that quietly
# doesn't work.

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


def _require_url_scheme(value: str, field_name: str) -> str:
    """
    Strips a trailing slash and enforces http(s):// is present. Anything
    built from this value (whatsapp.py, paystack.py, supabase.py) assumes
    a fully-qualified URL — without this check, a bare domain pasted into
    an env var (a one-character typo: forgetting "https://") doesn't fail
    until the first real request tries to use it, inside a background
    task, invisible to whoever's testing.
    """
    value = value.strip()
    if value and not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError(
            f"{field_name} must start with http:// or https:// — got {value!r}. "
            f"Likely a bare domain was pasted without the scheme."
        )
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Supabase ─────────────────────────────────────────────────
    supabase_url:              str
    supabase_service_role_key: str

    # ── Gemini (ordering conversation LLM) ───────────────────────
    gemini_api_key: str
    gemini_model:   str = "gemini-2.0-flash"

    # ── Evolution API (WhatsApp) ──────────────────────────────────
    evolution_api_url:       str
    evolution_api_key:       str
    evolution_instance_name: str = "savoury-spud-bot"
    webhook_secret:          str = ""

    # ── Paystack ───────────────────────────────────────────────────
    paystack_secret_key: str
    paystack_public_key: str = ""
    # Where Paystack redirects the customer's browser after they pay — only
    # matters if you ever open the hosted checkout page directly rather than
    # relying purely on the webhook. Safe to leave blank for WhatsApp-only.
    paystack_callback_url: str = ""

    # ── Admin ────────────────────────────────────────────────────
    # Dashboard auth — same single-shared-secret pattern as the real-estate
    # CRM (X-Admin-Key header). Upgrade to real login only once a second
    # staff account is actually needed.
    admin_api_key: str = ""

    # The owner's WhatsApp number(s). Messages from these numbers are routed
    # to the admin-command handler instead of the ordering flow. Comma
    # separated if there's more than one (e.g. owner + a manager).
    admin_whatsapp_numbers: str = ""

    # ── Org ──────────────────────────────────────────────────────
    # Single-tenant today (see migration notes) — this just picks which
    # organizations row this deployment answers for.
    org_slug: str = "savoury-spud"

    # Optional: shown to a customer who taps the 5-star feedback option
    # (see message_pipeline._handle_feedback_rating). Leave blank to just
    # thank them without a review link — nothing breaks either way.
    google_review_url: str = ""

    # operating_hours.open_time/close_time are stored as plain local-time
    # values with no timezone attached — this is what turns "now" (always
    # fetched in UTC) into the business's local clock time before comparing
    # against them. Defaults to WAT (Africa/Lagos, UTC+1, no DST).
    business_utc_offset_hours: float = 1.0

    # How long an abandoned CART (never reached checkout) is resumed for
    # before a returning customer gets a fresh one instead. Not a short
    # session timeout — it's meant to catch "came back two days later to
    # order something else", not "took a bathroom break mid-order".
    cart_stale_after_hours: float = 4.0

    # How many recent messages (both sides) are included as short-term
    # context for the ordering LLM. Keeps it a bounded, recent window —
    # not a full transcript — so prompts stay small and relevant.
    conversation_history_turns: int = 8

    # ── App ──────────────────────────────────────────────────────
    app_env:   str = "production"
    log_level: str = "INFO"
    currency:  str = "NGN"

    # Comma-separated list of origins allowed to call /dashboard/* from a
    # browser (the dashboard frontend's deployed URL, e.g. Vercel). Required
    # in production — CORS has nothing to allow without it.
    dashboard_allowed_origins: str = ""

    # ── Distributed lock / cache (Upstash Redis) ──────────────────
    # Optional — falls back to in-process locking/caching if unset, same
    # trade-off documented in app/utils/distributed_state.py.
    upstash_redis_rest_url:   str = ""
    upstash_redis_rest_token: str = ""

    @field_validator("evolution_api_url")
    @classmethod
    def _validate_evolution_api_url(cls, v: str) -> str:
        return _require_url_scheme(v, "EVOLUTION_API_URL")

    @field_validator("supabase_url")
    @classmethod
    def _validate_supabase_url(cls, v: str) -> str:
        return _require_url_scheme(v, "SUPABASE_URL")

    @field_validator("upstash_redis_rest_url")
    @classmethod
    def _validate_upstash_url(cls, v: str) -> str:
        return _require_url_scheme(v, "UPSTASH_REDIS_REST_URL") if v else v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def dashboard_origin_list(self) -> list[str]:
        return [o.strip() for o in self.dashboard_allowed_origins.split(",") if o.strip()]

    @property
    def admin_number_list(self) -> list[str]:
        return [n.strip() for n in self.admin_whatsapp_numbers.split(",") if n.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
