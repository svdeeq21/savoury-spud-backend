# savoury-spud-backend/app/services/notifications.py

from app.core.config import get_settings
from app.services import whatsapp
from app.utils.logger import log

settings = get_settings()


async def notify_new_paid_order(order: dict, customer: dict) -> None:
    if not settings.admin_number_list:
        await log.warn("NO_ADMIN_NUMBER_CONFIGURED", ref_type="order", ref_id=order.get("id"))
        return

    items_summary = ", ".join(
        f"{i['quantity']}x {i['product_name']}" for i in order.get("items", [])
    ) or "(items unavailable)"

    text = (
        f"🔔 New paid order — ₦{order['subtotal']:,.2f} (food only)\n"
        f"{customer.get('name') or customer.get('phone_number')}\n"
        f"{items_summary}\n"
        f"Order ID: {order['id']}"
    )

    if order.get("fulfillment_method") == "DELIVERY":
        text += (
            f"\n\n🚴 DELIVERY to: {order.get('delivery_address') or '(no address on file)'}"
            + (f" ({order['delivery_area']})" if order.get("delivery_area") else "")
            + "\n⚠️ Reach out to confirm the delivery fee — not yet charged."
        )
    elif order.get("fulfillment_method") == "PICKUP":
        text += "\n\n🏠 PICKUP order."

    for number in settings.admin_number_list:
        try:
            await whatsapp.send_admin_alert(number, text)
        except Exception as e:
            await log.error("ADMIN_ALERT_FAILED", ref_type="order", ref_id=order.get("id"), metadata={"error": str(e)[:200]})
