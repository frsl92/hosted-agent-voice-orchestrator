"""Code-driven call orchestration: router -> billing/orders sub-agent -> router.

Routing is decided here from Decision-1 probabilities. The LLM only speaks and extracts
parameters. All per-conversation data lives in a JSON-serializable ``state`` dict (no module
globals), so it can be persisted by the host between ``/responses`` calls.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import lang
import prompts as P
from calltrace import elapsed_ms, emit
from decision import (
    HUMAN_THRESHOLD,
    TOPIC_BILLING,
    TOPIC_ORDERS,
    TOPIC_OTHER,
    TOPICS,
    format_transcript,
)
from routing_policy import (
    AI,
    DECLINE,
    DEFAULT_POLICY,
    HUMAN,
    HUMAN_REQUEST,
    REASON_CALLER,
    REASON_POLICY,
    HandoffService,
    is_call_start,
    partition,
    policy_from_call_start,
    route,
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
        self,
        agent: str,
        history: list[dict[str, str]],
        directive: str | None,
        signals: TurnSignals,
        state: dict[str, Any] | None = None,
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
        # Routing policy snapshot taken at call start, and what is still owed to a human at the end of the call.
        "policy": dict(DEFAULT_POLICY),
        "deferred_human": [],
        "declined_noted": [],
        "proposed_declined": [],
        # Foundry session ids of the remote sub-agents, so repeated calls reuse the same session.
        "remote": {},
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
        self._human = HandoffService()

    async def handle_turn(self, state: dict[str, Any], user_text: str) -> AsyncIterator[str]:
        """Process one customer utterance, yielding the reply text as it is produced."""
        ensure_state(state)
        user_text = (user_text or "").strip()
        if is_call_start(user_text):
            if not state["greeted"] and not state["history"]:
                state["policy"] = policy_from_call_start(user_text)
                emit("policy.snapshot", policy=dict(state["policy"]))
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
                    if not "".join(turn.parts).endswith(SEGMENT_BREAK):
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
            async for chunk in self._speak(
                state, turn, _Plan(ROUTER, P.directive_greet(state["policy"])), TurnSignals()
            ):
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
            other=analysis.other, wants_human=analysis.wants_human,
        )

        if analysis.ok and analysis.confident_end:
            return _Plan(ROUTER, P.DIRECTIVE_GOODBYE, ends_call=True)

        policy = state["policy"]
        if analysis.ok and getattr(analysis, "human_requested", False):
            return self._human_plan(state, [HUMAN_REQUEST], REASON_CALLER)

        requested = list(topics)
        if analysis.ok and getattr(analysis, "other_detected", False):
            requested.append(TOPIC_OTHER)
        ai, human, declined = partition(policy, requested)
        if ai or human:
            return self._propose(state, ai + human, declined)
        if declined:
            emit("policy.declined", topics=declined, policy=dict(policy))
            return _Plan(ROUTER, P.directive_declined(policy, declined))
        if correction:
            return _Plan(ROUTER, P.DIRECTIVE_CORRECTION)
        if not state["greeted"]:
            return _Plan(ROUTER, P.directive_greet_reply(policy))
        return _Plan(ROUTER, P.directive_clarify(policy))

    def _human_plan(self, state: dict[str, Any], topics: list[str], reason: str) -> _Plan:
        """Transfer the call to a human operator (mock): record the payload, say it, and end the call."""
        self._human.request(state, reason, topics)
        state["deferred_human"] = []
        if reason == REASON_CALLER:
            return _Plan(ROUTER, P.DIRECTIVE_HUMAN_REQUESTED, ends_call=True)
        return _Plan(ROUTER, P.DIRECTIVE_HUMAN_HANDOFF.format(topics=P.topics_text(topics)), ends_call=True)

    def _closing(self, state: dict[str, Any]) -> _Plan:
        """The router has the call back and every AI topic is done: hand over to a human if one is owed."""
        if state["deferred_human"]:
            return self._human_plan(state, list(state["deferred_human"]), REASON_POLICY)
        return _Plan(ROUTER, P.DIRECTIVE_ANYTHING_ELSE)

    def _propose(self, state: dict[str, Any], topics: list[str], declined: list[str] | None = None) -> _Plan:
        """Do not hand off yet: tell the customer what was understood and ask them to confirm."""
        declined = list(declined or [])
        state["proposed"] = list(topics)
        state["proposed_declined"] = declined
        state["phase"] = PROPOSING
        state["reasks"] = 0
        state["queue"] = []
        emit(
            "phase", phase=PROPOSING, proposed=[{"topic": t, "route": route(state["policy"], t)} for t in topics],
            declined=declined,
        )
        directive = P.DIRECTIVE_CONFIRM.format(topics=P.topics_text(topics)) + P.confirm_extra(
            state["policy"], topics, declined
        )
        return _Plan(ROUTER, directive)

    def _reset_proposal(self, state: dict[str, Any]) -> None:
        state["proposed"] = []
        state["proposed_declined"] = []
        state["phase"] = IDLE
        state["reasks"] = 0

    async def _plan_confirmation(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        # One Decision-1 call reads yes/no/neither and "that's all" together, so "ok, never mind" is not a "yes".
        conf = await self._decision.confirmation(self._transcript(state))
        verdict = conf.verdict
        if verdict == "yes" and conf.confident_end:
            verdict = "no"
        proposed = list(state["proposed"])
        declined = list(state["proposed_declined"])
        emit(
            "decision.confirmation", verdict=verdict, wants_to_end=conf.wants_to_end,
            proposed=[{"topic": t} for t in proposed],
        )

        if verdict == "yes" and proposed:
            self._reset_proposal(state)
            ai, human, _ = partition(state["policy"], proposed)
            state["deferred_human"] = list(human)
            emit("phase", phase=IDLE, confirmed=[{"topic": t} for t in proposed])
            if not ai:
                return self._closing(state)
            first, rest = ai[0], ai[1:]
            state["queue"] = list(rest)
            # One handoff per confirmed topic plus the final return to the router.
            turn.origin = ""
            turn.max_handoffs = len(ai) + 1
            turn.run_queue = True
            if self._handoff(state, turn, AGENT_FOR_TOPIC[first]):
                return _Plan(AGENT_FOR_TOPIC[first], P.DIRECTIVE_CONFIRMED.format(topic=_label(first)))
            return _Plan(ROUTER, P.directive_clarify(state["policy"]))

        if verdict == "amend" and proposed:
            # "Actually the order is 3472": keep every proposed topic and ask for confirmation again.
            plan = self._propose(state, proposed, declined)
            plan.directive = P.DIRECTIVE_CONFIRM_AMENDED.format(topics=P.topics_text(proposed)) + P.confirm_extra(
                state["policy"], proposed, declined
            )
            return plan

        if verdict == "neither" and proposed:
            state["reasks"] += 1
            if state["reasks"] <= MAX_REASKS:
                return _Plan(ROUTER, P.DIRECTIVE_REASK.format(topics=P.topics_text(proposed)))
            self._reset_proposal(state)
            emit("phase", phase=IDLE, proposed=[])
            return _Plan(ROUTER, P.directive_open_question(state["policy"]))

        self._reset_proposal(state)
        emit("phase", phase=IDLE, proposed=[])
        # Only a rejection needs the full topic analysis (the customer may be correcting the plan).
        return await self._plan_router(state, turn, correction=True)

    async def _plan_sub(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        active = state["active_agent"]
        topic = TOPIC_FOR_AGENT[active]
        guard = await self._decision.on_topic(self._transcript(state), topic)
        other_p, resolved_p, ok = guard[0], guard[1], guard[2]
        unsupported_p = getattr(guard, "unsupported", 0.0)
        human_p = getattr(guard, "wants_human", 0.0)
        logger.info("Guard[%s]: other_topic=%.2f resolved=%.2f ok=%s", active, other_p, resolved_p, ok)
        emit(
            "decision.guard", agent=active, other_topic=other_p, resolved=resolved_p, ok=ok,
            unsupported=unsupported_p, wants_human=human_p, queue=list(state["queue"]),
        )

        pending = bool(state["pending_return"])
        resolved = pending
        state["pending_return"] = False
        noted: str | None = None
        noted_directive = P.DIRECTIVE_NOTED_OTHER
        policy = state["policy"]
        if ok and human_p >= HUMAN_THRESHOLD:
            # The caller asked for a person: leave the specialist and transfer now.
            turn.origin = ""
            turn.max_handoffs = max(turn.max_handoffs, 1)
            if self._handoff(state, turn, ROUTER):
                return self._human_plan(state, [HUMAN_REQUEST], REASON_CALLER)
        if ok:
            other = _other(topic)
            switched = other_p >= OTHER_TOPIC_THRESHOLD
            if switched:
                target = route(policy, other)
                if target == AI and other not in state["queue"]:
                    state["queue"].append(other)
                    noted = other
                elif target == HUMAN and other not in state["deferred_human"]:
                    state["deferred_human"].append(other)
                    noted, noted_directive = other, P.DIRECTIVE_NOTED_HUMAN
                elif target == DECLINE and other not in state["declined_noted"]:
                    state["declined_noted"].append(other)
                    noted, noted_directive = other, P.DIRECTIVE_NOTED_DECLINED
            if unsupported_p >= OTHER_TOPIC_THRESHOLD and route(policy, TOPIC_OTHER) == HUMAN:
                if TOPIC_OTHER not in state["deferred_human"]:
                    state["deferred_human"].append(TOPIC_OTHER)
                    noted, noted_directive = TOPIC_OTHER, P.DIRECTIVE_NOTED_HUMAN
            # A deferred return only stands if the customer did not just ask something more on this topic.
            resolved = resolved_p >= RESOLVED_THRESHOLD or (pending and switched)

        if resolved:
            return await self._advance(state, turn)
        return _Plan(active, noted_directive.format(other=_label(noted)) if noted else None)

    async def _advance(self, state: dict[str, Any], turn: _Turn) -> _Plan:
        """The current topic is resolved: bridge to the next queued topic, or return to the router."""
        queued = self._next_queued(state, turn)
        if queued is not None:
            return queued
        active = state["active_agent"]
        if not self._handoff(state, turn, ROUTER):
            return _Plan(active)
        if state["deferred_human"]:
            return self._closing(state)
        analysis = await self._decision.analyze_turn(self._transcript(state))
        emit(
            "decision.analyze", billing=analysis.billing, order_status=analysis.order_status,
            primary_topic=analysis.primary_topic, primary_confidence=analysis.primary_confidence,
            wants_to_end=analysis.wants_to_end, ok=analysis.ok,
            topics=list(analysis.detected_topics) if analysis.ok else [],
            other=analysis.other, wants_human=analysis.wants_human,
        )
        if analysis.ok and analysis.confident_end:
            return _Plan(ROUTER, P.DIRECTIVE_GOODBYE, ends_call=True)
        return self._closing(state)

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
            return self._closing(state)
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
            async for chunk in self._runner.stream(plan.agent, history, plan.directive, signals, state=state):
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
