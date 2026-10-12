"""Mock order-status tools (deterministic, no external calls)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from agent_framework import tool

_ORDERS = {
    "12345": {"item": "Fiber router", "status": "shipped", "carrier": "FastShip", "tracking": "FS123456789", "eta": "2026-10-14"},
    "67890": {"item": "5G smartphone", "status": "processing", "carrier": None, "tracking": None, "eta": "2026-10-20"},
    "55555": {"item": "SIM card", "status": "delivered", "carrier": "FastShip", "tracking": "FS555555555", "eta": "2026-10-08"},
}


def _find(order_id: str) -> dict | None:
    digits = "".join(c for c in order_id if c.isdigit())
    return _ORDERS.get(digits)


@tool(approval_mode="never_require")
async def lookup_order(order_id: Annotated[str, "The order number, digits only"]) -> str:
    """Look up an order's item and current status."""
    await asyncio.sleep(0.3)
    o = _find(order_id)
    return f"Order {order_id}: {o['item']}, status {o['status']}." if o else f"No order found with number {order_id}."


@tool(approval_mode="never_require")
async def get_tracking(order_id: Annotated[str, "The order number, digits only"]) -> str:
    """Get carrier and tracking details for a shipped order."""
    await asyncio.sleep(0.4)
    o = _find(order_id)
    if not o:
        return f"No order found with number {order_id}."
    if not o["tracking"]:
        return f"Order {order_id} has not shipped yet, so there is no tracking number."
    return f"Order {order_id} is with {o['carrier']}, tracking number {o['tracking']}."


@tool(approval_mode="never_require")
async def get_delivery_eta(order_id: Annotated[str, "The order number, digits only"]) -> str:
    """Get the estimated delivery date for an order."""
    await asyncio.sleep(0.3)
    o = _find(order_id)
    if not o:
        return f"No order found with number {order_id}."
    return f"Order {order_id} is expected on {o['eta']}." if o["status"] != "delivered" else f"Order {order_id} was already delivered on {o['eta']}."


ORDER_TOOLS = [lookup_order, get_tracking, get_delivery_eta]
