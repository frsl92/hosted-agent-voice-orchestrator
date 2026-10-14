from types import SimpleNamespace as NS

import pytest

import remote_runner as rr
from orchestrator import BILLING, ORDERS, ROUTER, TurnSignals, new_state


def resp(rid="r1", session="S1", usage=None):
    r = NS(id=rid, usage=usage, model_extra={"agent_session_id": session} if session else {})
    return r


def ev(kind, **kw):
    return NS(type=kind, **kw)


def script(text_parts, tools=(), rid="r1", session="S1"):
    events = [ev("response.created", response=resp(rid, session))]
    for name in tools:
        events.append(ev("response.output_item.added", item=NS(type="function_call", name=name, call_id=f"c-{name}", arguments="")))
        events.append(ev("response.output_item.done", item=NS(type="function_call", name=name, call_id=f"c-{name}", arguments='{"a":1}')))
        events.append(ev("response.output_item.done", item=NS(type="function_call_output", call_id=f"c-{name}", output=f"{name}-result")))
    events += [ev("response.output_text.delta", delta=t) for t in text_parts]
    usage = NS(input_tokens=10, output_tokens=5)
    events.append(ev("response.completed", response=resp(rid, session, usage)))
    return events


class FakeResponses:
    def __init__(self, scripts):
        self.scripts, self.calls = list(scripts), []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item

        async def gen():
            for e in item:
                yield e

        return gen()


def make(scripts):
    responses = FakeResponses(scripts)
    clients = {}

    def openai_for(name):
        return clients.setdefault(name, NS(responses=responses))

    class Local:
        def stream(self, *a, **k):
            raise AssertionError("router must stay local")

    runner = rr.RemoteSubAgentRunner(Local(), openai_for)
    return runner, responses


async def run(runner, agent, history, directive, state, signals=None):
    signals = signals or TurnSignals()
    return "".join([c async for c in runner.stream(agent, history, directive, signals, state)]), signals


H1 = [{"role": "user", "text": "estado da encomenda 12345"}]


async def test_first_call_creates_session_and_streams_text():
    runner, responses = make([script(["A encomenda ", "foi enviada."], tools=["lookup_order"])])
    state = new_state()
    state["history"] = list(H1)
    text, _ = await run(runner, ORDERS, H1, None, state)
    assert text == "A encomenda foi enviada."
    assert "agent_session_id" not in responses.calls[0].get("extra_body", {})
    assert state["remote"][ORDERS] == {"agent_session_id": "S1", "previous_response_id": "r1", "synced": 1}


async def test_second_call_reuses_the_same_session():
    runner, responses = make([script(["Um."], session="S1", rid="r1"), script(["Dois."], session="S1", rid="r2")])
    state = new_state()
    await run(runner, ORDERS, H1, None, state)
    history = H1 + [{"role": "assistant", "text": "Um."}, {"role": "user", "text": "e a 67890?"}]
    state["history"] = list(history)
    await run(runner, ORDERS, history, None, state)
    second = responses.calls[1]
    assert second["extra_body"] == {"agent_session_id": "S1"}
    assert second["previous_response_id"] == "r1"
    assert state["remote"][ORDERS]["previous_response_id"] == "r2"


async def test_each_sub_agent_has_its_own_session():
    runner, _ = make([script(["Fatura."], session="B1"), script(["Encomenda."], session="O1")])
    state = new_state()
    await run(runner, BILLING, H1, None, state)
    await run(runner, ORDERS, H1, None, state)
    assert state["remote"][BILLING]["agent_session_id"] == "B1"
    assert state["remote"][ORDERS]["agent_session_id"] == "O1"


async def test_return_to_router_call_sets_the_signal():
    runner, _ = make([script(["Pronto."], tools=["return_to_router"])])
    _, signals = await run(runner, BILLING, H1, None, new_state())
    assert signals.return_requested


async def test_tool_calls_are_relayed_as_trace_events(caplog):
    runner, _ = make([script(["Ok."], tools=["lookup_order"])])
    with caplog.at_level("INFO", logger="calltrace"):
        await run(runner, ORDERS, H1, None, new_state())
    messages = [r.getMessage() for r in caplog.records]
    assert any('"kind": "tool.call"' in m and "lookup_order-result" in m and "orders-agent" in m for m in messages)
    assert any('"kind": "tool.call"' in m and '"args": {"a": 1}' in m for m in messages)
    assert any('"kind": "subagent.call"' in m for m in messages)
    assert any('"kind": "usage"' in m for m in messages)


async def test_expired_session_is_replaced_with_a_fresh_one():
    runner, responses = make([RuntimeError("404 session not found"), script(["Ola."], session="S2", rid="r9")])
    state = new_state()
    state["history"] = list(H1)
    state["remote"][ORDERS] = {"agent_session_id": "OLD", "previous_response_id": "rOLD", "synced": 1}
    text, _ = await run(runner, ORDERS, H1, None, state)
    assert text == "Ola."
    assert "extra_body" not in responses.calls[1] and "previous_response_id" not in responses.calls[1]
    assert state["remote"][ORDERS]["agent_session_id"] == "S2"


async def test_delegated_user_identity_is_stable_for_the_conversation():
    runner, responses = make([script(["Um."], session="S1", rid="r1"), script(["Dois."], session="S2", rid="r2")])
    runner._delegate_user = True
    state = new_state()
    await run(runner, ORDERS, H1, None, state)
    await run(runner, BILLING, H1, None, state)
    headers = [c["extra_headers"][rr.USER_IDENTITY_HEADER] for c in responses.calls]
    assert headers[0] == headers[1] == state["caller_id"]


async def test_no_user_identity_header_unless_delegation_is_on():
    runner, responses = make([script(["Um."])])
    await run(runner, ORDERS, H1, None, new_state())
    assert "extra_headers" not in responses.calls[0]


async def test_router_stays_local():
    runner, _ = make([])
    with pytest.raises(AssertionError):
        runner.stream(ROUTER, H1, None, TurnSignals(), new_state())


def test_build_input_plain_follow_up_is_just_the_customer_text():
    history = H1 + [{"role": "assistant", "text": "Um."}, {"role": "user", "text": "e a 67890?"}]
    assert rr.build_input(history, synced=2, directive=None) == "e a 67890?"


def test_build_input_includes_unseen_conversation_and_directive():
    history = [
        {"role": "assistant", "text": "Olá."},
        {"role": "user", "text": "fatura e encomenda 12345"},
        {"role": "assistant", "text": "Confirma?"},
        {"role": "user", "text": "sim"},
    ]
    out = rr.build_input(history, synced=0, directive="Check billing.")
    assert out.startswith("Instruction for this turn: Check billing.")
    assert "Customer: fatura e encomenda 12345" in out and "Agent: Confirma?" in out
    assert out.endswith("Customer's latest message: sim")
