"""Per-session routing policy: which topics go to a specialist, a human operator, or are declined."""

import json

import pytest

from decision import TurnAnalysis
from lang import CALL_START
from orchestrator import BILLING, ORDERS, ROUTER, Orchestrator, new_state
from routing_policy import call_start_message, is_call_start, partition, policy_from_call_start
from tests.test_orchestrator import FakeDecision, FakeRunner, say

BILLING_HUMAN = {"billing": "human", "order_status": "ai_agent", "other": "decline"}
ORDERS_ONLY = {"billing": "decline", "order_status": "ai_agent", "other": "decline"}
OTHER_HUMAN = {"billing": "ai_agent", "order_status": "ai_agent", "other": "human"}


class RecordingHandoff:
    def __init__(self):
        self.requests = []

    def request(self, state, reason, topics):
        self.requests.append((reason, list(topics)))
        return {}


def ta(billing=0.0, orders=0.0, primary="unclear", other=0.0, human=0.0, end=0.0):
    return TurnAnalysis(
        billing=billing, order_status=orders, primary_topic=primary, primary_confidence=0.9,
        wants_to_end=end, other=other, wants_human=human,
    )


@pytest.fixture
def state():
    s = new_state()
    s["lang"] = "en"
    return s


async def make(state, policy, analyses=None, guards=None, verdicts=None, returns=()):
    decision = FakeDecision(analyses, guards, verdicts)
    runner = FakeRunner(returns)
    orch = Orchestrator(decision, runner)
    orch._human = RecordingHandoff()
    await say(orch, state, call_start_message(policy))
    runner.calls.clear()
    return orch, decision, runner


def test_call_start_envelope_roundtrip():
    msg = call_start_message(BILLING_HUMAN)
    assert is_call_start(msg) and is_call_start(CALL_START) and not is_call_start("hello")
    assert policy_from_call_start(msg) == BILLING_HUMAN
    assert json.loads(msg.split("\n")[1].removeprefix("[policy]")) == BILLING_HUMAN


def test_partition_keeps_order_and_splits_by_route():
    assert partition(BILLING_HUMAN, ["billing", "order_status", "other"]) == (["order_status"], ["billing"], ["other"])


async def test_policy_is_snapshotted_at_call_start(state):
    await make(state, BILLING_HUMAN)
    assert state["policy"] == BILLING_HUMAN and state["greeted"]


async def test_mixed_request_answers_the_ai_topic_then_transfers_to_a_human(state):
    orch, decision, runner = await make(
        state, BILLING_HUMAN, [ta(billing=0.9, orders=0.9, primary="order_status"), ta()], verdicts=["yes"],
        returns=[ORDERS],
    )
    await say(orch, state, "where is order 12345 and why is my bill high")
    assert state["proposed"] == ["order_status", "billing"]
    assert "human" in runner.calls[0]["directive"].lower()
    await say(orch, state, "yes")
    agents = [c["agent"] for c in runner.calls]
    assert agents == [ROUTER, ORDERS, ROUTER]  # billing never reaches a specialist
    assert "transferring them to a human operator" in runner.calls[-1]["directive"]
    assert orch._human.requests == [("policy", ["billing"])]
    assert state["deferred_human"] == []


async def test_human_only_topic_is_confirmed_and_transferred(state):
    orch, _, runner = await make(state, BILLING_HUMAN, [ta(billing=0.9, primary="billing")], verdicts=["yes"])
    await say(orch, state, "how much do I owe")
    assert state["proposed"] == ["billing"]
    await say(orch, state, "yes")
    assert [c["agent"] for c in runner.calls] == [ROUTER, ROUTER]
    assert orch._human.requests == [("policy", ["billing"])]


async def test_declined_topic_is_told_and_the_call_goes_on(state):
    orch, _, runner = await make(state, ORDERS_ONLY, [ta(billing=0.9, primary="billing")])
    await say(orch, state, "how much is my bill")
    assert state["phase"] == "idle" and state["proposed"] == []
    assert "cannot" in runner.calls[0]["directive"].lower() or "can't" in runner.calls[0]["directive"].lower()
    assert orch._human.requests == []


async def test_other_topic_follows_its_route(state):
    orch, _, _ = await make(state, OTHER_HUMAN, [ta(other=0.9, primary="other")], verdicts=["yes"])
    await say(orch, state, "can you change my mobile plan")
    assert state["proposed"] == ["other"]
    await say(orch, state, "yes")
    assert orch._human.requests == [("policy", ["other"])]


async def test_other_topic_is_declined_by_default(state):
    orch, _, runner = await make(state, None, [ta(other=0.9, primary="other")])
    await say(orch, state, "can you change my mobile plan")
    assert state["proposed"] == [] and orch._human.requests == []


async def test_caller_asking_for_a_person_transfers_without_confirmation(state):
    orch, decision, runner = await make(state, None, [ta(billing=0.9, primary="billing", human=0.95)])
    await say(orch, state, "I want to talk to a person about my bill")
    assert decision.confirm_calls == 0
    assert orch._human.requests == [("caller_requested", ["human_request"])]
    assert "transferring them to a human operator right now" in runner.calls[-1]["directive"]


async def test_state_with_policy_is_json_serializable(state):
    orch, _, _ = await make(state, BILLING_HUMAN, [ta(billing=0.9, orders=0.9, primary="billing")])
    await say(orch, state, "bill and order 12345")
    json.dumps(state)
