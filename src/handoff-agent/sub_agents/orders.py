from __future__ import annotations

from typing import Any

from agent_framework import Agent

from sub_agents._common import build_agent, make_return_tool
from prompts import ORDERS_INSTRUCTIONS
from tools import ORDER_TOOLS


def create_orders_agent(client: Any, directive: str | None, signals: Any) -> Agent:
    instructions = ORDERS_INSTRUCTIONS + (f"\nFor this turn: {directive}\n" if directive else "")
    return build_agent(client, "orders", instructions, [*ORDER_TOOLS, make_return_tool(signals)])
