"""Mock billing tools (deterministic, no external calls)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from agent_framework import tool

from calltrace import traced_tool

_INVOICES = {
    "current": {"period": "September 2026", "total": 84.90, "due": "2026-10-15", "status": "unpaid"},
    "previous": {"period": "August 2026", "total": 62.40, "due": "2026-09-15", "status": "paid"},
}
_CHARGES = {
    "roaming": "Roaming data used on your trip abroad: 12.50 USD.",
    "device": "Monthly installment for your phone: 15.00 USD.",
    "overage": "Data overage beyond your 20 GB plan: 7.00 USD.",
}


@tool(approval_mode="never_require")
@traced_tool
async def get_account_balance() -> str:
    """Get the customer's current account balance and next due date."""
    await asyncio.sleep(0.2)
    inv = _INVOICES["current"]
    return f"Current balance is {inv['total']:.2f} USD, due {inv['due']}. Status: {inv['status']}."


@tool(approval_mode="never_require")
@traced_tool
async def get_latest_invoice(
    period: Annotated[str, "Which invoice: 'current' or 'previous'"] = "current",
) -> str:
    """Get a summary of the customer's invoice for the current or previous period."""
    await asyncio.sleep(0.3)
    inv = _INVOICES.get(period.lower(), _INVOICES["current"])
    return f"Invoice for {inv['period']}: total {inv['total']:.2f} USD, due {inv['due']}, status {inv['status']}."


@tool(approval_mode="never_require")
@traced_tool
async def explain_charge(
    charge: Annotated[str, "Charge type to explain, e.g. 'roaming', 'device', 'overage'"],
) -> str:
    """Explain a specific charge on the customer's bill."""
    await asyncio.sleep(0.3)
    key = next((k for k in _CHARGES if k in charge.lower()), None)
    return _CHARGES[key] if key else f"No charge matching '{charge}' was found on the latest invoice."


@tool(approval_mode="never_require")
@traced_tool
async def get_payment_status() -> str:
    """Get the status of the customer's most recent payment."""
    await asyncio.sleep(0.2)
    return "Last payment of 62.40 USD was received on 2026-09-10 and has been applied to the account."


BILLING_TOOLS = [get_account_balance, get_latest_invoice, explain_charge, get_payment_status]
