"""Code-driven call orchestration: router -> billing/orders sub-agent -> router.

Routing is decided here from Decision-1 probabilities. The LLM only speaks and extracts
parameters. All per-conversation data lives in a JSON-serializable ``state`` dict (no module
globals), so it can be persisted by the host between ``/responses`` calls.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import prompts as P
from decision import (
    CHOICE_CONFIDENCE_THRESHOLD,
    TOPIC_BILLING,
    TOPIC_ORDERS,
    TOPIC_OTHER,
    TOPICS,
    format_transcript,
)

logger = logging.getLogger(__name__)

ROUTER = "router"
BILLING = "billing"
ORDERS = "orders"

AGENT_FOR_TOPIC = {TOPIC_BILLING: BILLING, TOPIC_ORDERS: ORDERS}
TOPIC_FOR_AGENT = {v: k for k, v in AGENT_FOR_TOPIC.items()}

OTHER_TOPIC_THRESHOLD = 0.6
RESOLVED_THRESHOLD = 0.7
MAX_HISTORY = 30
MAX_HANDOFFS_PER_CALL = 30
MAX_HANDOFFS_PER_TURN = 1

FALLBACK_REPLY = "Sorry, I had a problem on my side. Could you please say that again?"
EMPTY_INPUT_REPLY = "Sorry, I didn't catch that. Could you say it again?"


@dataclass
class TurnSignals:
    """Per-call signals raised by an agent's tools (e.g. ``return_to_router``)."""

    return_requested: bool = False


class AgentRunner(Protocol):
    """Runs one agent for one turn and streams its spoken text."""

    def stream(
        self, agent: str, history: list[dict[str, str]], directive: str | None, signals: TurnSignals
    ) -> AsyncIterator[str]: ...


def new_state() -> dict[str, Any]:
    return {
        "active_agent": ROUTER,
        "queue": [],
        "history": [],
        "greeted": False,
        "ended": False,
        "pending_return": False,
        "handoff_count": 0,
    }


def ensure_state(state: dict[str, Any]) -> dict[str, Any]:
    for key, value in new_state().items():
        state.setdefault(key, value)
    return state


@dataclass
class _Plan:
    agent: str
    directive: str | None = None
    ends_call: bool = False


@dataclass
class _Turn:
    handoffs: int = 0
    origin: str = ROUTER
    parts: list[str] = field(default_factory=list)


def _other(topic: str) -> str:
    return TOPIC_ORDERS if topic == TOPIC_BILLING else TOPIC_BILLING


def _label(topic: str) -> str:
    return P.TOPIC_LABEL.get(topic, topic)


