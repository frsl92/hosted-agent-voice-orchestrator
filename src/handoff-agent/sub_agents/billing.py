from __future__ import annotations

from typing import Any

from agent_framework import Agent

from sub_agents._common import build_agent, make_return_tool
from prompts import BILLING_INSTRUCTIONS
from tools import BILLING_TOOLS


def create_billing_agent(client: Any, directive: str | None, signals: Any) -> Agent:
    instructions = BILLING_INSTRUCTIONS + (f"\nFor this turn: {directive}\n" if directive else "")
    return build_agent(client, "billing", instructions, [*BILLING_TOOLS, make_return_tool(signals)])
