"""Per-session routing policy: what the agent does for each kind of request.

The call-centre admin picks a route for every topic of a fixed catalog. Code enforces the policy; the LLM only
voices the outcome. The web app sends the policy at call start (``[call connected]`` + a ``[policy] {json}``
line) and the agent snapshots it into its conversation state, so it cannot change mid-call.

Keep this file identical in ``handoff-agent`` and ``background-agent`` (the agents never import each other).
"""

from __future__ import annotations

import json
import re
from typing import Any

import lang
from calltrace import emit

AI, HUMAN, DECLINE = "ai_agent", "human", "decline"
ROUTES = (AI, HUMAN, DECLINE)

BILLING, ORDERS, OTHER = "billing", "order_status", "other"
# Synthetic topic: the caller explicitly asks for a person. It always goes to a human, whatever the policy says.
HUMAN_REQUEST = "human_request"
CATALOG = (BILLING, ORDERS, OTHER)

# ``other`` has no AI specialist, so it can only be handed to a human or declined.
ALLOWED: dict[str, tuple[str, ...]] = {BILLING: ROUTES, ORDERS: ROUTES, OTHER: (HUMAN, DECLINE)}
DEFAULT_POLICY: dict[str, str] = {BILLING: AI, ORDERS: AI, OTHER: DECLINE}

POLICY_MARKER = "[policy]"
# Reasons recorded in the handoff payload.
REASON_POLICY, REASON_CALLER = "policy", "caller_requested"

_DIGITS = re.compile(r"\b\d[\d\s-]{2,}\d\b")


def normalize(raw: Any) -> dict[str, str]:
    """Return a complete, valid policy from a dict or a ``topic=route,topic=route`` string; bad input falls back."""
    policy = dict(DEFAULT_POLICY)
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                raw = json.loads(text)
            except ValueError:
                raw = {}
        else:
            raw = dict(p.split("=", 1) for p in text.split(",") if "=" in p)
    if isinstance(raw, dict):
        for topic in CATALOG:
            value = str(raw.get(topic, "")).strip().lower()
            if value in ALLOWED[topic]:
                policy[topic] = value
    return policy


def is_default(policy: dict[str, str]) -> bool:
    return normalize(policy) == DEFAULT_POLICY


def route(policy: dict[str, str] | None, topic: str) -> str:
    if topic == HUMAN_REQUEST:
        return HUMAN
    return (policy or DEFAULT_POLICY).get(topic, DECLINE)


def partition(policy: dict[str, str] | None, topics: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split ``topics`` (order kept) into ``(ai, human, declined)``."""
    ai: list[str] = []
    human: list[str] = []
    declined: list[str] = []
    for topic in topics:
        target = {AI: ai, HUMAN: human}.get(route(policy, topic), declined)
        if topic not in target:
            target.append(topic)
    return ai, human, declined


def ai_topics(policy: dict[str, str] | None) -> list[str]:
    return [t for t in (BILLING, ORDERS) if route(policy, t) == AI]


def is_call_start(text: str) -> bool:
    """True for the call-connected cue, with or without a policy line after it."""
    text = (text or "").strip()
    return text == lang.CALL_START or text.startswith(lang.CALL_START + "\n")


def policy_from_call_start(text: str) -> dict[str, str]:
    """Parse the policy carried by the call-start message (default policy when there is none)."""
    for line in (text or "").splitlines()[1:]:
        line = line.strip()
        if line.startswith(POLICY_MARKER):
            return normalize(line[len(POLICY_MARKER):].strip())
    return dict(DEFAULT_POLICY)


def call_start_message(policy: dict[str, str] | None) -> str:
    """The first user item the web app sends: the cue plus the (normalized) policy."""
    if policy is None:
        return lang.CALL_START
    return f"{lang.CALL_START}\n{POLICY_MARKER} {json.dumps(normalize(policy), separators=(',', ':'))}"


def order_ids(history: list[dict[str, str]]) -> list[str]:
    found: list[str] = []
    for item in history:
        if item.get("role") != "user":
            continue
        for match in _DIGITS.findall(item.get("text", "")):
            digits = re.sub(r"\D", "", match)
            if len(digits) >= 4 and digits not in found:
                found.append(digits)
    return found


class HandoffService:
    """Mock of the call-centre's human-handoff API: it only emits the structured payload to the trace."""

    def request(self, state: dict[str, Any], reason: str, topics: list[str]) -> dict[str, Any]:
        history = state.get("history") or []
        said = [h["text"] for h in history if h.get("role") == "user" and h.get("text")][-3:]
        payload = {
            "reason": reason,
            "topics": list(topics),
            "entities": {"order_ids": order_ids(history)},
            "summary": f"Caller needs a human for: {', '.join(topics) or 'unspecified'}. Last customer messages: "
            + " | ".join(said),
            "policy": dict(state.get("policy") or DEFAULT_POLICY),
            "lang": state.get("lang", lang.DEFAULT),
        }
        emit("human.handoff", **payload)
        return payload
