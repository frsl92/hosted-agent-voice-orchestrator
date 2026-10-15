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
import time
from collections.abc import AsyncIterator
from typing import Any

from calltrace import elapsed_ms, emit
from decision import (
    TOPIC_BILLING,
    TOPIC_ORDERS,
    TOPIC_OTHER,
    DecisionClient,
    TurnAnalysis,
    format_transcript,
)
from lang import DEFAULT, EN, detect
from lang import t as tr
from llm import Extraction, ForegroundLLM
from routing_policy import (
    DEFAULT_POLICY,
    HUMAN_REQUEST,
    REASON_CALLER,
    REASON_POLICY,
    HandoffService,
    ai_topics,
    is_call_start,
    partition,
    policy_from_call_start,
)
from task_store import Task, TaskStatus, TaskStore, signature

logger = logging.getLogger(__name__)

IDLE, PROPOSING, RUNNING = "idle", "proposing", "running"

CANCEL_RE = re.compile(
    r"\b(cancel|stop that|never ?mind|forget it|abort|cancela(r|do)?|deixa estar|deixe estar|esquece|esqueça|"
    r"esqueca|não importa|nao importa|pára isso|para isso)\b",
    re.IGNORECASE,
)
MAX_HISTORY = 30
MAX_REASKS = 2


def new_state() -> dict[str, Any]:
    return {
        "phase": IDLE, "proposed": [], "history": [], "task_ids": [], "reasks": 0, "pending_topics": [],
        "lang": DEFAULT,
        # Routing policy snapshot taken at call start, and the topics owed to a human once the results are given.
        "policy": dict(DEFAULT_POLICY), "proposed_human": [], "proposed_declined": [], "deferred_human": [],
    }


def ensure_state(state: dict[str, Any]) -> dict[str, Any]:
    for key, value in new_state().items():
        state.setdefault(key, value)
    return state


# What the AI can do, in the words used inside LLM instructions (English: the model translates as needed).
_CAN_AND = {TOPIC_BILLING: "bill", TOPIC_ORDERS: "order status"}
_CAN_CHOICE = {TOPIC_BILLING: "their bill", TOPIC_ORDERS: "an order"}
_CAN_OR = {TOPIC_BILLING: "their bill", TOPIC_ORDERS: "the status of an order"}


def can_and(policy: dict[str, str]) -> str:
    return " and ".join(_CAN_AND[t] for t in ai_topics(policy))


def can_or(policy: dict[str, str]) -> str:
    return " or ".join(_CAN_OR[t] for t in ai_topics(policy))


def can_choice(policy: dict[str, str]) -> str:
    return " or ".join(_CAN_CHOICE[t] for t in ai_topics(policy))


def topic_label(topic: str, lang: str) -> str:
    return tr(f"label_{topic}", lang)

def describe(topic: str, params: dict[str, Any], lang: str = EN) -> str:
    q = params.get("question")
    if topic == TOPIC_BILLING:
        previous = params.get("invoice_period") == "previous"
        if lang == EN:
            what = "your previous bill" if previous else "your latest bill"
            return f"look into {what}" + (f" ({q})" if q else "")
        what = tr("your_prev_bill" if previous else "your_latest_bill", lang)
        return f"veja {what}" + (f" ({q})" if q else "")
    order_id = params.get("order_id", "")
    if lang == EN:
        return f"check the status of order {order_id}" + (f" ({q})" if q else "")
    return f"verifique o estado da encomenda {order_id}" + (f" ({q})" if q else "")


