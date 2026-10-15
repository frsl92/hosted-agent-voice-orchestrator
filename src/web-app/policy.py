"""Per-session routing policy chosen in the UI and handed to the agent at call start.

The agents own the semantics (see ``routing_policy.py`` in the agents); this module only validates what the
browser sends and builds the call-start message. The catalog is fixed, only the route per topic is configurable.
"""

from __future__ import annotations

import json
from typing import Any

AI, HUMAN, DECLINE = "ai_agent", "human", "decline"
CATALOG = ("billing", "order_status", "other")
ALLOWED: dict[str, tuple[str, ...]] = {
    "billing": (AI, HUMAN, DECLINE),
    "order_status": (AI, HUMAN, DECLINE),
    "other": (HUMAN, DECLINE),
}
DEFAULT_POLICY: dict[str, str] = {"billing": AI, "order_status": AI, "other": DECLINE}

PRESETS = [
    {"id": "normal", "label": "Normal", "policy": dict(DEFAULT_POLICY)},
    {"id": "billing-human", "label": "Billing → human",
     "policy": {"billing": HUMAN, "order_status": AI, "other": DECLINE}},
    {"id": "orders-only", "label": "Orders only",
     "policy": {"billing": DECLINE, "order_status": AI, "other": DECLINE}},
    {"id": "other-human", "label": "Other → human",
     "policy": {"billing": AI, "order_status": AI, "other": HUMAN}},
]

CALL_START = "[call connected]"
POLICY_MARKER = "[policy]"


def normalize(raw: Any) -> dict[str, str]:
    """A complete, valid policy from a dict, a JSON string or ``topic=route,topic=route``; bad input falls back."""
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


def call_start_message(policy: dict[str, str] | None) -> str:
    """The first user item: the cue alone for the default policy, otherwise the cue plus a policy line."""
    if policy is None or is_default(policy):
        return CALL_START
    return f"{CALL_START}\n{POLICY_MARKER} {json.dumps(normalize(policy), separators=(',', ':'))}"
