"""Real ``AgentRunner``: runs the MAF ``Agent`` for the active role and streams its text."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import Message, add_usage_details

from calltrace import emit_usage
from sub_agents import create_billing_agent, create_orders_agent, create_router_agent
from orchestrator import BILLING, ORDERS, ROUTER, TurnSignals

logger = logging.getLogger(__name__)

_FACTORIES = {
    ROUTER: create_router_agent,
    BILLING: create_billing_agent,
    ORDERS: create_orders_agent,
}

_TOOL_TYPES = ("function_call", "function_result")


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


class MafAgentRunner:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def stream(
        self, agent: str, history: list[dict[str, str]], directive: str | None, signals: TurnSignals
    ) -> AsyncIterator[str]:
        """Stream spoken text live.

        After ``return_to_router`` has been called the model tends to repeat its answer, so text
        produced from then on is buffered per segment and only emitted if it adds something new.
        """
        maf_agent = _FACTORIES[agent](self._client, directive, signals)
        messages = [Message(m["role"], [m["text"]]) for m in history]
        emitted = ""
        buffered: list[str] = []
        pending_space = False

        def with_space(text: str) -> str:
            if pending_space and emitted and not emitted[-1].isspace() and not text[0].isspace():
                return " " + text
            return text

        def flush() -> str:
            segment = "".join(buffered)
            buffered.clear()
            norm = _norm(segment)
            if not norm or norm in _norm(emitted):
                return ""
            return with_space(segment)

        usage: Any = None
        async for update in maf_agent.run(messages, stream=True):
            for c in update.contents or []:
                if getattr(c, "type", None) == "usage":
                    usage = add_usage_details(usage, c.usage_details)
            if any(getattr(c, "type", None) in _TOOL_TYPES for c in (update.contents or [])):
                out = flush()
                if out:
                    emitted += out
                    yield out
                pending_space = True
            text = update.text
            if not text:
                continue
            if signals.return_requested:
                buffered.append(text)
                continue
            text = with_space(text)
            pending_space = False
            emitted += text
            yield text

        out = flush()
        if out:
            yield out
        emit_usage(os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-luna"), usage, f"agent:{agent}")