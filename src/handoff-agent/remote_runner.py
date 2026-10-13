"""Runs the billing / orders sub-agents as separate Foundry hosted agents, over the Responses API.

The orchestrator calls each sub-agent through ``AIProjectClient.get_openai_client(agent_name=...)``.
The first call to a sub-agent lets Foundry create a session for it; the session id is kept in the
conversation ``state`` so every later call to the same sub-agent lands in the same session (and sandbox).
The reply is streamed back and relayed through the same text pipeline as a local agent.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

from calltrace import elapsed_ms, emit
from orchestrator import BILLING, ORDERS, TurnSignals
from runner import RETURN_TOOL, MafAgentRunner

logger = logging.getLogger(__name__)

DEFAULT_AGENT_NAMES = {BILLING: "billing-agent", ORDERS: "orders-agent"}
USER_IDENTITY_HEADER = "x-ms-user-identity"


def agent_names_from_env() -> dict[str, str]:
    return {
        BILLING: os.environ.get("BILLING_AGENT_NAME", DEFAULT_AGENT_NAMES[BILLING]),
        ORDERS: os.environ.get("ORDERS_AGENT_NAME", DEFAULT_AGENT_NAMES[ORDERS]),
    }


def delegation_from_env() -> bool:
    """Opt-in: send ``x-ms-user-identity`` (needs the UserIdentityImpersonation data action). Off by default."""
    return os.environ.get("SUBAGENT_USER_DELEGATION", "false").strip().lower() in ("1", "true", "yes")


def build_input(history: list[dict[str, str]], synced: int, directive: str | None) -> str:
    """The text sent to a sub-agent for this call.

    The sub-agent keeps its own history in its Foundry session, so it only needs what it has not seen:
    the messages since its last turn (everything, on the first call) and this turn's directive.
    """
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=len(history) - 1)
    latest = history[last_user]["text"] if history else ""
    unseen = history[min(synced, last_user):]
    if not directive and len(unseen) <= 1:
        return latest
    parts = []
    if directive:
        parts.append(f"Instruction for this turn: {directive}")
    if len(unseen) > 1:
        lines = [f"{'Customer' if m['role'] == 'user' else 'Agent'}: {m['text']}" for m in unseen]
        parts.append("Conversation since your last turn:\n" + "\n".join(lines))
    parts.append(f"Customer's latest message: {latest}")
    return "\n\n".join(parts)


def _extra(obj: Any, key: str) -> Any:
    """Read a field the OpenAI SDK model does not declare (e.g. Foundry's ``agent_session_id``)."""
    value = getattr(obj, key, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(key)
    return value


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _tool_update(name: str) -> SimpleNamespace:
    return SimpleNamespace(text="", contents=[SimpleNamespace(type="function_call", name=name)])


class RemoteSubAgentRunner:
    """``AgentRunner`` that sends billing / orders turns to their own hosted agents; the router stays local."""

    def __init__(
        self,
        local: MafAgentRunner,
        openai_for: Callable[[str], Any],
        agent_names: dict[str, str] | None = None,
        delegate_user: bool = False,
    ) -> None:
        self._local = local
        self._openai_for = openai_for
        self._names = agent_names or dict(DEFAULT_AGENT_NAMES)
        self._delegate_user = delegate_user

    def stream(
        self,
        agent: str,
        history: list[dict[str, str]],
        directive: str | None,
        signals: TurnSignals,
        state: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        if agent not in self._names or state is None:
            return self._local.stream(agent, history, directive, signals, state)
        updates = self._updates(agent, history, directive, signals, state)
        return MafAgentRunner._consume(agent, updates, signals)

    async def _updates(
        self, agent: str, history: list[dict[str, str]], directive: str | None, signals: TurnSignals, state: dict[str, Any]
    ) -> AsyncIterator[Any]:
        name = self._names[agent]
        entry: dict[str, Any] = state.setdefault("remote", {}).setdefault(agent, {})
        for attempt in (1, 2):
            produced = False
            had_session = bool(entry.get("agent_session_id"))
            try:
                async for update in self._call(agent, name, history, directive, signals, state, entry):
                    produced = True
                    yield update
                return
            except Exception as exc:
                # A session that expired (idle timeout) is replaced by a fresh one, which gets the whole conversation.
                if attempt == 1 and had_session and not produced:
                    emit("subagent.session_lost", agent=name, agent_session_id=entry.get("agent_session_id"), error=str(exc))
                    entry.clear()
                    continue
                raise

    async def _call(
        self,
        agent: str,
        name: str,
        history: list[dict[str, str]],
        directive: str | None,
        signals: TurnSignals,
        state: dict[str, Any],
        entry: dict[str, Any],
    ) -> AsyncIterator[Any]:
        session_id = entry.get("agent_session_id")
        previous = entry.get("previous_response_id")
        text = build_input(history, entry.get("synced", 0) if session_id else 0, directive)
        kwargs: dict[str, Any] = {"input": text, "stream": True}
        if previous:
            kwargs["previous_response_id"] = previous
        if session_id:
            kwargs["extra_body"] = {"agent_session_id": session_id}
        caller = None
        if self._delegate_user:
            # A hosted agent called by another agent has no end user of its own; the orchestrator names one per
            # conversation so Foundry can scope the sub-agent session (needs the UserIdentityImpersonation role).
            caller = state.setdefault("caller_id", f"call-{uuid.uuid4().hex}")
            kwargs["extra_headers"] = {USER_IDENTITY_HEADER: caller}
        started = time.perf_counter()
        emit(
            "subagent.call", agent=name, new_session=not session_id, agent_session_id=session_id,
            previous_response_id=previous, user_identity=caller, input=text,
        )
        stream = await self._openai_for(name).responses.create(**kwargs)

        calls: dict[str, dict[str, Any]] = {}
        usage: dict[str, int] | None = None
        response_id: str | None = None
        async for event in stream:
            kind = getattr(event, "type", "")
            if kind in ("response.created", "response.in_progress", "response.completed"):
                response = event.response
                response_id = getattr(response, "id", None) or response_id
                session_id = _extra(response, "agent_session_id") or session_id
                if kind == "response.completed" and getattr(response, "usage", None):
                    u = response.usage
                    usage = {
                        "input_token_count": getattr(u, "input_tokens", 0) or 0,
                        "output_token_count": getattr(u, "output_tokens", 0) or 0,
                    }
            elif kind == "response.output_text.delta":
                if event.delta:
                    yield SimpleNamespace(text=event.delta, contents=[])
            elif kind == "response.output_item.added" and getattr(event.item, "type", "") == "function_call":
                item = event.item
                calls[item.call_id] = {"name": item.name, "args": getattr(item, "arguments", "") or "", "start": time.perf_counter()}
                yield _tool_update(item.name)
                if item.name == RETURN_TOOL:
                    signals.return_requested = True
            elif kind == "response.output_item.done":
                item = event.item
                item_type = getattr(item, "type", "")
                if item_type == "function_call" and item.call_id in calls:
                    calls[item.call_id]["args"] = getattr(item, "arguments", "") or calls[item.call_id]["args"]
                elif item_type == "function_call_output":
                    call = calls.get(item.call_id, {})
                    emit(
                        "tool.call", name=call.get("name", "?"), args=_parse_args(call.get("args", "")),
                        result=getattr(item, "output", ""),
                        remote=name, ms=elapsed_ms(call["start"]) if "start" in call else None,
                    )
            elif kind in ("response.failed", "error"):
                error = getattr(getattr(event, "response", None), "error", None) or getattr(event, "message", "") or kind
                raise RuntimeError(f"sub-agent {name} failed: {error}")

        if session_id:
            entry["agent_session_id"] = session_id
        if response_id:
            entry["previous_response_id"] = response_id
        entry["synced"] = len(state["history"])
        emit(
            "subagent.done", agent=name, agent_session_id=session_id, response_id=response_id,
            new_session=not previous, ms=elapsed_ms(started),
        )
        if usage:
            yield SimpleNamespace(text="", contents=[SimpleNamespace(type="usage", usage_details=usage)])


def build_openai_for(endpoint: str) -> Callable[[str], Any]:
    """Lazily build one async OpenAI client per sub-agent, all sharing one Entra-authenticated project client."""
    from azure.ai.projects.aio import AIProjectClient
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    project = AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True)
    clients: dict[str, Any] = {}

    def openai_for(agent_name: str) -> Any:
        if agent_name not in clients:
            clients[agent_name] = project.get_openai_client(agent_name=agent_name)
        return clients[agent_name]

    return openai_for
