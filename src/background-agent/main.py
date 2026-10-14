"""Hosted agent entry point: foreground + background call-center agent (Responses protocol)."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import AgentResponse, AgentResponseUpdate, AgentSession, Content, Message, ResponseStream
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from decision import DecisionClient
from foreground import Foreground, ensure_state
from lang import t as tr
from task_store import TaskStore

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("background-agent")


def build_foreground() -> Foreground:
    from agent_framework.foundry import FoundryChatClient

    from llm import FoundryForegroundLLM
    from workers import build_worker_runner

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    if os.environ.get("SUBAGENT_MODE", "remote").lower() == "remote":
        from remote_workers import RemoteWorkerRunner, agent_names_from_env, build_openai_for

        runner = RemoteWorkerRunner(build_openai_for(os.environ["FOUNDRY_PROJECT_ENDPOINT"]), agent_names_from_env())
    else:
        runner = build_worker_runner(client)
    return Foreground(DecisionClient(), FoundryForegroundLLM(client), TaskStore(runner))


class BackgroundCallCenterAgent:
    """Duck-typed ``SupportsAgentRun`` wrapping the code-driven ``Foreground``."""

    id = "background-call-center-agent"
    name = "background-call-center-agent"
    description = "Telecom call-center voice agent: confirms tasks, then runs billing / order lookups in the background."

    def __init__(self, foreground: Foreground | None = None) -> None:
        self._foreground = foreground

    @property
    def foreground(self) -> Foreground:
        if self._foreground is None:
            self._foreground = build_foreground()
        return self._foreground

    def create_session(self, *, session_id: str | None = None, **kwargs: Any) -> AgentSession:
        return AgentSession(session_id=session_id or uuid.uuid4().hex)

    def get_session(self, service_session_id: Any, *, session_id: str | None = None, **kwargs: Any) -> AgentSession:
        return AgentSession(service_session_id=service_session_id, session_id=session_id)

    @staticmethod
    def _key(session: AgentSession) -> str:
        return str(session.session_id or session.service_session_id or uuid.uuid4().hex)

    async def _chunks(self, messages: Any, session: AgentSession | None) -> AsyncIterator[str]:
        session = session or self.create_session()
        items = [messages] if isinstance(messages, (str, Message)) else list(messages or [])
        text = " ".join(m if isinstance(m, str) else (m.text or "") for m in items).strip()
        state = ensure_state(session.state)
        key = state.setdefault("session_key", self._key(session))
        if not text:
            yield tr("empty_input", state["lang"])
            return
        async for chunk in self.foreground.handle(text, state, key):
            # Blank line = message boundary (one bubble per answer). It is a suffix, not a prefix,
            # so the TTS sees the sentence end immediately instead of holding the chunk until the next one.
            yield chunk.rstrip() + "\n\n"

    def run(self, messages: Any = None, *, stream: bool = False, session: AgentSession | None = None, **kwargs: Any) -> Any:
        if stream:
            async def updates() -> AsyncIterator[AgentResponseUpdate]:
                async for chunk in self._chunks(messages, session):
                    yield AgentResponseUpdate(contents=[Content.from_text(chunk)], role="assistant")

            return ResponseStream(updates(), finalizer=AgentResponse.from_updates)

        async def collect() -> AgentResponse:
            text = "".join([c async for c in self._chunks(messages, session)])
            return AgentResponse(messages=[Message("assistant", [text])])

        return collect()


def main() -> None:
    from voice_server import VoiceResponsesHostServer

    VoiceResponsesHostServer(BackgroundCallCenterAgent(), history_source="agent").run(
        port=int(os.environ["PORT"]) if os.environ.get("PORT") else None
    )


if __name__ == "__main__":
    main()
