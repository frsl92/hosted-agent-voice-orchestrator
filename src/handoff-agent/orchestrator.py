"""Code-driven call orchestration: router -> billing/orders sub-agent -> router.

Routing is decided here from Decision-1 probabilities. The LLM only speaks and extracts
parameters. All per-conversation data lives in a JSON-serializable ``state`` dict (no module
globals), so it can be persisted by the host between ``/responses`` calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import lang
import prompts as P
from calltrace import elapsed_ms, emit
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

IDLE, PROPOSING = "idle", "proposing"
OTHER_TOPIC_THRESHOLD = 0.6
RESOLVED_THRESHOLD = 0.7
MAX_HISTORY = 30
MAX_HANDOFFS_PER_CALL = 30
MAX_HANDOFFS_PER_TURN = 1
MAX_REASKS = 2
# Blank line between agents: the web app shows each agent's answer as its own message. It goes *after* a
# segment so the text-to-speech engine sees the sentence end immediately.
SEGMENT_BREAK = "\n\n"

FALLBACK_REPLY = "fallback"
EMPTY_INPUT_REPLY = "empty_input"
GREETING_CUE = "(The customer has just connected to the call.)"


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
        "phase": IDLE,
        "proposed": [],
        "reasks": 0,
        "lang": lang.DEFAULT,
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
    # A confirmed request runs every topic in one turn, so it gets a bigger budget and may return to the router.
    max_handoffs: int = MAX_HANDOFFS_PER_TURN
    # Set once the customer confirmed: each topic's answer is followed by the next one without waiting for the customer.
    run_queue: bool = False
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
        if user_text == lang.CALL_START:
            async for chunk in self._greet(state):
                yield chunk
            return
        if not user_text:
            yield lang.t(EMPTY_INPUT_REPLY, state["lang"])
            return
        state["lang"] = lang.detect(user_text, state["lang"])
        if state["ended"]:
            state["ended"] = False
            state["active_agent"] = ROUTER
        state["history"].append({"role": "user", "text": user_text})
        turn = _Turn(origin=state["active_agent"])
        turn_started = time.perf_counter()
        emit("turn.start", agent=state["active_agent"], user=user_text, queue=list(state["queue"]))

        try:
            plan: _Plan | None = await self._plan(state, turn)
        except Exception as exc:  # never crash the call because of routing
            logger.exception("Planning failed; staying with %s", state["active_agent"])
            emit("plan.error", error=str(exc), agent=state["active_agent"])
            plan = _Plan(state["active_agent"])

        signals = TurnSignals()
        while plan is not None:
            async for chunk in self._speak(state, turn, plan, signals):
                yield chunk
            if plan.ends_call:
                state["ended"] = True
            plan = None
            # After a confirmed plan the sequence continues even if the model forgot to call return_to_router,
            # unless the agent is waiting for a detail from the customer.
            asked = "".join(turn.parts).rstrip().endswith("?")
            returned = signals.return_requested or (turn.run_queue and not asked)
            if returned and state["active_agent"] != ROUTER and not state["ended"]:
                plan = self._after_return(state, turn)
                signals = TurnSignals()
                if plan is not None and turn.parts:
                    # Each agent's answer is its own message (and its own breath in the voice).
                    turn.parts.append(SEGMENT_BREAK)
                    yield SEGMENT_BREAK

        state["greeted"] = True
        reply = "".join(turn.parts).strip()
        if reply:
            state["history"].append({"role": "assistant", "text": reply})
        del state["history"][:-MAX_HISTORY]
        emit(
            "turn.end", agent=state["active_agent"], reply=reply, queue=list(state["queue"]),
            ended=state["ended"], handoffs=turn.handoffs, ms=elapsed_ms(turn_started),
        )

    async def _greet(self, state: dict[str, Any]) -> AsyncIterator[str]:
        """Opening line of the call: the router greets before the customer says anything."""
        if state["greeted"] or state["history"]:
            return
        turn = _Turn(origin=ROUTER)
        started = time.perf_counter()
        emit("turn.start", agent=ROUTER, user="(call connected)", queue=[])
        # The LLM needs a user message to answer; it is removed again once the greeting is spoken.
        state["history"].append({"role": "user", "text": GREETING_CUE})
        try:
            async for chunk in self._speak(state, turn, _Plan(ROUTER, P.DIRECTIVE_GREET), TurnSignals()):
                yield chunk
        finally:
            state["history"].clear()
        state["greeted"] = True
        reply = "".join(turn.parts).strip()
        if reply:
            state["history"].append({"role": "assistant", "text": reply})
        emit("turn.end", agent=ROUTER, reply=reply, queue=[], ended=False, handoffs=0, ms=elapsed_ms(started))

    # ------------------------------------------------------------------ planning

    def _transcript(self, state: dict[str, Any]) -> str:
        return format_transcript([(m["role"], m["text"]) for m in state["history"]])

    def _handoff(self, state: dict[str, Any], turn: _Turn, target: str) -> bool:
        """Switch the active agent; refuses repeated/bouncing handoffs. Returns True if switched."""
        if (
            turn.handoffs >= turn.max_handoffs
            or target == state["active_agent"]
            or target == turn.origin
            or state["handoff_count"] >= MAX_HANDOFFS_PER_CALL
        ):
            logger.info("Handoff to %s refused (active=%s)", target, state["active_agent"])
            emit("handoff", source=state["active_agent"], target=target, refused=True)
            return False
        logger.info("Handoff %s -> %s", state["active_agent"], target)
        emit("handoff", source=state["active_agent"], target=target, refused=False)
        state["active_agent"] = target
        state["handoff_count"] += 1
        turn.handoffs += 1
        return True

    async def _plan(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        if state["active_agent"] == ROUTER:
            if state["phase"] == PROPOSING and state["proposed"]:
                return await self._plan_confirmation(state, turn)
            return await self._plan_router(state, turn)
        return await self._plan_sub(state, turn)

    async def _plan_router(
        self, state: dict[str, Any], turn: _Turn, analysis: Any = None, correction: bool = False
    ) -> _Plan:
        if analysis is None:
            analysis = await self._decision.analyze_turn(self._transcript(state))
        topics = list(analysis.detected_topics) if analysis.ok else []
        logger.info(
            "Analysis: billing=%.2f orders=%.2f primary=%s end=%.2f ok=%s",
            analysis.billing, analysis.order_status, analysis.primary_topic, analysis.wants_to_end, analysis.ok,
        )
        emit(
            "decision.analyze", billing=analysis.billing, order_status=analysis.order_status,
            primary_topic=analysis.primary_topic, primary_confidence=analysis.primary_confidence,
            wants_to_end=analysis.wants_to_end, ok=analysis.ok, topics=topics,
        )

        if analysis.ok and analysis.confident_end:
            return _Plan(ROUTER, P.DIRECTIVE_GOODBYE, ends_call=True)

        if topics:
            return self._propose(state, topics)

        if (
            analysis.ok
            and analysis.primary_topic == TOPIC_OTHER
            and analysis.primary_confidence >= CHOICE_CONFIDENCE_THRESHOLD
        ):
            return _Plan(ROUTER, P.DIRECTIVE_OUT_OF_SCOPE)
        if correction:
            return _Plan(ROUTER, P.DIRECTIVE_CORRECTION)
        if not state["greeted"]:
            return _Plan(ROUTER, P.DIRECTIVE_GREET_REPLY)
        return _Plan(ROUTER, P.DIRECTIVE_CLARIFY)

    def _propose(self, state: dict[str, Any], topics: list[str]) -> _Plan:
        """Do not hand off yet: tell the customer what was understood and ask them to confirm."""
        state["proposed"] = list(topics)
        state["phase"] = PROPOSING
        state["reasks"] = 0
        state["queue"] = []
        emit("phase", phase=PROPOSING, proposed=[{"topic": t} for t in topics])
        return _Plan(ROUTER, P.DIRECTIVE_CONFIRM.format(topics=" and ".join(_label(t) for t in topics)))

    def _reset_proposal(self, state: dict[str, Any]) -> None:
        state["proposed"] = []
        state["phase"] = IDLE
        state["reasks"] = 0

    async def _plan_confirmation(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        transcript = self._transcript(state)
        # Analysed in parallel so "ok, never mind, that's all" is not mistaken for a "yes".
        verdict, analysis = await asyncio.gather(
            self._decision.confirmation(transcript), self._decision.analyze_turn(transcript)
        )
        if verdict == "yes" and analysis.ok and analysis.confident_end:
            verdict = "no"
        proposed = [t for t in state["proposed"] if t in TOPICS]
        emit(
            "decision.confirmation", verdict=verdict, wants_to_end=analysis.wants_to_end,
            proposed=[{"topic": t} for t in proposed],
        )

        if verdict == "yes" and proposed:
            self._reset_proposal(state)
            first, rest = proposed[0], proposed[1:]
            state["queue"] = list(rest)
            emit("phase", phase=IDLE, confirmed=[{"topic": t} for t in proposed])
            # One handoff per confirmed topic plus the final return to the router.
            turn.origin = ""
            turn.max_handoffs = len(proposed) + 1
            turn.run_queue = True
            if self._handoff(state, turn, AGENT_FOR_TOPIC[first]):
                return _Plan(AGENT_FOR_TOPIC[first], P.DIRECTIVE_CONFIRMED.format(topic=_label(first)))
            return _Plan(ROUTER, P.DIRECTIVE_CLARIFY)

        if verdict == "neither" and proposed:
            state["reasks"] += 1
            if state["reasks"] <= MAX_REASKS:
                return _Plan(ROUTER, P.DIRECTIVE_REASK.format(topics=" and ".join(_label(t) for t in proposed)))
            self._reset_proposal(state)
            emit("phase", phase=IDLE, proposed=[])
            return _Plan(ROUTER, P.DIRECTIVE_OPEN_QUESTION)

        self._reset_proposal(state)
        emit("phase", phase=IDLE, proposed=[])
        return await self._plan_router(state, turn, analysis=analysis, correction=True)

    async def _plan_sub(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        active = state["active_agent"]
        topic = TOPIC_FOR_AGENT[active]
        other_p, resolved_p, ok = await self._decision.on_topic(self._transcript(state), topic)
        logger.info("Guard[%s]: other_topic=%.2f resolved=%.2f ok=%s", active, other_p, resolved_p, ok)
        emit("decision.guard", agent=active, other_topic=other_p, resolved=resolved_p, ok=ok, queue=list(state["queue"]))

        pending = bool(state["pending_return"])
        resolved = pending
        state["pending_return"] = False
        noted: str | None = None
        if ok:
            other = _other(topic)
            switched = other_p >= OTHER_TOPIC_THRESHOLD
            if switched and other not in state["queue"]:
                state["queue"].append(other)
                noted = other
            # A deferred return only stands if the customer did not just ask something more on this topic.
            resolved = resolved_p >= RESOLVED_THRESHOLD or (pending and switched)

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
        emit(
            "decision.analyze", billing=analysis.billing, order_status=analysis.order_status,
            primary_topic=analysis.primary_topic, primary_confidence=analysis.primary_confidence,
            wants_to_end=analysis.wants_to_end, ok=analysis.ok,
            topics=list(analysis.detected_topics) if analysis.ok else [],
        )
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
        if turn.handoffs >= turn.max_handoffs:
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
        needs_separator = bool(turn.parts) and not turn.parts[-1][-1:].isspace()
        produced = False
        started = time.perf_counter()
        first_token_ms: int | None = None
        emit("agent.start", agent=plan.agent, directive=plan.directive, ends_call=plan.ends_call)
        try:
            async for chunk in self._runner.stream(plan.agent, history, plan.directive, signals):
                if not chunk:
                    continue
                if needs_separator and not chunk[0].isspace():
                    chunk = " " + chunk
                needs_separator = False
                produced = True
                if first_token_ms is None:
                    first_token_ms = elapsed_ms(started)
                turn.parts.append(chunk)
                yield chunk
            emit("agent.end", agent=plan.agent, first_token_ms=first_token_ms, ms=elapsed_ms(started))
        except Exception as exc:
            logger.exception("Agent %s failed", plan.agent)
            emit("agent.error", agent=plan.agent, error=str(exc), ms=elapsed_ms(started))
            if not produced:
                fallback = lang.t(FALLBACK_REPLY, state["lang"])
                turn.parts.append(fallback)
                yield fallback
