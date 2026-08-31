# savoury-spud-backend/app/models/schemas.py
#
# All Pydantic models used across routers and services.

from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime, time
from uuid import UUID


# ── WhatsApp webhook payload (Evolution API → FastAPI) ────────────
# Same shape/quirks as the real-estate backend: `data` is sometimes a list
# (batch delivery), some events are missing `key`/`messageType` entirely.

class WAMessageData(BaseModel):
    key:         Optional[dict] = None
    message:     Optional[dict] = None
    messageType: Optional[str]  = None
    pushName:    Optional[str]  = None
    status:      Optional[str]  = None


class WAWebhookPayload(BaseModel):
    event:    str
    instance: str
    data:     WAMessageData

    @field_validator("data", mode="before")
    @classmethod
    def unwrap_data_list(cls, v):
        if isinstance(v, list):
            return v[0] if v else {}
        return v


# ── Catalog ─────────────────────────────────────────────────────

class ModifierOut(BaseModel):
    id:        UUID
    name:      str
    price:     float
    available: bool


class ModifierGroupOut(BaseModel):
    id:             UUID
    name:           str
    selection_type: Literal["single", "multiple"]
    required:       bool
    max_selections: Optional[int] = None  # null = unlimited (Extras); otherwise the free-included cap
    modifiers:      list[ModifierOut] = Field(default_factory=list)


class ProductOut(BaseModel):
    id:              UUID
    name:            str
    description:     Optional[str] = None
    base_price:      float
    available:       bool
    category_id:     Optional[UUID] = None
    modifier_groups: list[ModifierGroupOut] = Field(default_factory=list)


class ProductIn(BaseModel):
    name:        str
    description: Optional[str] = None
    base_price:  float = Field(ge=0)
    category_id: Optional[UUID] = None
    available:   bool = True


class ProductPatch(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    base_price:  Optional[float] = Field(default=None, ge=0)
    available:   Optional[bool] = None


class ModifierPatch(BaseModel):
    name:      Optional[str] = None
    price:     Optional[float] = Field(default=None, ge=0)
    available: Optional[bool] = None


# ── Availability ────────────────────────────────────────────────

class AvailabilityStatusOut(BaseModel):
    status:        Literal["OPEN", "CLOSED", "PAUSED"]
    pause_reason:  Optional[str] = None
    pause_message: Optional[str] = None
    is_accepting_orders: bool   # resolved status × operating hours, what the bot actually checks
    closed_reason: Optional[str] = None  # human-readable, sent to the customer when blocked


class AvailabilityPatch(BaseModel):
    status:        Literal["OPEN", "CLOSED", "PAUSED"]
    pause_reason:  Optional[str] = None
    pause_message: Optional[str] = None


class OperatingHoursOut(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    open_time:   Optional[time] = None
    close_time:  Optional[time] = None
    is_closed:   bool


class OperatingHoursPatch(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    open_time:   Optional[time] = None
    close_time:  Optional[time] = None
    is_closed:   bool = False


# ── Fulfillment (pickup / delivery) ────────────────────────────

class FulfillmentIn(BaseModel):
    method: Literal["PICKUP", "DELIVERY"]
    # Delivery-only fields — validated as a group in orders.set_fulfillment_details
    # rather than here, since which ones are required depends on `method`.
    delivery_address:  Optional[str] = None
    delivery_area:     Optional[str] = None
    delivery_landmark: Optional[str] = None
    time_preference:   Literal["ASAP", "SCHEDULED"] = "ASAP"
    scheduled_for:      Optional[datetime] = None


class DeliveryFeeIn(BaseModel):
    delivery_fee: float = Field(ge=0)


# ── Cart / order ────────────────────────────────────────────────

class CartItemModifierIn(BaseModel):
    modifier_id: UUID


class AddProductIn(BaseModel):
    product_id: UUID
    quantity:   int = Field(default=1, ge=1)
    modifier_ids: list[UUID] = Field(default_factory=list)


class SetQuantityIn(BaseModel):
    order_item_id: UUID
    quantity:       int = Field(ge=1)


class OrderItemOut(BaseModel):
    id:           UUID
    product_id:   Optional[UUID]
    product_name: str
    base_price:   float
    quantity:     int
    line_total:   float
    modifiers:    list[dict] = Field(default_factory=list)


class OrderOut(BaseModel):
    id:               UUID
    status:           str
    subtotal:         float
    delivery_fee:     float
    total:            float
    delivery_address: Optional[str] = None
    customer_notes:   Optional[str] = None
    fulfillment_method:         Optional[Literal["PICKUP", "DELIVERY"]] = None
    delivery_area:              Optional[str] = None
    delivery_landmark:          Optional[str] = None
    fulfillment_time_preference: Optional[Literal["ASAP", "SCHEDULED"]] = None
    scheduled_for:               Optional[datetime] = None
    delivery_fee_confirmed:      bool = True
    items:            list[OrderItemOut] = Field(default_factory=list)
    created_at:       datetime
    updated_at:       datetime
    paid_at:          Optional[datetime] = None
    customer_id:      Optional[UUID] = None
    customer_name:    Optional[str] = None
    customer_phone:   Optional[str] = None


class OrderStatusPatch(BaseModel):
    status: Literal["PREPARING", "READY", "COMPLETED", "CANCELLED"]


# ── Payments ────────────────────────────────────────────────────

class PaystackInitResult(BaseModel):
    authorization_url: str
    access_code:       str
    reference:          str


# ── Admin commands (parsed from a WhatsApp message sent by the owner) ──

class AdminCommand(BaseModel):
    type: Literal[
        "pause_orders",
        "resume_orders",
        "set_item_availability",
        "status_report",
        "unknown",
    ]
    item_name: Optional[str] = None
    available: Optional[bool] = None
    reason:    Optional[str] = None
    raw_text:  str


# ── Customer ────────────────────────────────────────────────────

class CustomerOut(BaseModel):
    id:             UUID
    name:           Optional[str] = None
    phone_number:   str
    total_orders:   int
    last_order_at:  Optional[datetime] = None
    created_at:     datetime
