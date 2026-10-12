from __future__ import annotations

import asyncio

from decision import TOPIC_BILLING, TOPIC_ORDERS
from task_store import TaskStatus, TaskStore


async def fast(topic, params):
    return f"done-{topic}"


async def test_launch_and_collect_exactly_once():
    store = TaskStore(fast)
    t = store.launch("s", TOPIC_BILLING, {"invoice_period": "current"})
    assert store.active("s") == [t]
    await store.drain()
    assert t.status == TaskStatus.COMPLETED
    first = store.collect_completed("s")
    assert [x.result for x in first] == ["done-billing"]
    assert t.status == TaskStatus.DELIVERED
    assert store.collect_completed("s") == []


async def test_duplicate_guard_returns_existing_active_task():
    gate = asyncio.Event()

    async def slow(topic, params):
        await gate.wait()
        return "x"

    store = TaskStore(slow)
    a = store.launch("s", TOPIC_ORDERS, {"order_id": "1"})
    b = store.launch("s", TOPIC_ORDERS, {"order_id": "1"})
    c = store.launch("s", TOPIC_ORDERS, {"order_id": "2"})
    assert a is b and a is not c
    assert len(store.tasks("s")) == 2
    gate.set()
    await store.drain()
    # once finished, an identical request may run again
    assert store.launch("s", TOPIC_ORDERS, {"order_id": "1"}) is not a


async def test_failure_is_captured_and_delivered_once():
    async def boom(topic, params):
        raise RuntimeError("nope")

    store = TaskStore(boom)
    t = store.launch("s", TOPIC_BILLING, {})
    await store.drain()
    assert t.status == TaskStatus.FAILED and t.error == "nope"
    assert store.collect_completed("s") == [t]
    assert store.collect_completed("s") == []


async def test_cancel_stops_worker_and_is_never_delivered():
    gate = asyncio.Event()

    async def slow(topic, params):
        await gate.wait()

    store = TaskStore(slow)
    t = store.launch("s", TOPIC_BILLING, {})
    await asyncio.sleep(0)
    assert [x.id for x in store.cancel("s")] == [t.id]
    await store.drain()
    assert t.status == TaskStatus.CANCELLED
    assert store.collect_completed("s") == []


async def test_sessions_are_isolated():
    store = TaskStore(fast)
    store.launch("a", TOPIC_BILLING, {})
    store.launch("b", TOPIC_BILLING, {})
    await store.drain()
    assert len(store.collect_completed("a")) == 1
    assert len(store.collect_completed("b")) == 1
