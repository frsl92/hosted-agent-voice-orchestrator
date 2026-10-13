import json

import pytest

from decision import Confirmation, TurnAnalysis
from lang import CALL_START
from orchestrator import BILLING, ORDERS, ROUTER, Orchestrator, new_state


class FakeDecision:
    """Scripted Decision-1: queues of analyses / guard results / verdicts, consumed in order."""

    def __init__(self, analyses=None, guards=None, verdicts=None):
        self.analyses = list(analyses or [])
        self.guards = list(guards or [])
        self.verdicts = list(verdicts or [])
        self.analyze_calls = 0
        self.confirm_calls = 0
        self.guard_calls = []

    async def analyze_turn(self, transcript):
        self.analyze_calls += 1
        return self.analyses.pop(0) if self.analyses else TurnAnalysis()

    async def confirmation(self, transcript):
        self.confirm_calls += 1
        item = self.verdicts.pop(0) if self.verdicts else "neither"
        return Confirmation(*((item, 0.0) if isinstance(item, str) else item))

    async def on_topic(self, transcript, topic):
        self.guard_calls.append(topic)
        return self.guards.pop(0) if self.guards else (0.0, 0.0, True)


class FakeRunner:
    """Records which agent spoke with which directive; optionally raises return_to_router."""

    def __init__(self, returns=()):
        self.calls = []
        self.returns = list(returns)  # agent names that call return_to_router

    async def stream(self, agent, history, directive, signals, state=None):
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
    s = new_state()
    s["lang"] = "en"
    return s


def make(analyses=None, guards=None, returns=(), verdicts=None):
    decision = FakeDecision(analyses, guards, verdicts)
    runner = FakeRunner(returns)
    return Orchestrator(decision, runner), decision, runner


async def test_greeting_stays_in_router(state):
    orch, _, runner = make([analysis()])
    out = await say(orch, state, "Hi")
    assert out == "[router] ok."
    assert state["active_agent"] == ROUTER
    assert state["greeted"] is True
    assert "Greet" in runner.calls[0]["directive"]


async def test_single_topic_is_proposed_not_handed_off(state):
    orch, _, runner = make([analysis(billing=0.95, primary="billing")])
    await say(orch, state, "Why is my bill so high?")
    assert state["active_agent"] == ROUTER
    assert state["phase"] == "proposing"
    assert state["proposed"] == ["billing"]
    assert [c["agent"] for c in runner.calls] == [ROUTER]
    assert "Do NOT answer yet" in runner.calls[0]["directive"]


async def test_two_topics_are_proposed_together(state):
    orch, _, runner = make([analysis(billing=0.9, orders=0.9, primary="order_status")])
    await say(orch, state, "where is order 12345 and why is my bill high")
    assert state["active_agent"] == ROUTER
    assert state["proposed"] == ["order_status", "billing"]
    assert "order status and billing" in runner.calls[0]["directive"]
    assert [c["agent"] for c in runner.calls] == [ROUTER]


async def test_confirmed_topics_run_one_after_another_in_one_turn(state):
    orch, decision, runner = make(
        [analysis(billing=0.9, orders=0.9, primary="order_status"), analysis()],
        returns=[ORDERS, BILLING],
        verdicts=["yes"],
    )
    await say(orch, state, "where is order 12345 and why is my bill high")
    out = await say(orch, state, "yes")
    assert decision.confirm_calls == 1
    assert [c["agent"] for c in runner.calls] == [ROUTER, ORDERS, BILLING, ROUTER]
    assert "order status" in runner.calls[1]["directive"]
    assert "Now, about your bill" in runner.calls[2]["directive"]
    assert "anything else" in runner.calls[3]["directive"]
    assert out == "[orders] ok.\n\n[billing] ok.\n\n[router] ok."
    assert state["active_agent"] == ROUTER
    assert state["phase"] == "idle" and state["proposed"] == [] and state["queue"] == []


async def test_confirmed_topics_continue_even_if_the_model_forgets_to_return(state):
    orch, _, runner = make(
        [analysis(billing=0.9, orders=0.9, primary="order_status"), analysis()], verdicts=["yes"]
    )
    await say(orch, state, "where is order 12345 and why is my bill high")
    out = await say(orch, state, "yes")
    assert [c["agent"] for c in runner.calls] == [ROUTER, ORDERS, BILLING, ROUTER]
    assert out == "[orders] ok.\n\n[billing] ok.\n\n[router] ok."


async def test_confirmed_sub_agent_waiting_for_a_detail_keeps_the_call(state):
    orch, _, runner = make([analysis(orders=0.95, primary="order_status")], verdicts=["yes"])
    runner.calls = []

    async def asking(agent, history, directive, signals, state=None):
        runner.calls.append({"agent": agent, "directive": directive, "history": history})
        yield "What is the order number?"

    runner.stream = asking
    await say(orch, state, "where is my order")
    await say(orch, state, "yes")
    assert [c["agent"] for c in runner.calls] == [ROUTER, ORDERS]
    assert state["active_agent"] == ORDERS


