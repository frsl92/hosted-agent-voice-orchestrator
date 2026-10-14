from __future__ import annotations

from typing import Any

from agent_framework import Agent

from sub_agents._common import build_agent
from prompts import ROUTER_INSTRUCTIONS


def create_router_agent(client: Any, directive: str | None, signals: Any = None) -> Agent:
    instructions = ROUTER_INSTRUCTIONS + (f"\nFor this turn: {directive}\n" if directive else "")
    return build_agent(client, "router", instructions, [])
