# savoury-spud-backend/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import webhook, dashboard, health

settings = get_settings()

app = FastAPI(title="Savoury Spud Ordering", version="0.1.0")

# Wide open outside production for local dev; in production, only the
# origins listed in DASHBOARD_ALLOWED_ORIGINS (comma-separated) may call
# /dashboard/* from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else settings.dashboard_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(dashboard.router)
