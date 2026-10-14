from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from decision import TOPIC_BILLING, TOPIC_ORDERS
from remote_workers import RemoteWorkerRunner


class FakeResponses:
    def __init__(self, agent: str, log: list, fail_with_session: set[str]) -> None:
        self.agent, self.log, self.fail_with_session = agent, log, fail_with_session
        self.count = 0

    async def create(self, **kwargs: Any) -> Any:
        self.log.append((self.agent, kwargs))
        sid = (kwargs.get("extra_body") or {}).get("agent_session_id")
        if sid in self.fail_with_session:
            raise RuntimeError("session not found")
        await asyncio.sleep(0)
        self.count += 1
        return NS(
            id=f"resp-{self.agent}-{self.count}",
            error=None,
            model_extra={"agent_session_id": sid or f"sess-{self.agent}-{len(self.log)}"},
            output=[
                NS(type="function_call", call_id="c1", name="lookup_order", arguments='{"order_id": "1"}'),
                NS(type="function_call_output", call_id="c1", output="Order 1: shipped"),
            ],
            output_text=f"answer from {self.agent}",
            usage=NS(input_tokens=10, output_tokens=5),
        )


def make_runner(fail_with_session: set[str] | None = None):
    log: list = []
    clients: dict[str, Any] = {}

    def openai_for(name: str) -> Any:
        return clients.setdefault(name, NS(responses=FakeResponses(name, log, fail_with_session or set())))

    return RemoteWorkerRunner(openai_for), log


async def test_first_call_creates_a_session_and_later_calls_reuse_it():
    runner, log = make_runner()
    assert await runner(TOPIC_ORDERS, {"order_id": "1"}, "conv") == "answer from orders-agent"
    first = log[0][1]
    assert "previous_response_id" not in first and "extra_body" not in first
    assert "background lookup" in first["input"] and "Order number: 1" in first["input"]

    await runner(TOPIC_ORDERS, {"order_id": "2"}, "conv")
    second = log[1][1]
    assert second["previous_response_id"] == "resp-orders-agent-1"
    assert second["extra_body"] == {"agent_session_id": "sess-orders-agent-1"}


async def test_sessions_are_per_conversation_and_per_topic():
    runner, log = make_runner()
    await runner(TOPIC_ORDERS, {"order_id": "1"}, "a")
    await runner(TOPIC_ORDERS, {"order_id": "1"}, "b")
    await runner(TOPIC_BILLING, {}, "a")
    assert all("extra_body" not in kw for _, kw in log)
    assert [agent for agent, _ in log] == ["orders-agent", "orders-agent", "billing-agent"]


async def test_expired_session_is_replaced_with_a_fresh_one():
    runner, log = make_runner(fail_with_session={"sess-orders-agent-1"})
    await runner(TOPIC_ORDERS, {"order_id": "1"}, "conv")
    assert await runner(TOPIC_ORDERS, {"order_id": "2"}, "conv") == "answer from orders-agent"
    retry = log[-1][1]
    assert "previous_response_id" not in retry and "extra_body" not in retry


async def test_concurrent_tasks_of_one_topic_share_the_session_in_order():
    runner, log = make_runner()
    await asyncio.gather(runner(TOPIC_ORDERS, {"order_id": "1"}, "c"), runner(TOPIC_ORDERS, {"order_id": "2"}, "c"))
    assert "previous_response_id" not in log[0][1]
    assert log[1][1]["previous_response_id"] == "resp-orders-agent-1"


async def test_unknown_topic_and_tool_trace(caplog):
    runner, _ = make_runner()
    with pytest.raises(ValueError):
        await runner("nope", {}, "c")
    with caplog.at_level("INFO", logger="calltrace"):
        await runner(TOPIC_ORDERS, {"order_id": "1"}, "c")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert '"kind": "tool.call"' in text and '"remote": "orders-agent"' in text and '"kind": "usage"' in text
