"""Background workers: one small MAF Agent per topic."""

from __future__ import annotations

from typing import Any

from agent_framework import Agent

from decision import TOPIC_BILLING, TOPIC_ORDERS
from task_store import WorkerRunner
from workers.billing import build_billing_worker, billing_prompt
from workers.orders import build_orders_worker, orders_prompt

__all__ = ["build_worker_runner"]


def build_worker_runner(client: Any) -> WorkerRunner:
    """Return ``async (topic, params) -> speakable summary`` backed by per-topic agents."""
    agents: dict[str, Agent] = {}

    async def run(topic: str, params: dict[str, Any]) -> str:
        if topic == TOPIC_BILLING:
            agent = agents.setdefault(topic, build_billing_worker(client))
            prompt = billing_prompt(params)
        elif topic == TOPIC_ORDERS:
            agent = agents.setdefault(topic, build_orders_worker(client))
            prompt = orders_prompt(params)
        else:
            raise ValueError(f"unknown topic {topic!r}")
        response = await agent.run(prompt)
        return response.text

    return run
