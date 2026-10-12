"""Foreground LLM abstraction: speaks and extracts parameters, never routes."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from agent_framework import Agent
from pydantic import BaseModel

from prompts import EXTRACTION_INSTRUCTIONS, FOREGROUND_INSTRUCTIONS

logger = logging.getLogger(__name__)

History = list[dict[str, str]]


@dataclass
class Extraction:
    order_id: str = ""
    invoice_period: str = "current"
    billing_question: str = ""
    order_question: str = ""


class ForegroundLLM(Protocol):
    async def say(self, instruction: str, history: History) -> str:
        """Return what the agent says aloud next, following ``instruction``."""

    async def extract(self, history: History) -> Extraction:
        """Extract order id / invoice period / questions from the conversation."""


def render_history(history: History) -> str:
    label = {"user": "Customer", "assistant": "Agent"}
    return "\n".join(f"{label.get(h['role'], h['role'])}: {h['text']}" for h in history if h.get("text"))


class _ExtractionModel(BaseModel):
    order_id: str = ""
    invoice_period: str = "current"
    billing_question: str = ""
    order_question: str = ""


_ORDER_RE = re.compile(r"\b(\d{4,})\b")


def regex_order_id(history: History) -> str:
    """Last-resort extraction: most recent 4+ digit number said by the customer."""
    for h in reversed(history):
        if h["role"] == "user":
            m = _ORDER_RE.findall(re.sub(r"(?<=\d)[\s-](?=\d)", "", h["text"]))
            if m:
                return m[-1]
    return ""


class FoundryForegroundLLM:
    """``ForegroundLLM`` backed by a MAF chat client (gpt-5.6-luna on Foundry)."""

    def __init__(self, client: Any) -> None:
        self._speaker = Agent(client, FOREGROUND_INSTRUCTIONS, name="foreground", default_options={"store": False})
        self._extractor = Agent(
            client,
            EXTRACTION_INSTRUCTIONS,
            name="extractor",
            default_options={"store": False, "response_format": _ExtractionModel},
        )

    async def say(self, instruction: str, history: History) -> str:
        prompt = f"Conversation so far:\n{render_history(history)}\n\nYour task now: {instruction}"
        return (await self._speaker.run(prompt)).text.strip()

    async def extract(self, history: History) -> Extraction:
        try:
            response = await self._extractor.run(f"Conversation:\n{render_history(history)}")
            value = response.value
            if not isinstance(value, _ExtractionModel):
                value = _ExtractionModel.model_validate_json(response.text)
            order_id = re.sub(r"\D", "", value.order_id)
            period = "previous" if value.invoice_period.lower().startswith("prev") else "current"
            return Extraction(order_id or regex_order_id(history), period, value.billing_question, value.order_question)
        except Exception as exc:
            logger.warning("parameter extraction failed, using regex fallback: %s", exc)
            return Extraction(order_id=regex_order_id(history))