class Orchestrator:
    def __init__(self, decision: Any, runner: AgentRunner) -> None:
        self._decision = decision
        self._runner = runner

    async def handle_turn(self, state: dict[str, Any], user_text: str) -> AsyncIterator[str]:
        """Process one customer utterance, yielding the reply text as it is produced."""
        ensure_state(state)
        user_text = (user_text or "").strip()
        if not user_text:
            yield EMPTY_INPUT_REPLY
            return

        if state["ended"]:
            state["ended"] = False
            state["active_agent"] = ROUTER
        state["history"].append({"role": "user", "text": user_text})
        turn = _Turn(origin=state["active_agent"])

        try:
            plan: _Plan | None = await self._plan(state, turn)
        except Exception:  # never crash the call because of routing
            logger.exception("Planning failed; staying with %s", state["active_agent"])
            plan = _Plan(state["active_agent"])

        signals = TurnSignals()
        while plan is not None:
            async for chunk in self._speak(state, turn, plan, signals):
                yield chunk
            if plan.ends_call:
                state["ended"] = True
            plan = None
            if signals.return_requested and state["active_agent"] != ROUTER and not state["ended"]:
                plan = self._after_return(state, turn)
                signals = TurnSignals()

        state["greeted"] = True
        reply = "".join(turn.parts).strip()
        if reply:
            state["history"].append({"role": "assistant", "text": reply})
        del state["history"][:-MAX_HISTORY]

    # ------------------------------------------------------------------ planning

    def _transcript(self, state: dict[str, Any]) -> str:
        return format_transcript([(m["role"], m["text"]) for m in state["history"]])

    def _handoff(self, state: dict[str, Any], turn: _Turn, target: str) -> bool:
        """Switch the active agent; refuses repeated/bouncing handoffs. Returns True if switched."""
        if (
            turn.handoffs >= MAX_HANDOFFS_PER_TURN
            or target == state["active_agent"]
            or target == turn.origin
            or state["handoff_count"] >= MAX_HANDOFFS_PER_CALL
        ):
            logger.info("Handoff to %s refused (active=%s)", target, state["active_agent"])
            return False
        logger.info("Handoff %s -> %s", state["active_agent"], target)
        state["active_agent"] = target
        state["handoff_count"] += 1
        turn.handoffs += 1
        return True

    async def _plan(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        if state["active_agent"] == ROUTER:
            return await self._plan_router(state, turn)
        return await self._plan_sub(state, turn)

    async def _plan_router(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        analysis = await self._decision.analyze_turn(self._transcript(state))
        topics = list(analysis.detected_topics) if analysis.ok else []
        logger.info(
            "Analysis: billing=%.2f orders=%.2f primary=%s end=%.2f ok=%s",
            analysis.billing, analysis.order_status, analysis.primary_topic, analysis.wants_to_end, analysis.ok,
        )

        if analysis.ok and analysis.confident_end:
            return _Plan(ROUTER, P.DIRECTIVE_GOODBYE, ends_call=True)

        if topics:
            first, rest = topics[0], topics[1:]
            state["queue"] = [t for t in state["queue"] if t != first]
            for t in rest:
                if t not in state["queue"]:
                    state["queue"].append(t)
            if self._handoff(state, turn, AGENT_FOR_TOPIC[first]):
                directive = P.DIRECTIVE_HANDOFF_FIRST
                if rest:
                    directive = P.DIRECTIVE_ALSO_RAISED.format(other=_label(rest[0])) + " " + directive
                return _Plan(AGENT_FOR_TOPIC[first], directive)

        if (
            analysis.ok
            and analysis.primary_topic == TOPIC_OTHER
            and analysis.primary_confidence >= CHOICE_CONFIDENCE_THRESHOLD
        ):
            return _Plan(ROUTER, P.DIRECTIVE_OUT_OF_SCOPE)
        if not state["greeted"]:
            return _Plan(ROUTER, P.DIRECTIVE_GREET)
        return _Plan(ROUTER, P.DIRECTIVE_CLARIFY)

    async def _plan_sub(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        active = state["active_agent"]
        topic = TOPIC_FOR_AGENT[active]
        other_p, resolved_p, ok = await self._decision.on_topic(self._transcript(state), topic)
        logger.info("Guard[%s]: other_topic=%.2f resolved=%.2f ok=%s", active, other_p, resolved_p, ok)

        resolved = bool(state["pending_return"])
        state["pending_return"] = False
        noted: str | None = None
        if ok:
            other = _other(topic)
            if other_p >= OTHER_TOPIC_THRESHOLD:
                if other not in state["queue"]:
                    state["queue"].append(other)
                    noted = other
            resolved = resolved or resolved_p >= RESOLVED_THRESHOLD

        if resolved:
            return await self._advance(state, turn)
        return _Plan(active, P.DIRECTIVE_NOTED_OTHER.format(other=_label(noted)) if noted else None)

    async def _advance(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        """The current topic is resolved: bridge to the next queued topic, or return to the router."""
        queued = self._next_queued(state, turn)
        if queued is not None:
            return queued
        active = state["active_agent"]
        if not self._handoff(state, turn, ROUTER):
            return _Plan(active)
        analysis = await self._decision.analyze_turn(self._transcript(state))
        if analysis.ok and analysis.confident_end:
            return _Plan(ROUTER, P.DIRECTIVE_GOODBYE, ends_call=True)
        return _Plan(ROUTER, P.DIRECTIVE_ANYTHING_ELSE)

    def _next_queued(self, state: dict[str, Any], turn: _Turn) -> _Plan | None:
        while state["queue"]:
            topic = state["queue"][0]
            if topic not in TOPICS:
                state["queue"].pop(0)
                continue
            target = AGENT_FOR_TOPIC[topic]
            if target == state["active_agent"]:
                state["queue"].pop(0)
                continue
            if not self._handoff(state, turn, target):
                return None
            state["queue"].pop(0)
            directive = P.DIRECTIVE_BRIDGE.format(topic=_label(topic), bridge=P.BRIDGE_LINE[topic])
            return _Plan(target, directive)
        return None

    def _after_return(self, state: dict[str, Any], turn: _Turn) -> _Plan | None:
        """The sub-agent called ``return_to_router`` after answering."""
        if turn.handoffs >= MAX_HANDOFFS_PER_TURN:
            state["pending_return"] = True
            return None
        queued = self._next_queued(state, turn)
        if queued is not None:
            return queued
        if self._handoff(state, turn, ROUTER):
            return _Plan(ROUTER, P.DIRECTIVE_ANYTHING_ELSE)
        state["pending_return"] = True
        return None

    # ------------------------------------------------------------------ speaking

    async def _speak(
        self, state: dict[str, Any], turn: _Turn, plan: _Plan, signals: TurnSignals
    ) -> AsyncIterator[str]:
        history = [dict(m) for m in state["history"]]
        if turn.parts:
            history.append({"role": "assistant", "text": "".join(turn.parts).strip()})
        needs_separator = bool(turn.parts)
        produced = False
        try:
            async for chunk in self._runner.stream(plan.agent, history, plan.directive, signals):
                if not chunk:
                    continue
                if needs_separator and not chunk[0].isspace():
                    chunk = " " + chunk
                needs_separator = False
                produced = True
                turn.parts.append(chunk)
                yield chunk
        except Exception:
            logger.exception("Agent %s failed", plan.agent)
            if not produced:
                turn.parts.append(FALLBACK_REPLY)
                yield FALLBACK_REPLY
