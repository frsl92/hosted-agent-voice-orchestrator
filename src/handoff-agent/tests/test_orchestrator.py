import json

import pytest

from decision import TurnAnalysis
from orchestrator import BILLING, ORDERS, ROUTER, Orchestrator, new_state


class FakeDecision:
    """Scripted Decision-1: queues of analyses / guard results, consumed in order."""

    def __init__(self, analyses=None, guards=None):
        self.analyses = list(analyses or [])
        self.guards = list(guards or [])
        self.analyze_calls = 0
        self.guard_calls = []

    async def analyze_turn(self, transcript):
        self.analyze_calls += 1
        return self.analyses.pop(0) if self.analyses else TurnAnalysis()

    async def on_topic(self, transcript, topic):
        self.guard_calls.append(topic)
        return self.guards.pop(0) if self.guards else (0.0, 0.0, True)


class FakeRunner:
    """Records which agent spoke with which directive; optionally raises return_to_router."""

    def __init__(self, returns=()):
        self.calls = []
        self.returns = list(returns)  # agent names that call return_to_router

    async def stream(self, agent, history, directive, signals):
        self.calls.append({"agent": agent, "directive": directive, "history": history})
        if agent in self.returns:
            self.returns.remove(agent)
            signals.return_requested = True
        yield f"[{agent}]"
        yield " ok."


def analysis(billing=0.0, orders=0.0, primary="unclear", end=0.0, conf=0.9, ok=True):
    return TurnAnalysis(
        billing=billing, order_status=orders, primary_topic=primary, primary_confidence=conf, wants_to_end=end, ok=ok
    )


async def say(orch, state, text):
    return "".join([c async for c in orch.handle_turn(state, text)])


@pytest.fixture
def state():
    return new_state()


def make(analyses=None, guards=None, returns=()):
    decision = FakeDecision(analyses, guards)
    runner = FakeRunner(returns)
    return Orchestrator(decision, runner), decision, runner


async def test_greeting_stays_in_router(state):
    orch, _, runner = make([analysis()])
    out = await say(orch, state, "Hi")
    assert out == "[router] ok."
    assert state["active_agent"] == ROUTER
    assert state["greeted"] is True
    assert "Greet" in runner.calls[0]["directive"]


async def test_single_topic_handoff(state):
    orch, _, runner = make([analysis(billing=0.95, primary="billing")])
    await say(orch, state, "Why is my bill so high?")
    assert state["active_agent"] == BILLING
    assert state["queue"] == []
    assert [c["agent"] for c in runner.calls] == [BILLING]


async def test_two_topics_primary_first_and_queued(state):
    orch, _, runner = make([analysis(billing=0.9, orders=0.9, primary="order_status")])
    await say(orch, state, "where is order 12345 and why is my bill high")
    assert state["active_agent"] == ORDERS
    assert state["queue"] == ["billing"]
    assert "billing" in runner.calls[0]["directive"]


async def test_guard_moves_other_topic_to_queue(state):
    state["active_agent"] = BILLING
    orch, _, runner = make(guards=[(0.9, 0.1, True)])
    await say(orch, state, "also where is my order")
    assert state["active_agent"] == BILLING
    assert state["queue"] == ["order_status"]
    assert "order status" in runner.calls[0]["directive"]


async def test_resolved_goes_to_next_queued(state):
    state["active_agent"] = BILLING
    state["queue"] = ["order_status"]
    orch, _, runner = make(guards=[(0.0, 0.9, True)])
    await say(orch, state, "ok thanks")
    assert state["active_agent"] == ORDERS
    assert state["queue"] == []
    assert "Now, about your order" in runner.calls[0]["directive"]


async def test_resolved_returns_to_router_asks_anything_else(state):
    state["active_agent"] = ORDERS
    orch, decision, runner = make([analysis()], guards=[(0.0, 0.9, True)])
    await say(orch, state, "great thanks")
    assert state["active_agent"] == ROUTER
    assert runner.calls[0]["agent"] == ROUTER
    assert "anything else" in runner.calls[0]["directive"]
    assert decision.analyze_calls == 1


async def test_resolved_and_wants_to_end_says_goodbye(state):
    state["active_agent"] = ORDERS
    orch, _, runner = make([analysis(end=0.95)], guards=[(0.0, 0.9, True)])
    await say(orch, state, "thanks that's all")
    assert state["ended"] is True
    assert "goodbye" in runner.calls[0]["directive"]


async def test_wants_to_end_at_router_goodbye(state):
    orch, _, runner = make([analysis(end=0.95)])
    await say(orch, state, "no thanks that's all")
    assert state["ended"] is True
    assert state["active_agent"] == ROUTER
    assert "goodbye" in runner.calls[0]["directive"]
    # a new utterance after the goodbye starts a fresh exchange
    orch._decision.analyses.append(analysis())
    await say(orch, state, "actually hello again")
    assert state["ended"] is False


