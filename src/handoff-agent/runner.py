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
from orchestrator import BILLING, ORDERS, ROUTER, SEGMENT_BREAK, TurnSignals

logger = logging.getLogger(__name__)

_FACTORIES = {
    ROUTER: create_router_agent,
    BILLING: create_billing_agent,
    ORDERS: create_orders_agent,
}

_TOOL_TYPES = ("function_call", "function_result")
RETURN_TOOL = "return_to_router"


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


class MafAgentRunner:
    def __init__(self, client: Any) -> None:
        self._client = client

    def stream(
        self, agent: str, history: list[dict[str, str]], directive: str | None, signals: TurnSignals, state: dict | None = None
    ) -> AsyncIterator[str]:
        maf_agent = _FACTORIES[agent](self._client, directive, signals)
        messages = [Message(m["role"], [m["text"]]) for m in history]
        return self._consume(agent, maf_agent.run(messages, stream=True), signals)

    @staticmethod
    async def _consume(agent: str, updates: Any, signals: TurnSignals) -> AsyncIterator[str]:
        """Turn agent updates (local MAF or relayed from a remote agent) into spoken text, live.

        After ``return_to_router`` has been called the model tends to repeat its answer, so text
        produced from then on is buffered per segment and only emitted if it adds something new.
        """
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
        unsegmented = False  # spoken text not yet closed by a segment break
        async for update in updates:
            contents = update.contents or []
            for c in contents:
                if getattr(c, "type", None) == "usage":
                    usage = add_usage_details(usage, c.usage_details)
            if any(getattr(c, "type", None) in _TOOL_TYPES for c in contents):
                out = flush()
                if out:
                    emitted += out
                    unsegmented = unsegmented or bool(out.strip())
                    yield out
                pending_space = True
                # "I'll check that now" is its own message, spoken while the data tools run; the answer that
                # follows is the next one. The final return_to_router call closes nothing: the caller adds the break.
                calls_return = any(getattr(c, "name", None) == RETURN_TOOL for c in contents)
                if unsegmented and not calls_return and not signals.return_requested:
                    emitted += SEGMENT_BREAK
                    unsegmented = False
                    pending_space = False
                    yield SEGMENT_BREAK
            text = update.text
            if not text:
                continue
            if signals.return_requested:
                buffered.append(text)
                continue
            text = with_space(text)
            pending_space = False
            emitted += text
            unsegmented = unsegmented or bool(text.strip())
            yield text

        out = flush()
        if out:
            yield out
        emit_usage(os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-luna"), usage, f"agent:{agent}")