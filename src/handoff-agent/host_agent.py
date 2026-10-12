"""Duck-typed ``SupportsAgentRun`` wrapper hosted by ``ResponsesHostServer``."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import AgentResponse, AgentResponseUpdate, AgentSession, Content, Message, ResponseStream

from orchestrator import Orchestrator, ensure_state

logger = logging.getLogger(__name__)


def build_default_orchestrator() -> Orchestrator:
    """Wire the real Foundry chat client, Decision-1 client and agent runner."""
    from azure.identity import DefaultAzureCredential

    from agent_framework.foundry import FoundryChatClient
    from decision import DecisionClient
    from runner import MafAgentRunner

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    return Orchestrator(DecisionClient(), MafAgentRunner(client))


def _user_text(messages: Any) -> str:
    if messages is None:
        return ""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, Message):
        messages = [messages]
    texts = [m.text for m in messages if getattr(m, "role", "user") == "user" and getattr(m, "text", "")]
    return " ".join(texts).strip()


class CallCenterAgent:
    """Router + billing/orders call-center agent exposed through the Responses protocol."""

    id = "handoff-agent"
    name = "handoff-agent"
    description = "Telecom call-center voice agent: a router hands off to billing / order-status sub-agents."

    def __init__(self, orchestrator: Orchestrator | None = None) -> None:
        self._orchestrator = orchestrator or build_default_orchestrator()

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(session_id=session_id)

    def get_session(self, service_session_id: str, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(service_session_id=service_session_id, session_id=session_id)

    def run(
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        **kwargs: Any,
    ) -> Any:
        if session is None:
            session = self.create_session()
        state = ensure_state(session.state)
        text = _user_text(messages)
        if not text:
            logger.warning("Empty user input; raw messages: %r", messages)

        async def updates() -> AsyncIterator[AgentResponseUpdate]:
            async for chunk in self._orchestrator.handle_turn(state, text):
                yield AgentResponseUpdate(contents=[Content.from_text(chunk)], role="assistant")

        if stream:
            return ResponseStream(updates(), finalizer=AgentResponse.from_updates)

        async def collect() -> AgentResponse:
            parts = [chunk async for chunk in self._orchestrator.handle_turn(state, text)]
            return AgentResponse(messages=[Message("assistant", ["".join(parts)])])

        return collect()
