"""Shared helpers for the sub-agent factories."""

from __future__ import annotations

from typing import Annotated, Any

from agent_framework import Agent, tool


def make_return_tool(signals: Any):
    """Build a ``return_to_router`` tool bound to this call's ``signals`` (no globals)."""

    @tool(approval_mode="never_require")
    async def return_to_router(
        reason: Annotated[str, "Short reason, e.g. 'billing question answered'"] = "",
    ) -> str:
        """Call when the customer's question on your topic is fully answered and they have nothing more to ask about it."""
        signals.return_requested = True
        return "Done. The customer has already heard your answer, so do not repeat it or say anything more."

    return return_to_router


def build_agent(client: Any, name: str, instructions: str, tools: list[Any]) -> Agent:
    return Agent(
        client,
        instructions,
        name=name,
        tools=tools,
        default_options={"store": False},
    )
