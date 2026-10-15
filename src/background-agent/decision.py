"""Client for Microsoft-Decision-1 (typed decisions over a conversation).

Decision-1 is called with a conversation transcript plus a set of typed questions and
returns calibrated probabilities. The orchestration code makes routing decisions from
those probabilities; the LLM never decides routing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx
from azure.identity.aio import DefaultAzureCredential

from calltrace import elapsed_ms, emit, emit_usage

logger = logging.getLogger(__name__)

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
DECISION_PATH = "/providers/microsoft/v1/systemone"

TOPIC_BILLING = "billing"
TOPIC_ORDERS = "order_status"
TOPIC_OTHER = "other"
TOPIC_UNCLEAR = "unclear"
TOPICS = (TOPIC_BILLING, TOPIC_ORDERS)

# Tunable thresholds. Below them we ask a clarifying question instead of guessing.
TOPIC_THRESHOLD = 0.6
CHOICE_CONFIDENCE_THRESHOLD = 0.6
END_THRESHOLD = 0.7
CONFIRM_THRESHOLD = 0.6
HUMAN_THRESHOLD = 0.7


class DecisionError(RuntimeError):
    """Decision-1 could not be reached or returned an unusable answer."""


def resource_endpoint(project_endpoint: str) -> str:
    """Return ``https://<resource>.services.ai.azure.com`` from a project endpoint."""
    parsed = urlparse(project_endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


LATEST_MARKER = "--- LATEST CUSTOMER MESSAGE: answer every question from this message alone; earlier turns are only context ---"


def format_transcript(turns: list[tuple[str, str]], limit: int = 6) -> str:
    """Render ``(role, text)`` turns as a plain transcript for Decision-1.

    Earlier turns are kept as context, but a marker line sets the latest customer message apart so topic
    questions are judged on it and not on what was discussed (and already handled) earlier in the call.
    """
    label = {"user": "Customer", "assistant": "Agent"}
    recent = [(r, t) for r, t in turns[-limit:] if t]
    lines = [f"{label.get(r, r)}: {t}" for r, t in recent]
    last = max((i for i, (r, _) in enumerate(recent) if r == "user"), default=0)
    if last == 0:
        return "\n".join(lines)
    return "\n".join([*lines[:last], LATEST_MARKER, *lines[last:]])


@dataclass
class Confirmation:
    """Decision-1's reading of the customer's reply to a proposed plan."""

    verdict: str = "neither"
    wants_to_end: float = 0.0

    @property
    def confident_end(self) -> bool:
        return self.wants_to_end >= END_THRESHOLD


class Guard(NamedTuple):
    """Result of the sub-agent guard. Unpacks like the old ``(other_topic, resolved, ok)`` triple at indexes 0-2."""

    other_topic: float
    resolved: float
    ok: bool
    unsupported: float = 0.0
    wants_human: float = 0.0


@dataclass
class TurnAnalysis:
    """Decision-1's reading of the latest customer utterance."""

    billing: float = 0.0
    order_status: float = 0.0
    primary_topic: str = TOPIC_UNCLEAR
    primary_confidence: float = 0.0
    wants_to_end: float = 0.0
    ok: bool = True
    raw: dict[str, Any] = field(default_factory=dict)
    other: float = 0.0
    wants_human: float = 0.0

    @property
    def other_detected(self) -> bool:
        """The customer raised something that is neither billing nor order status."""
        return self.other >= TOPIC_THRESHOLD or (
            self.primary_topic == TOPIC_OTHER and self.primary_confidence >= CHOICE_CONFIDENCE_THRESHOLD
        )

    @property
    def human_requested(self) -> bool:
        """The customer explicitly asked to speak to a person."""
        return self.wants_human >= HUMAN_THRESHOLD

    @property
    def detected_topics(self) -> list[str]:
        """Topics above threshold, primary (first-raised) topic first."""
        found = [t for t, p in ((TOPIC_BILLING, self.billing), (TOPIC_ORDERS, self.order_status)) if p >= TOPIC_THRESHOLD]
        if self.primary_topic in found:
            found.remove(self.primary_topic)
            found.insert(0, self.primary_topic)
        return found

    @property
    def confident_end(self) -> bool:
        return self.wants_to_end >= END_THRESHOLD


class DecisionClient:
    """Async Decision-1 client authenticated with Microsoft Entra ID."""

    def __init__(
        self,
        endpoint: str | None = None,
        deployment: str | None = None,
        credential: Any | None = None,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
        self._endpoint = (endpoint or (resource_endpoint(project) if project else "")).rstrip("/")
        self._deployment = deployment or os.environ.get("DECISION_DEPLOYMENT_NAME", "Decision-1")
        self._credential = credential or DefaultAzureCredential()
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._max_retries = max_retries

    async def ask(self, state: str, questions: dict[str, dict[str, Any]], purpose: str = "") -> dict[str, Any]:
        """Send one request with several questions; return ``answers`` keyed by question name."""
        if not self._endpoint:
            raise DecisionError("Decision-1 endpoint is not configured (FOUNDRY_PROJECT_ENDPOINT).")
        body = {"model": self._deployment, "state": state, "questions": questions}
        names = list(questions)
        url = f"{self._endpoint}{DECISION_PATH}"
        last: Exception | None = None
        started = time.perf_counter()
        for attempt in range(self._max_retries):
            try:
                token = await self._credential.get_token(TOKEN_SCOPE)
                resp = await self._http.post(url, json=body, headers={"Authorization": f"Bearer {token.token}"})
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = DecisionError(f"Decision-1 returned {resp.status_code}")
                    await asyncio.sleep(min(2**attempt * 0.4, 3.0))
                    continue
                if resp.status_code != 200:
                    raise DecisionError(f"Decision-1 returned {resp.status_code}: {resp.text[:200]}")
                payload = resp.json()
                answers = payload["answers"]
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
                emit("decision.call", purpose=purpose, model=self._deployment, questions=names, request=body,
                     answers=answers, usage=usage, attempts=attempt + 1, ms=elapsed_ms(started))
                emit_usage(self._deployment, usage, purpose)
                return answers
            except DecisionError as exc:
                emit("decision.call", purpose=purpose, model=self._deployment, questions=names, request=body,
                     error=str(exc), ms=elapsed_ms(started))
                raise
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last = exc
                await asyncio.sleep(min(2**attempt * 0.4, 3.0))
        emit("decision.call", purpose=purpose, model=self._deployment, questions=names, request=body,
             error=str(last), ms=elapsed_ms(started))
        raise DecisionError(f"Decision-1 call failed: {last}")

    async def analyze_turn(self, transcript: str) -> TurnAnalysis:
        """Detect topics (possibly several), the first-raised topic, and intent to end the call."""
        questions = {
            "billing": {
                "type": "noul",
                "instructions": "In the customer's latest message, is the customer asking about their bill, charges, invoices, balance or payments?",
            },
            "order_status": {
                "type": "noul",
                "instructions": "In the customer's latest message, is the customer asking about the status, tracking or delivery of an order?",
            },
            "other": {
                "type": "noul",
                "instructions": (
                    "In the customer's latest message, is the customer asking for help with something that is neither "
                    "billing nor the status of an order, such as a technical problem, a plan or contract change, or a "
                    "cancellation? Greetings, thanks and goodbyes are not."
                ),
            },
            "wants_human": {
                "type": "noul",
                "instructions": "Does the customer's latest message explicitly ask to speak to a person, an operator or a human agent?",
            },
            "primary_topic": {
                "type": "choice",
                "instructions": "Which topic does the customer's latest message raise first? Ignore topics from earlier turns that the latest message does not mention.",
                "criteria": {
                    TOPIC_BILLING: "bill, charges, invoice, balance or payment",
                    TOPIC_ORDERS: "order status, tracking or delivery",
                    TOPIC_OTHER: "something unrelated to billing or orders",
                    TOPIC_UNCLEAR: "the topic cannot be determined",
                },
            },
            "wants_to_end": {
                "type": "noul",
                "instructions": "Does the customer's latest message say they have nothing else and want to end the call?",
            },
        }
        try:
            a = await self.ask(transcript, questions, "analyze_turn")
            primary = a["primary_topic"]
            return TurnAnalysis(
                billing=float(a["billing"]["noul"]),
                order_status=float(a["order_status"]["noul"]),
                primary_topic=primary["choice"],
                primary_confidence=float(primary.get("confidence", 0.0)),
                wants_to_end=float(a["wants_to_end"]["noul"]),
                raw=a,
                other=float(a.get("other", {}).get("noul", 0.0)),
                wants_human=float(a.get("wants_human", {}).get("noul", 0.0)),
            )
        except (DecisionError, KeyError, TypeError, ValueError) as exc:
            logger.warning("analyze_turn failed, using safe fallback: %s", exc)
            return TurnAnalysis(ok=False)

    async def on_topic(self, transcript: str, topic: str) -> Guard:
        """Guard for a sub-agent turn.

        Returns a ``Guard``: ``other_topic`` is the probability the customer moved to the *other* supported topic,
        ``unsupported`` that they raised something outside both topics, ``wants_human`` that they asked for a person.
        """
        other = TOPIC_ORDERS if topic == TOPIC_BILLING else TOPIC_BILLING
        questions = {
            "other_topic": {
                "type": "noul",
                "instructions": f"Is the customer's latest message about {other.replace('_', ' ')} rather than {topic.replace('_', ' ')}?",
            },
            "resolved": {
                "type": "noul",
                "instructions": (
                    "Is the customer's latest message only an acknowledgement or thanks, showing they are satisfied "
                    "and have nothing more to ask on this topic? Answer no if the latest message contains any "
                    "question or request, even a repeated one."
                ),
            },
            "unsupported": {
                "type": "noul",
                "instructions": (
                    "Is the customer's latest message asking for help with something that is neither billing nor "
                    "the status of an order, such as a technical problem, a plan or contract change, or a cancellation?"
                ),
            },
            "wants_human": {
                "type": "noul",
                "instructions": "Does the customer's latest message explicitly ask to speak to a person, an operator or a human agent?",
            },
        }
        try:
            a = await self.ask(transcript, questions, f"on_topic:{topic}")
            return Guard(
                float(a["other_topic"]["noul"]),
                float(a["resolved"]["noul"]),
                True,
                float(a.get("unsupported", {}).get("noul", 0.0)),
                float(a.get("wants_human", {}).get("noul", 0.0)),
            )
        except (DecisionError, KeyError, TypeError, ValueError) as exc:
            logger.warning("on_topic guard failed, staying with current agent: %s", exc)
            return Guard(0.0, 0.0, False)

    async def confirmation(self, transcript: str) -> Confirmation:
        """Did the customer confirm a proposal? One call answers yes/no/neither and whether they want to end."""
        questions = {
            "confirmation": {
                "type": "choice",
                "instructions": "The agent proposed a plan and asked the customer to confirm. What was the customer's reply?",
                "criteria": {
                    "yes": "the customer agrees or confirms the plan is right",
                    "amend": (
                        "the customer corrects or adds a detail of the plan (for example a different order number) "
                        "but still wants everything that was proposed"
                    ),
                    "no": "the customer rejects the plan or no longer wants one of the proposed topics",
                    "neither": "the customer says something unrelated or ambiguous",
                },
            },
            "wants_to_end": {
                "type": "noul",
                "instructions": "Does the customer's latest message say they have nothing else and want to end the call?",
            },
        }
        try:
            a = await self.ask(transcript, questions, "confirmation")
            ends = float(a["wants_to_end"]["noul"])
            c = a["confirmation"]
            if float(c.get("confidence", 0.0)) < CONFIRM_THRESHOLD:
                return Confirmation("neither", ends)
            return Confirmation(c["choice"], ends)
        except (DecisionError, KeyError, TypeError, ValueError) as exc:
            logger.warning("confirmation failed: %s", exc)
            return Confirmation("neither")

    async def aclose(self) -> None:
        await self._http.aclose()
        close = getattr(self._credential, "close", None)
        if close:
            await close()
