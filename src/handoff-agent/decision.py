"""Client for Microsoft-Decision-1 (typed decisions over a conversation).

Decision-1 is called with a conversation transcript plus a set of typed questions and
returns calibrated probabilities. The orchestration code makes routing decisions from
those probabilities; the LLM never decides routing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from azure.identity.aio import DefaultAzureCredential

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


class DecisionError(RuntimeError):
    """Decision-1 could not be reached or returned an unusable answer."""


def resource_endpoint(project_endpoint: str) -> str:
    """Return ``https://<resource>.services.ai.azure.com`` from a project endpoint."""
    parsed = urlparse(project_endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


def format_transcript(turns: list[tuple[str, str]], limit: int = 8) -> str:
    """Render ``(role, text)`` turns as a plain transcript for Decision-1."""
    label = {"user": "Customer", "assistant": "Agent"}
    return "\n".join(f"{label.get(r, r)}: {t}" for r, t in turns[-limit:] if t)


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

    async def ask(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Send one request with several questions; return ``answers`` keyed by question name."""
        if not self._endpoint:
            raise DecisionError("Decision-1 endpoint is not configured (FOUNDRY_PROJECT_ENDPOINT).")
        body = {"model": self._deployment, "state": state, "questions": questions}
        url = f"{self._endpoint}{DECISION_PATH}"
        last: Exception | None = None
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
                return resp.json()["answers"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last = exc
                await asyncio.sleep(min(2**attempt * 0.4, 3.0))
        raise DecisionError(f"Decision-1 call failed: {last}")

    async def analyze_turn(self, transcript: str) -> TurnAnalysis:
        """Detect topics (possibly several), the first-raised topic, and intent to end the call."""
        questions = {
            "billing": {
                "type": "noul",
                "instructions": "Is the customer asking about their bill, charges, invoices, balance or payments?",
            },
            "order_status": {
                "type": "noul",
                "instructions": "Is the customer asking about the status, tracking or delivery of an order?",
            },
            "primary_topic": {
                "type": "choice",
                "instructions": "Which topic did the customer raise first in their latest message?",
                "criteria": {
                    TOPIC_BILLING: "bill, charges, invoice, balance or payment",
                    TOPIC_ORDERS: "order status, tracking or delivery",
                    TOPIC_OTHER: "something unrelated to billing or orders",
                    TOPIC_UNCLEAR: "the topic cannot be determined",
                },
            },
            "wants_to_end": {
                "type": "noul",
                "instructions": "Does the customer say they have nothing else and want to end the call?",
            },
        }
        try:
            a = await self.ask(transcript, questions)
            primary = a["primary_topic"]
            return TurnAnalysis(
                billing=float(a["billing"]["noul"]),
                order_status=float(a["order_status"]["noul"]),
                primary_topic=primary["choice"],
                primary_confidence=float(primary.get("confidence", 0.0)),
                wants_to_end=float(a["wants_to_end"]["noul"]),
                raw=a,
            )
        except (DecisionError, KeyError, TypeError, ValueError) as exc:
            logger.warning("analyze_turn failed, using safe fallback: %s", exc)
            return TurnAnalysis(ok=False)

    async def on_topic(self, transcript: str, topic: str) -> tuple[float, float, bool]:
        """Guard for a sub-agent turn: ``(still_on_topic, resolved, ok)``-style signals.

        Returns ``(other_topic_prob, resolved_prob, ok)`` where ``other_topic_prob`` is the
        probability the customer moved to a *different* supported topic.
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
        }
        try:
            a = await self.ask(transcript, questions)
            return float(a["other_topic"]["noul"]), float(a["resolved"]["noul"]), True
        except (DecisionError, KeyError, TypeError, ValueError) as exc:
            logger.warning("on_topic guard failed, staying with current agent: %s", exc)
            return 0.0, 0.0, False

    async def confirmation(self, transcript: str) -> str:
        """Did the customer confirm a proposal? Returns ``yes``, ``no`` or ``neither``."""
        questions = {
            "confirmation": {
                "type": "choice",
                "instructions": "The agent proposed a plan and asked the customer to confirm. What was the customer's reply?",
                "criteria": {
                    "yes": "the customer agrees or confirms the plan is right",
                    "no": "the customer disagrees, corrects, or rejects the plan",
                    "neither": "the customer says something unrelated or ambiguous",
                },
            }
        }
        try:
            a = (await self.ask(transcript, questions))["confirmation"]
            if float(a.get("confidence", 0.0)) < CONFIRM_THRESHOLD:
                return "neither"
            return a["choice"]
        except (DecisionError, KeyError, TypeError, ValueError) as exc:
            logger.warning("confirmation failed: %s", exc)
            return "neither"

    async def aclose(self) -> None:
        await self._http.aclose()
        close = getattr(self._credential, "close", None)
        if close:
            await close()
