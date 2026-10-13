"""Builds the order-status specialist as a MAF agent."""

from __future__ import annotations

import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

from prompts import ORDERS_INSTRUCTIONS
from tools import ORDER_TOOLS


def build_agent() -> Agent:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    return Agent(
        client,
        ORDERS_INSTRUCTIONS,
        name="orders-agent",
        description="Telecom order-status specialist: order status, tracking and delivery dates.",
        tools=ORDER_TOOLS,
        default_options={"store": False},
    )
