from __future__ import annotations

from typing import Any

from agent_framework import Agent

from prompts import ORDERS_WORKER_INSTRUCTIONS
from tools import ORDER_TOOLS


def build_orders_worker(client: Any) -> Agent:
    return Agent(
        client,
        ORDERS_WORKER_INSTRUCTIONS,
        name="orders-worker",
        tools=ORDER_TOOLS,
        default_options={"store": False},
    )


def orders_prompt(params: dict[str, Any]) -> str:
    question = params.get("question") or "onde está a encomenda e quando vai chegar"
    return f"Order number: {params.get('order_id', '')}. Customer wants to know: {question}."
