"""Per-session routing policy: which topics go to the AI workers, a human operator, or are declined."""

from __future__ import annotations

from conftest import Harness
from decision import TurnAnalysis
from foreground import IDLE, PROPOSING
from lang import CALL_START
from routing_policy import (
    DEFAULT_POLICY,
    HUMAN_REQUEST,
    REASON_CALLER,
    REASON_POLICY,
    call_start_message,
    is_call_start,
    normalize,
    partition,
    policy_from_call_start,
)

BILLING_HUMAN = {"billing": "human", "order_status": "ai_agent", "other": "decline"}
ORDERS_ONLY = {"billing": "decline", "order_status": "ai_agent", "other": "decline"}
OTHER_HUMAN = {"billing": "ai_agent", "order_status": "ai_agent", "other": "human"}


class RecordingHandoff:
    def __init__(self) -> None:
        self.requests: list[tuple[str, list[str]]] = []

    def request(self, state, reason, topics):
        self.requests.append((reason, list(topics)))
        return {}


async def started(policy: dict[str, str]) -> Harness:
    h = Harness()
    h.fg.human = RecordingHandoff()
    await h.say(call_start_message(policy))
    return h


def both() -> TurnAnalysis:
    return TurnAnalysis(billing=0.9, order_status=0.9, primary_topic="billing")


def test_normalize_and_partition():
    assert normalize("garbage") == DEFAULT_POLICY
    assert normalize({"other": "ai_agent"})["other"] == "decline"  # no AI specialist for "other"
    assert normalize("billing=human")["billing"] == "human"
    assert partition(BILLING_HUMAN, ["billing", "order_status", "other"]) == (["order_status"], ["billing"], ["other"])
    assert partition(None, ["billing", HUMAN_REQUEST]) == (["billing"], [HUMAN_REQUEST], [])


def test_call_start_envelope_roundtrip():
    msg = call_start_message(BILLING_HUMAN)
    assert is_call_start(msg) and is_call_start(CALL_START)
    assert policy_from_call_start(msg) == BILLING_HUMAN
    assert policy_from_call_start(CALL_START) == DEFAULT_POLICY
    assert not is_call_start("hello")


async def test_policy_is_snapshotted_from_the_call_start():
    h = await started(BILLING_HUMAN)
    assert h.state["policy"] == BILLING_HUMAN
    assert [m["role"] for m in h.state["history"]] == ["assistant"]


async def test_mixed_ai_and_human_answers_the_ai_topic_first_then_transfers():
    h = await started(BILLING_HUMAN)
    h.decision.analyses.append(both())
    await h.say("why is my bill so high and where is order 12345?")
    assert h.state["phase"] == PROPOSING
    assert [p["topic"] for p in h.state["proposed"]] == ["order_status"]
    assert h.state["proposed_human"] == ["billing"]
    assert h.fg.human.requests == []  # nothing is transferred before the customer confirms

    h.decision.confirmations.append("yes")
    out = await h.say("yes")
    assert [t for t, _ in h.runner.started] == ["order_status"]  # the billing worker never runs
    assert any("result-order_status" in c for c in out)
    assert h.fg.human.requests == [(REASON_POLICY, ["billing"])]
    assert h.state["phase"] == IDLE and not h.state["deferred_human"]


async def test_human_only_topic_is_confirmed_then_transferred_without_workers():
    h = await started(BILLING_HUMAN)
    h.decision.analyses.append(TurnAnalysis(billing=0.9, primary_topic="billing"))
    await h.say("how much do I owe this month?")
    assert h.state["proposed"] == [] and h.state["proposed_human"] == ["billing"]
    h.decision.confirmations.append("yes")
    await h.say("yes")
    assert h.runner.started == []
    assert h.fg.human.requests == [(REASON_POLICY, ["billing"])]


async def test_declined_topic_is_told_and_the_call_goes_on():
    h = await started(ORDERS_ONLY)
    h.decision.analyses.append(TurnAnalysis(billing=0.9, primary_topic="billing"))
    out = await h.say("how much is my bill?")
    assert out and h.state["phase"] == IDLE
    assert h.state["proposed"] == [] and h.fg.human.requests == [] and h.runner.started == []

    h.decision.analyses.append(TurnAnalysis(order_status=0.9, primary_topic="order_status"))
    await h.say("then check order 12345")
    assert [p["topic"] for p in h.state["proposed"]] == ["order_status"]


async def test_other_topic_follows_the_other_route():
    h = await started(OTHER_HUMAN)
    h.decision.analyses.append(TurnAnalysis(other=0.9, primary_topic="other", primary_confidence=0.9))
    await h.say("can you change my mobile plan?")
    assert h.state["proposed_human"] == ["other"]
    h.decision.confirmations.append("yes")
    await h.say("yes")
    assert h.fg.human.requests == [(REASON_POLICY, ["other"])]

    d = await started(DEFAULT_POLICY)
    d.decision.analyses.append(TurnAnalysis(other=0.9, primary_topic="other", primary_confidence=0.9))
    await d.say("can you change my mobile plan?")
    assert d.state["proposed_human"] == [] and d.fg.human.requests == []  # "other" is declined by default


async def test_caller_asking_for_a_person_transfers_immediately_without_confirmation():
    h = await started(DEFAULT_POLICY)
    h.decision.analyses.append(TurnAnalysis(billing=0.9, primary_topic="billing", wants_human=0.95))
    await h.say("I want to talk to a person about my bill")
    assert h.decision.confirm_calls == 0 and h.runner.started == []
    assert h.fg.human.requests == [(REASON_CALLER, [HUMAN_REQUEST])]


async def test_human_request_waits_for_running_tasks_first():
    h = await started(DEFAULT_POLICY)
    h.decision.analyses.append(TurnAnalysis(order_status=0.9, primary_topic="order_status"))
    await h.say("where is order 12345?")
    h.decision.confirmations.append("yes")
    await h.say("yes")
    assert [t for t, _ in h.runner.started] == ["order_status"]
    h.decision.analyses.append(TurnAnalysis(wants_human=0.9))
    await h.say("let me speak to someone")
    assert h.fg.human.requests == [(REASON_CALLER, [HUMAN_REQUEST])]


async def test_state_with_policy_stays_json_serializable():
    h = await started(BILLING_HUMAN)
    h.decision.analyses.append(both())
    await h.say("bill and order 12345")
    h.assert_serializable()
