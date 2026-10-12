from __future__ import annotations

from typing import Any

from agent_framework import Agent

from prompts import BILLING_WORKER_INSTRUCTIONS
from tools import BILLING_TOOLS


def build_billing_worker(client: Any) -> Agent:
    return Agent(
        client,
        BILLING_WORKER_INSTRUCTIONS,
        name="billing-worker",
        tools=BILLING_TOOLS,
        default_options={"store": False},
    )


def billing_prompt(params: dict[str, Any]) -> str:
    period = params.get("invoice_period") or "current"
    question = params.get("question") or "give an overview of the latest bill"
    return f"Invoice period: {period}. Customer wants to know: {question}."
