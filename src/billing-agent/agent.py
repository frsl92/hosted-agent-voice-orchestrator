"""Builds the billing specialist as a MAF agent."""

from __future__ import annotations

import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

from prompts import BILLING_INSTRUCTIONS
from tools import BILLING_TOOLS


def build_agent() -> Agent:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    return Agent(
        client,
        BILLING_INSTRUCTIONS,
        name="billing-agent",
        description="Telecom billing specialist: invoices, charges, balance and payments.",
        tools=BILLING_TOOLS,
        default_options={"store": False},
    )