async def test_confirmed_single_topic_returns_to_the_router_when_done(state):
    orch, _, runner = make([analysis(billing=0.95, primary="billing"), analysis()], verdicts=["yes"])
    await say(orch, state, "why is my bill so high")
    await say(orch, state, "yes")
    assert [c["agent"] for c in runner.calls] == [ROUTER, BILLING, ROUTER]
    assert state["active_agent"] == ROUTER


async def test_rejected_proposal_replans_from_the_correction(state):
    orch, _, runner = make(
        [analysis(billing=0.9, orders=0.9, primary="order_status"), analysis(billing=0.9, primary="billing")],
        verdicts=["no"],
    )
    await say(orch, state, "order 12345 and my bill")
    await say(orch, state, "no, just the bill")
    assert state["active_agent"] == ROUTER
    assert state["proposed"] == ["billing"]
    assert [c["agent"] for c in runner.calls] == [ROUTER, ROUTER]


async def test_amended_proposal_keeps_every_topic_and_asks_again(state):
    orch, decision, runner = make(
        [analysis(billing=0.9, orders=0.9, primary="order_status")], verdicts=["amend"]
    )
    await say(orch, state, "order 12345 and my bill")
    await say(orch, state, "wait, the order is 3472")
    assert decision.analyze_calls == 1
    assert state["phase"] == "proposing"
    assert state["proposed"] == ["order_status", "billing"]
    assert [c["agent"] for c in runner.calls] == [ROUTER, ROUTER]
    assert "corrected a detail" in runner.calls[1]["directive"]


async def test_amended_then_confirmed_runs_both_topics(state):
    orch, _, runner = make(
        [analysis(billing=0.9, orders=0.9, primary="order_status"), analysis()],
        verdicts=["amend", "yes"],
    )
    await say(orch, state, "order 12345 and my bill")
    await say(orch, state, "wait, the order is 3472")
    await say(orch, state, "yes")
    assert [c["agent"] for c in runner.calls] == [ROUTER, ROUTER, ORDERS, BILLING, ROUTER]


async def test_rejected_proposal_without_new_topic_asks_what_to_change(state):
    orch, _, runner = make([analysis(billing=0.9, primary="billing"), analysis()], verdicts=["no"])
    await say(orch, state, "my bill")
    await say(orch, state, "no")
    assert state["phase"] == "idle" and state["proposed"] == []
    assert "no problem" in runner.calls[1]["directive"]


async def test_unclear_answer_reasks_then_opens_the_question(state):
    orch, _, runner = make(
        [analysis(billing=0.9, primary="billing")], verdicts=["neither", "neither", "neither"]
    )
    await say(orch, state, "my bill")
    await say(orch, state, "hmm")
    assert state["phase"] == "proposing" and "ask again" in runner.calls[1]["directive"]
    await say(orch, state, "well")
    assert state["phase"] == "proposing"
    await say(orch, state, "uh")
    assert state["phase"] == "idle" and state["proposed"] == []
    assert "open question" in runner.calls[3]["directive"]


async def test_yes_that_is_really_goodbye_ends_the_call(state):
    orch, _, runner = make(
        [analysis(billing=0.9, primary="billing"), analysis(end=0.95)], verdicts=[("yes", 0.95)]
    )
    await say(orch, state, "my bill")
    await say(orch, state, "ok thanks, that's all")
    assert state["ended"] is True
    assert state["phase"] == "idle"
    assert "goodbye" in runner.calls[1]["directive"]


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
    assert out == "[billing] ok.\n\n[orders] ok."
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


async def test_empty_input_in_portuguese():
    orch, _, _ = make()
    out = await say(orch, new_state(), "   ")
    assert "não percebi" in out


async def test_call_start_greets_from_router_without_decision(state):
    orch, decision, runner = make()
    out = await say(orch, state, CALL_START)
    assert out == "[router] ok."
    assert decision.analyze_calls == 0
    assert "call-center operator" in runner.calls[0]["directive"]
    assert runner.calls[0]["history"][-1]["role"] == "user"  # synthetic cue, hidden afterwards
    assert [m["role"] for m in state["history"]] == ["assistant"]
    assert state["greeted"] is True and state["active_agent"] == ROUTER
    assert await say(orch, state, CALL_START) == ""  # never greets twice
    assert len(runner.calls) == 1


async def test_first_customer_turn_after_greeting_is_routed_normally(state):
    orch, _, runner = make([analysis(billing=0.95, primary="billing")])
    await say(orch, state, CALL_START)
    await say(orch, state, "Why is my bill so high?")
    assert [c["agent"] for c in runner.calls] == [ROUTER, ROUTER]
    assert state["phase"] == "proposing"
    assert [m["role"] for m in state["history"]] == ["assistant", "user", "assistant"]


async def test_language_follows_the_customer():
    orch, _, _ = make([analysis(), analysis()])
    state = new_state()
    await say(orch, state, "Olá, queria saber da minha fatura")
    assert state["lang"] == "pt"
    await say(orch, state, "Hello, what is my bill please")
    assert state["lang"] == "en"


async def test_state_json_serializable_and_history_trimmed(state):
    orch, _, _ = make([analysis(billing=0.9, primary="billing")])
    await say(orch, state, "bill?")
    state["active_agent"] = BILLING
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
    assert a["phase"] == "proposing"
    assert b["phase"] == "idle"
    assert b["proposed"] == []
    assert b["history"] == []
