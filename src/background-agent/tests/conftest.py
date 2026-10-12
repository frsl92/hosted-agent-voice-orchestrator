from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from decision import TurnAnalysis
from foreground import Foreground, new_state
from llm import Extraction
from task_store import TaskStore


class FakeDecision:
    """Scripted Decision-1: queue analyses / confirmations per call."""

    def __init__(self) -> None:
        self.analyses: list[TurnAnalysis] = []
        self.confirmations: list[str] = []
        self.analyze_calls = 0
        self.confirm_calls = 0

    async def analyze_turn(self, transcript: str) -> TurnAnalysis:
        self.analyze_calls += 1
        return self.analyses.pop(0) if self.analyses else TurnAnalysis()

    async def confirmation(self, transcript: str) -> str:
        self.confirm_calls += 1
        return self.confirmations.pop(0) if self.confirmations else "neither"


class FakeLLM:
    def __init__(self) -> None:
        self.extraction = Extraction(order_id="12345", billing_question="why it is high")
        self.said: list[str] = []
        self.fail = False

    async def say(self, instruction: str, history: list[dict[str, str]]) -> str:
        if self.fail:
            raise RuntimeError("llm down")
        self.said.append(instruction)
        return f"SAY[{instruction[:40]}]"

    async def extract(self, history: list[dict[str, str]]) -> Extraction:
        return self.extraction


def analysis(billing=0.0, orders=0.0, primary="unclear", end=0.0, ok=True) -> TurnAnalysis:
    return TurnAnalysis(billing=billing, order_status=orders, primary_topic=primary, wants_to_end=end, ok=ok)


class Runner:
    """Worker runner double: records starts; optional delay / failure."""

    def __init__(self, delay: float = 0.0, fail_topics: tuple[str, ...] = ()) -> None:
        self.delay = delay
        self.fail_topics = fail_topics
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, topic: str, params: dict[str, Any]) -> str:
        self.started.append((topic, dict(params)))
        if self.gate:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if topic in self.fail_topics:
            raise RuntimeError("boom")
        return f"result-{topic}"


class Harness:
    def __init__(self, runner: Runner | None = None, **kw: Any) -> None:
        self.decision = FakeDecision()
        self.llm = FakeLLM()
        self.runner = runner or Runner()
        self.store = TaskStore(self.runner)
        self.fg = Foreground(self.decision, self.llm, self.store, poll_interval=0.01, **kw)
        self.state = new_state()
        self.key = "s1"

    async def say(self, text: str) -> list[str]:
        return [c async for c in self.fg.handle(text, self.state, self.key)]

    def assert_serializable(self) -> None:
        json.dumps(self.state)


@pytest.fixture
def h() -> Harness:
    return Harness()


Factory = Callable[..., Harness]