def short_label(topic: str, params: dict[str, Any], lang: str = EN) -> str:
    if topic == TOPIC_BILLING:
        return tr("your_bill", lang)
    return tr("order_n", lang, order_id=params.get("order_id", ""))


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
        self.human = HandoffService()

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

    @staticmethod
    def _tr(state: dict[str, Any], name: str, **fmt: str) -> str:
        return tr(name, state["lang"], **fmt)

    def _cap_str(self, state: dict[str, Any], name: str) -> str:
        """A fixed sentence that mentions what the AI can help with, matched to this call's routing policy."""
        ai = ai_topics(state["policy"])
        if len(ai) == 2:
            return self._tr(state, name)
        if not ai:
            return self._tr(state, f"{name}_none")
        return self._tr(state, f"{name}_one", can=self._tr(state, f"can_{ai[0]}"))

    def _active_summary(self, key: str, lang: str = EN) -> str:
        joiner = " and " if lang == EN else " e "
        return joiner.join(short_label(task.topic, task.params, lang) for task in self.store.active(key))

    # ---- entry point ---------------------------------------------------------------------

    async def greet(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        """Opening line of the call, spoken before the customer says anything."""
        ensure_state(state)
        started = time.perf_counter()
        emit("turn.start", agent="foreground", phase=state["phase"], user="(call connected)", active_tasks=0)
        can = can_or(state["policy"])
        text = await self._say(
            state,
            "The call has just connected. Greet the customer like a call-center operator: say hello, thank them for "
            "calling, and ask how you can help"
            + (f", mentioning that you can help with {can}." if can else ", without listing what you can do."),
            self._cap_str(state, "greeting"),
        )
        emit("turn.end", agent="foreground", phase=state["phase"], reply=text, active_tasks=0, ms=elapsed_ms(started))
        yield text

    async def handle(self, user_text: str, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        """Process one customer utterance, yielding speakable chunks as they become ready."""
        ensure_state(state)
        if is_call_start(user_text):
            if not state["history"]:
                state["policy"] = policy_from_call_start(user_text)
                emit("policy.snapshot", policy=dict(state["policy"]))
                async for chunk in self.greet(state, key):
                    yield chunk
            return
        state["lang"] = detect(user_text, state["lang"])
        self._remember(state, "user", user_text)
        started = time.perf_counter()
        emit("turn.start", agent="foreground", phase=state["phase"], user=user_text, active_tasks=len(self.store.active(key)))
        replies: list[str] = []
        try:
            async for chunk in self._handle(user_text, state, key):
                replies.append(chunk)
                yield chunk
        finally:
            emit("turn.end", agent="foreground", phase=state["phase"], reply=" ".join(replies),
                 active_tasks=len(self.store.active(key)), ms=elapsed_ms(started))

    async def _handle(self, user_text: str, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        async for chunk in self._deliver_ready(state, key):
            yield chunk
        if state["phase"] == RUNNING and not self.store.active(key):
            state["phase"] = IDLE

        if state["deferred_human"] and not self.store.active(key) and state["phase"] != PROPOSING:
            # The results were delivered while the customer was busy: the owed transfer comes now.
            async for chunk in self._transfer(state, key):
                yield chunk
            return

        if self.store.active(key) and CANCEL_RE.search(user_text):
            self.store.cancel(key)
            state["phase"] = IDLE
            state["proposed"] = []
            state["proposed_human"] = []
            state["proposed_declined"] = []
            state["deferred_human"] = []
            yield await self._say(state, "Tell the customer you stopped that request and ask if there is anything else.",
                                  self._tr(state, "cancelled") + self._tr(state, "closing"))
            return

        if state["phase"] == PROPOSING and (state["proposed"] or state["proposed_human"]):
            async for chunk in self._handle_confirmation(state, key):
                yield chunk
        else:
            async for chunk in self._handle_request(state, key):
                yield chunk

    # ---- human transfer ------------------------------------------------------------------

    async def _transfer(
        self, state: dict[str, Any], key: str, topics: list[str] | None = None, reason: str = REASON_POLICY
    ) -> AsyncIterator[str]:
        """Hand the call to a human operator (mock): record the payload and say it."""
        topics = list(topics or state["deferred_human"])
        state["deferred_human"] = []
        state["phase"] = IDLE
        self.human.request(state, reason, topics)
        if reason == REASON_CALLER:
            yield await self._say(
                state,
                "The customer asked to speak to a person. Say that you are transferring them to a human operator right "
                "now, thank them for calling and say goodbye. Do not ask whether they need anything else.",
                self._tr(state, "human_requested"),
            )
            return
        labels = " and ".join(topic_label(t, EN) for t in topics)
        joiner = " and " if state["lang"] == EN else " e "
        yield await self._say(
            state,
            f"The customer needs {labels}, which a human colleague takes care of. Say that you are now transferring "
            "them to a human operator who will help with that, thank them for calling and say goodbye. Do not ask "
            "whether they need anything else and do not promise any waiting time.",
            self._tr(state, "human_transfer", topics=joiner.join(topic_label(t, state["lang"]) for t in topics)),
        )

    # ---- confirmation --------------------------------------------------------------------

    async def _handle_confirmation(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        transcript = self._transcript(state)
        # One Decision-1 call reads yes/no/neither and "that's all" together, so "ok, never mind" is not a "yes".
        conf = await self.decision.confirmation(transcript)
        verdict = conf.verdict
        if CANCEL_RE.search(state["history"][-1]["text"]) or (verdict == "yes" and conf.confident_end):
            verdict = "no"
        emit("decision.confirmation", verdict=verdict, wants_to_end=conf.wants_to_end, proposed=state["proposed"],
             human=state["proposed_human"])
        if verdict == "yes":
            async for chunk in self._launch_and_wait(state, key):
                yield chunk
        elif verdict in ("no", "amend"):
            previous = state["proposed"] + [{"topic": t, "params": {"human": True}} for t in state["proposed_human"]]
            state["proposed"] = []
            state["proposed_human"] = []
            state["proposed_declined"] = []
            state["phase"] = IDLE
            state["reasks"] = 0
            # The customer may be correcting the plan, so it needs the full topic analysis. An "amend" keeps every
            # topic of the plan; a "no" keeps them only when a detail changed (see _amends).
            async for chunk in self._handle_request(
                state, key, correction=True, previous=previous, keep_previous=verdict == "amend"
            ):
                yield chunk
        else:
            state["reasks"] += 1
            if state["reasks"] <= MAX_REASKS:
                plan = " and ".join(
                    [describe(p["topic"], p["params"]) for p in state["proposed"]]
                    + [f"transfer you to a colleague for {topic_label(t, EN)}" for t in state["proposed_human"]]
                )
                yield await self._say(
                    state,
                    f"The customer did not clearly answer. Briefly ask again whether they want you to {plan}.",
                    self._tr(state, "reask"),
                )
            else:
                state["proposed"] = []
                state["proposed_human"] = []
                state["proposed_declined"] = []
                state["phase"] = IDLE
                state["reasks"] = 0
                choice = can_choice(state["policy"])
                yield await self._say(
                    state,
                    "Drop the earlier plan and ask an open question: what can you help with"
                    + (f", {choice}?" if choice else "?"),
                    self._cap_str(state, "open_question"),
                )

    # ---- new request / small talk --------------------------------------------------------

    async def _handle_request(
        self, state: dict[str, Any], key: str, correction: bool = False, analysis: TurnAnalysis | None = None,
        previous: list[dict[str, Any]] | None = None, keep_previous: bool = False,
    ) -> AsyncIterator[str]:
        ex: Extraction | None = None
        if analysis is None:
            # Read the topics and the details (order id, period) in parallel: it saves a model round trip before
            # the confirmation question, and also covers a rejected plan being corrected.
            analysis, ex = await asyncio.gather(
                self.decision.analyze_turn(self._transcript(state)), self.llm.extract(state["history"])
            )
        analysis = analysis or await self.decision.analyze_turn(self._transcript(state))
        emit(
            "decision.analyze", billing=analysis.billing, order_status=analysis.order_status,
            primary_topic=analysis.primary_topic, wants_to_end=analysis.wants_to_end, ok=analysis.ok,
            topics=list(analysis.detected_topics), other=analysis.other, wants_human=analysis.wants_human,
        )
        active = self.store.active(key)

        if not analysis.ok:
            note = f" Work is in progress on {self._active_summary(key)}." if active else ""
            yield await self._say(
                state,
                f"Reply helpfully to the customer's last message. You can help with {self._can_text(state)} only."
                " If unclear, ask one short clarifying question." + note,
                self._tr(state, "say_again"),
            )
            async for chunk in self._wait_if_active(state, key):
                yield chunk
            return

        if analysis.confident_end:
            if active:
                yield self._remember(
                    state, "assistant", self._tr(state, "finishing", label=self._active_summary(key, state["lang"]))
                )
                async for chunk in self._wait(state, key, closing_goodbye=True):
                    yield chunk
            else:
                state["pending_topics"] = []
                yield await self._say(state, "Say a brief, warm goodbye; the call is ending.", self._tr(state, "goodbye"))
            return

        if analysis.human_requested:
            # An explicit request for a person always wins: let running work finish, then transfer.
            state["pending_topics"] = []
            if active:
                async for chunk in self._wait(state, key, closing=False):
                    yield chunk
            async for chunk in self._transfer(state, key, [HUMAN_REQUEST], REASON_CALLER):
                yield chunk
            return

        topics = list(analysis.detected_topics)
        for pending in state["pending_topics"]:
            if pending not in topics:
                topics.append(pending)

        if previous and (keep_previous or (ex is not None and self._amends(previous, ex))):
            # "Actually the order is 3472": keep the rest of the rejected plan instead of forgetting it.
            for p in previous:
                if p["topic"] not in topics:
                    topics.append(p["topic"])
        if ex is not None:
            topics = [tp for tp in topics if tp not in ex.dropped_topics]
        if analysis.other_detected and TOPIC_OTHER not in topics:
            topics.append(TOPIC_OTHER)

        ai, human, declined = partition(state["policy"], topics)
        if declined:
            emit("policy.declined", topics=declined)

        if ai or human:
            async for chunk in self._propose(state, key, ai, human, declined, ex):
                yield chunk
            return

        if declined:
            state["pending_topics"] = []
            labels = " and ".join(topic_label(t, EN) for t in declined)
            yield await self._say(
                state,
                f"The customer asked about {labels}, which you cannot help with on this call. Say so politely, "
                f"without offering to look into it, and mention you can help with {self._can_text(state, 'or')}."
                if ai_topics(state["policy"]) else
                f"The customer asked about {labels}, which you cannot help with on this call. Say so politely, "
                "without offering anything else.",
                self._tr(state, "declined", topics=" e ".join(topic_label(t, state["lang"]) for t in declined))
                if state["lang"] != EN else self._tr(state, "declined", topics=labels),
            )
            return

        if active:
            yield self._remember(
                state, "assistant", self._tr(state, "still_checking", label=self._active_summary(key, state["lang"]))
            )
            async for chunk in self._wait(state, key):
                yield chunk
            return

        if correction:
            yield await self._say(state, "Say no problem and ask what they would like to change. Do not ask them to "
                                  "confirm anything and do not offer to look anything up yet.",
                                  self._tr(state, "change"))
            return

        yield await self._say(
            state,
            f"Reply conversationally to the customer's last message. You can only help with {self._can_text(state)};"
            " if they ask for something else, say so politely. If it is unclear, ask one clarifying question."
            " Never ask them to confirm a request and never offer to start a lookup yourself.",
            self._cap_str(state, "which_one"),
        )

    @staticmethod
    def _can_text(state: dict[str, Any], joiner: str = "and") -> str:
        if joiner == "or":
            return can_or(state["policy"]) or "nothing"
        return can_and(state["policy"]) or "nothing"

    @staticmethod
    def _amends(previous: list[dict[str, Any]], ex: Extraction) -> bool:
        """True when the extraction changed a detail of the rejected plan (a new order id or bill period)."""
        for p in previous:
            if p["params"].get("human"):
                continue
            if p["topic"] == TOPIC_ORDERS and ex.order_id and ex.order_id != p["params"].get("order_id"):
                return True
            if p["topic"] == TOPIC_BILLING and ex.invoice_period != p["params"].get("invoice_period"):
                return True
        return False

    async def _propose(
        self, state: dict[str, Any], key: str, topics: list[str], human: list[str] | None = None,
        declined: list[str] | None = None, ex: Extraction | None = None,
    ) -> AsyncIterator[str]:
        human = list(human or [])
        declined = list(declined or [])
        proposed: list[dict[str, Any]] = []
        if topics:
            ex = ex or await self.llm.extract(state["history"])
            emit("llm.extract", order_id=ex.order_id, invoice_period=ex.invoice_period,
                 billing_question=ex.billing_question, order_question=ex.order_question, topics=topics)
            if TOPIC_ORDERS in topics and not ex.order_id:
                state["pending_topics"] = topics + human + declined
                yield await self._say(
                    state,
                    "You need the order number to check an order. Ask the customer for it."
                    + (" Mention you can look at their bill as well." if TOPIC_BILLING in topics else ""),
                    self._tr(state, "ask_order"),
                )
                return

            proposed = [{"topic": tp, "params": build_params(tp, ex)} for tp in topics]
            active = {task.signature for task in self.store.active(key)}
            proposed = [p for p in proposed if signature(p["topic"], p["params"]) not in active]
        state["pending_topics"] = []
        if not proposed and not human:
            yield self._remember(
                state, "assistant", self._tr(state, "already_checking", label=self._active_summary(key, state["lang"]))
            )
            async for chunk in self._wait(state, key):
                yield chunk
            return

        state["proposed"] = proposed
        state["proposed_human"] = human
        state["proposed_declined"] = declined
        state["phase"] = PROPOSING
        emit("phase", phase=PROPOSING, proposed=proposed, human=human, declined=declined)
        state["reasks"] = 0
        plan = " and ".join(describe(p["topic"], p["params"]) for p in proposed)
        local_plan = (" and " if state["lang"] == EN else " e ").join(
            describe(p["topic"], p["params"], state["lang"]) for p in proposed
        )
        joiner = " and " if state["lang"] == EN else " e "
        human_en = " and ".join(topic_label(t, EN) for t in human)
        human_local = joiner.join(topic_label(t, state["lang"]) for t in human)
        declined_en = " and ".join(topic_label(t, EN) for t in declined)
        declined_local = joiner.join(topic_label(t, state["lang"]) for t in declined)

        if not proposed:
            instruction = (
                f"Tell the customer what you understood: they need help with {human_en}, which you cannot handle but a "
                "human colleague can. Ask whether they would like you to transfer them. Do not transfer yet."
            )
            fallback = self._tr(state, "confirm_human_only", human=human_local)
        elif human:
            instruction = (
                f"Tell the customer what you understood: you'd like me to {plan}. Say that afterwards you will transfer "
                f"them to a human colleague for {human_en}. Ask them to confirm that is right."
                " Do not say you have started anything yet."
            )
            fallback = self._tr(state, "confirm_human", plan=local_plan, human=human_local)
        else:
            instruction = (
                f"Tell the customer what you understood: you'd like me to {plan}. Ask them to confirm that is right."
                " Do not say you have started anything yet."
            )
            fallback = self._tr(state, "confirm", plan=local_plan)
        if declined:
            instruction += f" Also say briefly that you cannot help with {declined_en} on this call."
            fallback += " " + self._tr(state, "declined", topics=declined_local)
        yield await self._say(state, instruction, fallback)

    # ---- launching and streaming results -------------------------------------------------

    async def _launch_and_wait(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        human = list(state["proposed_human"])
        for p in state["proposed"]:
            task = self.store.launch(key, p["topic"], p["params"])
            if task.id not in state["task_ids"]:
                state["task_ids"].append(task.id)
        launched = bool(state["proposed"])
        state["proposed"] = []
        state["proposed_human"] = []
        state["proposed_declined"] = []
        state["reasks"] = 0
        if human:
            state["deferred_human"] = human
        if not launched:
            # Nothing for the AI to do: the confirmed plan is just the transfer.
            async for chunk in self._transfer(state, key, human):
                yield chunk
            return
        state["phase"] = RUNNING
        emit("phase", phase=RUNNING, task_ids=list(state["task_ids"]))
        yield self._remember(state, "assistant", self._tr(state, "ack"))
        async for chunk in self._wait(state, key):
            yield chunk

    async def _wait_if_active(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        if self.store.active(key):
            async for chunk in self._wait(state, key):
                yield chunk

    async def _deliver_ready(self, state: dict[str, Any], key: str) -> AsyncIterator[str]:
        done = self.store.collect_completed(key)
        remaining = len(list(self.store.active(key)))
        for i, task in enumerate(done):
            yield self._remember(
                state, "assistant", self._result_text(task, state["lang"], i, len(done) + remaining)
            )

    @staticmethod
    def _result_text(task: Task, lang: str = EN, position: int = 0, total: int = 1) -> str:
        """One spoken message per sub-agent result, introduced so topics don't blur together."""
        label = short_label(task.topic, task.params, lang)
        if task.status == TaskStatus.FAILED or not task.result:
            return tr("failed", lang, label=label)
        key = "update_single" if total <= 1 else "update_first" if position == 0 else "update_next"
        return tr(key, lang, label=label, result=task.result)

    async def _wait(
        self, state: dict[str, Any], key: str, closing_goodbye: bool = False, closing: bool = True
    ) -> AsyncIterator[str]:
        """Stream progress and each result once; keep workers alive if we stop waiting."""
        loop = asyncio.get_running_loop()
        start = last = loop.time()
        progress = 0
        failed = False
        delivered = 0
        while True:
            batch = self.store.collect_completed(key)
            if batch:
                total = delivered + len(batch) + len(list(self.store.active(key)))
                for task in batch:
                    failed = failed or task.status == TaskStatus.FAILED or not task.result
                    yield self._remember(state, "assistant", self._result_text(task, state["lang"], delivered, total))
                    delivered += 1
            if not self.store.active(key):
                break
            now = loop.time()
            if now - start >= self.max_wait:
                state["phase"] = RUNNING
                yield self._remember(state, "assistant", self._tr(state, "still_working"))
                return
            await asyncio.sleep(self.poll_interval)
            now = loop.time()
            if now - last >= self.progress_interval and progress < self.max_progress:
                last = now
                yield self._remember(state, "assistant", self._tr(state, f"progress{progress % 3}"))
                progress += 1

        state["phase"] = IDLE
        if state["deferred_human"] and closing:
            # The AI part is done; what is left is the transfer the customer agreed to.
            async for chunk in self._transfer(state, key):
                yield chunk
        elif closing_goodbye:
            yield await self._say(state, "Say a brief, warm goodbye; the call is ending.", self._tr(state, "goodbye"))
        elif not failed and closing:
            yield self._remember(state, "assistant", self._tr(state, "closing"))