async def test_wants_to_end_wins_over_context_contaminated_topics(state):
    state["greeted"] = True
    orch, _, runner = make([analysis(billing=0.97, orders=0.98, primary="billing", end=1.0)])
    await say(orch, state, "thanks, I'm done")
    assert state["ended"] is True
    assert state["active_agent"] == ROUTER
    assert "goodbye" in runner.calls[0]["directive"]


async def test_decision_failure_stays_with_current_agent_sub(state):
    state["active_agent"] = BILLING
    orch, _, runner = make(guards=[(0.0, 0.0, False)])
    out = await say(orch, state, "and the roaming charge?")
    assert out == "[billing] ok."
    assert state["active_agent"] == BILLING


async def test_decision_failure_at_router_asks_clarifying(state):
    state["greeted"] = True
    orch, _, runner = make([analysis(ok=False)])
    await say(orch, state, "something")
    assert state["active_agent"] == ROUTER
    assert "clarifying" in runner.calls[0]["directive"]


async def test_out_of_scope_topic(state):
    state["greeted"] = True
    orch, _, runner = make([analysis(primary="other")])
    await say(orch, state, "what's the weather")
    assert "only help with billing" in runner.calls[0]["directive"]
    assert state["active_agent"] == ROUTER


async def test_return_tool_with_queue_hands_off_same_turn(state):
    state["active_agent"] = BILLING
    state["queue"] = ["order_status"]
    orch, _, runner = make(guards=[(0.0, 0.1, True)], returns=[BILLING])
    out = await say(orch, state, "what about the roaming charge")
    assert [c["agent"] for c in runner.calls] == [BILLING, ORDERS]
    assert out == "[billing] ok. [orders] ok."
    assert state["active_agent"] == ORDERS
    assert state["queue"] == []
    # second agent sees the first agent's reply
    assert runner.calls[1]["history"][-1]["text"] == "[billing] ok."


async def test_return_tool_no_queue_goes_to_router(state):
    state["active_agent"] = ORDERS
    orch, _, runner = make(guards=[(0.0, 0.1, True)], returns=[ORDERS])
    await say(orch, state, "where is 12345")
    assert [c["agent"] for c in runner.calls] == [ORDERS, ROUTER]
    assert state["active_agent"] == ROUTER


async def test_no_bounce_back_max_one_handoff_per_turn(state):
    # billing resolved -> orders (1 handoff); orders immediately returns -> must defer, not bounce
    state["active_agent"] = BILLING
    state["queue"] = ["order_status"]
    orch, _, runner = make(guards=[(0.0, 0.9, True)], returns=[ORDERS])
    await say(orch, state, "thanks")
    assert [c["agent"] for c in runner.calls] == [ORDERS]
    assert state["active_agent"] == ORDERS
    assert state["pending_return"] is True
    # next turn: the customer just acknowledges, so the deferred return is honoured
    orch._decision.guards.append((0.0, 0.9, True))
    orch._decision.analyses.append(analysis())
    await say(orch, state, "ok")
    assert state["active_agent"] == ROUTER
    assert state["pending_return"] is False


async def test_deferred_return_cancelled_by_follow_up_question(state):
    state["active_agent"] = ORDERS
    state["pending_return"] = True
    orch, _, runner = make(guards=[(0.0, 0.0, True)])
    await say(orch, state, "what's the tracking number?")
    assert [c["agent"] for c in runner.calls] == [ORDERS]
    assert state["active_agent"] == ORDERS
    assert state["pending_return"] is False


async def test_runner_failure_gives_fallback(state):
    class Boom:
        async def stream(self, *a, **k):
            raise RuntimeError("llm down")
            yield ""

    orch = Orchestrator(FakeDecision([analysis()]), Boom())
    out = await say(orch, state, "hi")
    assert "Sorry" in out


async def test_empty_input(state):
    orch, _, runner = make()
    out = await say(orch, state, "   ")
    assert "didn't catch" in out
    assert runner.calls == []


async def test_state_json_serializable_and_history_trimmed(state):
    orch, _, _ = make([analysis(billing=0.9, primary="billing")])
    await say(orch, state, "bill?")
    for i in range(40):
        state["history"].append({"role": "user", "text": f"x{i}"})
    orch._decision.guards.append((0.0, 0.0, True))
    await say(orch, state, "more")
    assert len(state["history"]) <= 30
    assert json.loads(json.dumps(state)) == state


async def test_state_isolated_between_sessions():
    orch, _, _ = make([analysis(billing=0.9, primary="billing")])
    a, b = new_state(), new_state()
    await say(orch, a, "bill?")
    assert a["active_agent"] == BILLING
    assert b["active_agent"] == ROUTER
    assert b["history"] == []
