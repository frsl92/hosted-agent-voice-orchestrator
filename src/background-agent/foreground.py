"""Code-driven foreground orchestration.

The foreground never lets the LLM decide routing: Decision-1 (via ``DecisionClient``) decides
whether a topic was raised, whether the customer confirmed, and whether they want to end the
call. The LLM only speaks and extracts parameters. Background workers can only be launched
after an explicit confirmation=yes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from decision import TOPIC_BILLING, TOPIC_ORDERS, DecisionClient, TurnAnalysis, format_transcript
from llm import Extraction, ForegroundLLM
from task_store import Task, TaskStatus, TaskStore, signature

logger = logging.getLogger(__name__)

IDLE, PROPOSING, RUNNING = "idle", "proposing", "running"

ACK = "On it, checking that now."
CLOSING = "Is there anything else I can help with?"
GOODBYE_FALLBACK = "Thanks for calling. Have a great day!"
PROGRESS = (
    "Still working on it, just a moment.",
    "Almost there, thanks for your patience.",
    "Still checking, this is taking a little longer than usual.",
)
STILL_WORKING = "I'm still working on that. I'll share it as soon as it's ready."
CANCEL_RE = re.compile(r"\b(cancel|stop that|never ?mind|forget it|abort)\b", re.IGNORECASE)
MAX_HISTORY = 30
MAX_REASKS = 2


def new_state() -> dict[str, Any]:
    return {"phase": IDLE, "proposed": [], "history": [], "task_ids": [], "reasks": 0, "pending_topics": []}


def ensure_state(state: dict[str, Any]) -> dict[str, Any]:
    for key, value in new_state().items():
        state.setdefault(key, value)
    return state


def describe(topic: str, params: dict[str, Any]) -> str:
    if topic == TOPIC_BILLING:
        what = "your previous bill" if params.get("invoice_period") == "previous" else "your latest bill"
        q = params.get("question")
        return f"look into {what}" + (f" ({q})" if q else "")
    q = params.get("question")
    return f"check the status of order {params.get('order_id', '')}" + (f" ({q})" if q else "")


def short_label(topic: str, params: dict[str, Any]) -> str:
    return "your bill" if topic == TOPIC_BILLING else f"order {params.get('order_id', '')}"


def build_params(topic: str, ex: Extraction) -> dict[str, Any]:
    if topic == TOPIC_BILLING:
        return {"invoice_period": ex.invoice_period or "current", "question": ex.billing_question}
    return {"order_id": ex.order_id, "question": ex.order_question}


class Foreground:
    def __init__(
        self,
        decision: DecisionClient,
        llm: ForegroundLLM,
        store: TaskStore,
        *,
        poll_interval: float = 0.2,
        progress_interval: float = 8.0,
        max_progress: int = 3,
        max_wait: float = 90.0,
    ) -> None:
        self.decision = decision
        self.llm = llm
        self.store = store
        self.poll_interval = poll_interval
        self.progress_interval = progress_interval
        self.max_progress = max_progress
        self.max_wait = max_wait

    # ---- helpers -------------------------------------------------------------------------

    @staticmethod
    def _remember(state: dict[str, Any], role: str, text: str) -> str:
        state["history"].append({"role": role, "text": text})
        del state["history"][:-MAX_HISTORY]
        return text

    async def _say(self, state: dict[str, Any], instruction: str, fallback: str) -> str:
        try:
            text = await self.llm.say(instruction, state["history"])
        except Exception as exc:
            logger.warning("foreground LLM failed: %s", exc)
            text = ""
        return self._remember(state, "assistant", text or fallback)

    def _transcript(self, state: dict[str, Any]) -> str:
        return format_transcript([(h["role"], h["text"]) for h in state["history"]])

    def _active_summary(self, key: str) -> str:
        return " and ".join(short_label(t.topic, t.params) for t in self.store.active(key))

    # ---- entry point ---------------------------------------------------------------------

    async def handle(self, user_text: str, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        """Process one customer utterance, yielding speakable chunks as they become ready."""
        ensure_state(state)
        self._remember(state, "user", user_text)

        async for chunk in self._deliver_ready(state, key):
            yield chunk
        if state["phase"] == RUNNING and not self.store.active(key):
            state["phase"] = IDLE

        if self.store.active(key) and CANCEL_RE.search(user_text):
            self.store.cancel(key)
            state["phase"] = IDLE
            state["proposed"] = []
            yield await self._say(state, "Tell the customer you stopped that request and ask if there is anything else.",
                                  "No problem, I've stopped that. " + CLOSING)
            return

        if state["phase"] == PROPOSING and state["proposed"]:
            async for chunk in self._handle_confirmation(state, key):
                yield chunk
        else:
            async for chunk in self._handle_request(state, key):
                yield chunk

    # ---- confirmation --------------------------------------------------------------------

    async def _handle_confirmation(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        transcript = self._transcript(state)
        # Analyse in parallel so "ok never mind, that's all" is not mistaken for a "yes".
        verdict, analysis = await asyncio.gather(
            self.decision.confirmation(transcript), self.decision.analyze_turn(transcript)
        )
        if CANCEL_RE.search(state["history"][-1]["text"]) or (
            verdict == "yes" and analysis.ok and analysis.confident_end
        ):
            verdict = "no"
        if verdict == "yes":
            async for chunk in self._launch_and_wait(state, key):
                yield chunk
        elif verdict == "no":
            state["proposed"] = []
            state["phase"] = IDLE
            state["reasks"] = 0
            async for chunk in self._handle_request(state, key, correction=True, analysis=analysis):
                yield chunk
        else:
            state["reasks"] += 1
            if state["reasks"] <= MAX_REASKS:
                plan = " and ".join(describe(p["topic"], p["params"]) for p in state["proposed"])
                yield await self._say(
                    state,
                    f"The customer did not clearly answer. Briefly ask again whether they want you to {plan}.",
                    "Sorry, I didn't catch that. Shall I go ahead?",
                )
            else:
                state["proposed"] = []
                state["phase"] = IDLE
                state["reasks"] = 0
                yield await self._say(
                    state,
                    "Drop the earlier plan and ask an open question: what can you help with, their bill or an order?",
                    "What can I help you with today, your bill or an order?",
                )

    # ---- new request / small talk --------------------------------------------------------

    async def _handle_request(
        self, state: dict[str, Any], key: str, correction: bool = False, analysis: TurnAnalysis | None = None
    ) -> AsyncIterator[str]:
        analysis = analysis or await self.decision.analyze_turn(self._transcript(state))
        active = self.store.active(key)

        if not analysis.ok:
            note = f" Work is in progress on {self._active_summary(key)}." if active else ""
            yield await self._say(
                state,
                "Reply helpfully to the customer's last message. You can help with their bill and order status only."
                " If unclear, ask one short clarifying question." + note,
                "Sorry, could you say that again? I can help with your bill or an order.",
            )
            async for chunk in self._wait_if_active(state, key):
                yield chunk
            return

        if analysis.confident_end:
            if active:
                yield self._remember(state, "assistant", f"Sure. I'm just finishing {self._active_summary(key)}.")
                async for chunk in self._wait(state, key, closing_goodbye=True):
                    yield chunk
            else:
                state["pending_topics"] = []
                yield await self._say(state, "Say a brief, warm goodbye; the call is ending.", GOODBYE_FALLBACK)
            return

        topics = list(analysis.detected_topics)
        for t in state["pending_topics"]:
            if t not in topics:
                topics.append(t)

        if topics:
            async for chunk in self._propose(state, key, topics):
                yield chunk
            return

        if active:
            yield self._remember(state, "assistant", f"I'm still checking {self._active_summary(key)}.")
            async for chunk in self._wait(state, key):
                yield chunk
            return

        if correction:
            yield await self._say(state, "Say no problem and ask what they would like to change.",
                                  "No problem. What would you like me to change?")
            return

        yield await self._say(
            state,
            "Reply conversationally to the customer's last message. You can only help with their bill and order status;"
            " if they ask for something else, say so politely. If it is unclear, ask one clarifying question.",
            "I can help with your bill or the status of an order. Which would you like?",
        )

    async def _propose(self, state: dict[str, Any], key: str, topics: list[str]) -> AsyncIterator[str]:
        ex = await self.llm.extract(state["history"])
        if TOPIC_ORDERS in topics and not ex.order_id:
            state["pending_topics"] = topics
            yield await self._say(
                state,
                "You need the order number to check an order. Ask the customer for it."
                + (" Mention you can look at their bill as well." if TOPIC_BILLING in topics else ""),
                "Sure. What's the order number?",
            )
            return

        proposed = [{"topic": t, "params": build_params(t, ex)} for t in topics]
        active = {task.signature for task in self.store.active(key)}
        proposed = [p for p in proposed if signature(p["topic"], p["params"]) not in active]
        state["pending_topics"] = []
        if not proposed:
            yield self._remember(state, "assistant", f"I'm already checking {self._active_summary(key)}.")
            async for chunk in self._wait(state, key):
                yield chunk
            return

        state["proposed"] = proposed
        state["phase"] = PROPOSING
        state["reasks"] = 0
        plan = " and ".join(describe(p["topic"], p["params"]) for p in proposed)
        yield await self._say(
            state,
            f"Tell the customer what you understood: you'd like me to {plan}. Ask them to confirm that is right."
            " Do not say you have started anything yet.",
            f"So you'd like me to {plan}. Is that right?",
        )

    # ---- launching and streaming results -------------------------------------------------

    async def _launch_and_wait(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        for p in state["proposed"]:
            task = self.store.launch(key, p["topic"], p["params"])
            if task.id not in state["task_ids"]:
                state["task_ids"].append(task.id)
        state["proposed"] = []
        state["reasks"] = 0
        state["phase"] = RUNNING
        yield self._remember(state, "assistant", ACK)
        async for chunk in self._wait(state, key):
            yield chunk

    async def _wait_if_active(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        if self.store.active(key):
            async for chunk in self._wait(state, key):
                yield chunk

    async def _deliver_ready(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        for task in self.store.collect_completed(key):
            yield self._remember(state, "assistant", self._result_text(task))

    @staticmethod
    def _result_text(task: Task) -> str:
        label = short_label(task.topic, task.params)
        if task.status == TaskStatus.FAILED or not task.result:
            return f"Sorry, I couldn't finish checking {label} just now. Would you like me to try again?"
        return f"Here's the update on {label}. {task.result}"

    async def _wait(self, state: dict[str, Any], key: str, closing_goodbye: bool = False) -> AsyncIterator[str]:
        """Stream progress and each result once; keep workers alive if we stop waiting."""
        loop = asyncio.get_running_loop()
        start = last = loop.time()
        progress = 0
        failed = False
        while True:
            for task in self.store.collect_completed(key):
                failed = failed or task.status == TaskStatus.FAILED or not task.result
                yield self._remember(state, "assistant", self._result_text(task))
            if not self.store.active(key):
                break
            now = loop.time()
            if now - start >= self.max_wait:
                state["phase"] = RUNNING
                yield self._remember(state, "assistant", STILL_WORKING)
                return
            await asyncio.sleep(self.poll_interval)
            now = loop.time()
            if now - last >= self.progress_interval and progress < self.max_progress:
                last = now
                yield self._remember(state, "assistant", PROGRESS[progress % len(PROGRESS)])
                progress += 1

        state["phase"] = IDLE
        if closing_goodbye:
            yield await self._say(state, "Say a brief, warm goodbye; the call is ending.", GOODBYE_FALLBACK)
        elif not failed:
            yield self._remember(state, "assistant", CLOSING)
