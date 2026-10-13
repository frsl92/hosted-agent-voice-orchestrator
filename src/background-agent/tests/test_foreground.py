from __future__ import annotations

import asyncio

from conftest import Harness, Runner, analysis
from foreground import IDLE, PROPOSING, RUNNING, new_state
from lang import CALL_START, detect
from lang import t as tr
from llm import Extraction
from task_store import TaskStatus

CLOSING = tr("closing", "en")


async def propose_both(h: Harness):
    h.decision.analyses.append(analysis(billing=0.9, orders=0.9, primary="billing"))
    return await h.say("why is my bill so high and where is order 12345?")


async def test_propose_before_start_no_worker_without_yes(h: Harness):
    out = await propose_both(h)
    assert h.state["phase"] == PROPOSING
    assert len(out) == 1
    assert h.runner.started == [] and h.store.tasks("s1") == []
    assert h.decision.confirm_calls == 0
    h.assert_serializable()


async def test_two_tasks_in_one_utterance_proposed_together(h: Harness):
    await propose_both(h)
    topics = [p["topic"] for p in h.state["proposed"]]
    assert topics == ["billing", "order_status"]
    assert h.state["proposed"][1]["params"]["order_id"] == "12345"
    assert len(h.llm.said) == 1  # a single confirmation question


async def test_confirm_yes_launches_runs_delivers_once_and_closes(h: Harness):
    await propose_both(h)
    h.decision.confirmations.append("yes")
    out = await h.say("yes")
    assert h.decision.analyze_calls == 1  # only the proposal's analysis; confirming costs one call
    assert h.decision.confirm_calls == 1
    assert out[0].startswith("Give me a moment")
    joined = " ".join(out)
    assert joined.count("result-billing") == 1 and joined.count("result-order_status") == 1
    results = [c for c in out if "result-" in c]
    assert len(results) == 2 and "first answer" in results[0] and results[1].startswith("Now, on")
    assert out[-1] == CLOSING
    assert h.state["phase"] == IDLE
    assert sorted(t for t, _ in h.runner.started) == ["billing", "order_status"]
    assert h.store.collect_completed("s1") == []  # nothing left to deliver
    assert len(h.state["task_ids"]) == 2
    h.assert_serializable()


async def test_confirm_no_reproposes_with_correction(h: Harness):
    await propose_both(h)
    h.decision.confirmations.append("no")
    h.decision.analyses.append(analysis(orders=0.9, primary="order_status"))
    h.llm.extraction = Extraction(order_id="67890")
    await h.say("no, the order is 67890")
    assert h.runner.started == []
    assert h.state["phase"] == PROPOSING
    assert [p["topic"] for p in h.state["proposed"]] == ["order_status"]
    assert h.state["proposed"][0]["params"]["order_id"] == "67890"


async def test_confirm_no_without_topic_asks_what_to_change(h: Harness):
    await propose_both(h)
    h.decision.confirmations.append("no")
    await h.say("hmm no")
    assert h.state["phase"] == IDLE and h.state["proposed"] == []
    assert h.runner.started == []
    assert "change" in h.llm.said[-1]


async def test_yes_with_confident_end_does_not_launch(h: Harness):
    await propose_both(h)
    h.decision.confirmations.append(("yes", 0.95))
    h.decision.analyses.append(analysis(end=0.95))
    out = await h.say("ok never mind, that's all")
    assert h.runner.started == []
    assert len(out) == 1 and "goodbye" in h.llm.said[-1]
    assert h.state["phase"] == IDLE and h.state["proposed"] == []


async def test_confirm_neither_reasks_twice_then_open_question(h: Harness):
    await propose_both(h)
    for _ in range(2):
        await h.say("what's the weather")
        assert h.state["phase"] == PROPOSING
        assert "again" in h.llm.said[-1]
    await h.say("blah")
    assert h.state["phase"] == IDLE and h.state["proposed"] == []
    assert "open question" in h.llm.said[-1]
    assert h.runner.started == []


async def test_missing_order_id_is_asked_not_proposed(h: Harness):
    h.llm.extraction = Extraction(order_id="")
    h.decision.analyses.append(analysis(orders=0.9, primary="order_status"))
    await h.say("where is my order?")
    assert h.state["phase"] == IDLE and h.state["proposed"] == []
    assert h.state["pending_topics"] == ["order_status"]
    assert "order number" in h.llm.said[-1]

    # customer gives the number; analysis alone may not detect the topic, pending does
    h.llm.extraction = Extraction(order_id="12345")
    await h.say("12345")
    assert h.state["phase"] == PROPOSING
    assert h.state["proposed"][0]["params"]["order_id"] == "12345"
    assert h.state["pending_topics"] == []
    assert h.runner.started == []


async def test_wants_to_end_says_goodbye(h: Harness):
    h.decision.analyses.append(analysis(end=0.95))
    out = await h.say("no thanks, that's all")
    assert len(out) == 1 and "goodbye" in h.llm.said[-1]
    assert h.runner.started == []


async def test_decision_failure_falls_back_to_llm(h: Harness):
    h.decision.analyses.append(analysis(ok=False))
    out = await h.say("hello?")
    assert len(out) == 1 and h.state["phase"] == IDLE
    assert h.runner.started == []


async def test_llm_failure_uses_canned_text_never_crashes(h: Harness):
    h.llm.fail = True
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    out = await h.say("my bill?")
    assert "Is that right" in out[0]
    assert h.state["phase"] == PROPOSING


async def test_no_topic_small_talk(h: Harness):
    out = await h.say("hi")
    assert len(out) == 1 and h.state["phase"] == IDLE
    assert "bill and order status" in h.llm.said[-1]


async def test_task_failure_reported_gracefully():
    h = Harness(Runner(fail_topics=("billing",)))
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    await h.say("my bill")
    h.decision.confirmations.append("yes")
    out = await h.say("yes")
    assert any("couldn't finish" in c for c in out)
    assert h.state["phase"] == IDLE
    assert CLOSING not in out


async def test_cancel_running_tasks_on_explicit_request():
    runner = Runner()
    runner.gate = asyncio.Event()  # workers never finish unless released
    h = Harness(runner, max_wait=0.1)
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    await h.say("my bill")
    h.decision.confirmations.append("yes")
    out = await h.say("yes")
    assert "still working" in out[-1]
    assert h.state["phase"] == RUNNING and len(h.store.active("s1")) == 1

    await h.say("actually cancel that")
    assert h.state["phase"] == IDLE
    assert h.store.active("s1") == []
    assert h.store.tasks("s1")[0].status == TaskStatus.CANCELLED


async def test_running_tasks_survive_wait_timeout_and_are_reported_next_turn():
    runner = Runner()
    runner.gate = asyncio.Event()
    h = Harness(runner, max_wait=0.05)
    h.decision.analyses.append(analysis(orders=0.9, primary="order_status"))
    await h.say("order 12345?")
    h.decision.confirmations.append("yes")
    await h.say("yes")
    assert h.state["phase"] == RUNNING

    runner.gate.set()
    await h.store.drain()
    out = await h.say("hello?")  # next turn: result delivered exactly once
    assert out[0].count("result-order_status") == 1
    again = await h.say("hello again")
    assert not any("result-" in c for c in again)
    assert h.state["phase"] == IDLE


async def test_status_request_while_running_reports_and_waits():
    runner = Runner(delay=0.15)
    h = Harness(runner, max_wait=0.05)
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    await h.say("my bill")
    h.decision.confirmations.append("yes")
    await h.say("yes")  # times out waiting, still running
    h.fg.max_wait = 5
    out = await h.say("any news?")
    assert "still checking your bill" in out[0]
    assert any("result-billing" in c for c in out)
    assert out[-1] == CLOSING


async def test_already_running_task_not_relaunched():
    runner = Runner()
    runner.gate = asyncio.Event()
    h = Harness(runner, max_wait=0.05)
    h.decision.analyses.append(analysis(orders=0.9, primary="order_status"))
    h.llm.extraction = Extraction(order_id="12345")
    await h.say("order 12345?")
    h.decision.confirmations.append("yes")
    await h.say("yes")
    h.fg.max_wait = 0.05
    h.decision.analyses.append(analysis(orders=0.9, primary="order_status"))
    out = await h.say("order 12345 again")
    assert h.state["phase"] == RUNNING  # no second proposal
    assert len(h.store.tasks("s1")) == 1
    assert any("already checking" in c for c in out)


async def test_progress_updates_limited_and_streamed():
    runner = Runner(delay=0.4)
    h = Harness(runner, progress_interval=0.05, max_progress=2)
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    await h.say("bill")
    h.decision.confirmations.append("yes")
    out = await h.say("yes")
    progress = [c for c in out if "Still" in c or "Almost" in c]
    assert len(progress) == 2


async def test_disconnect_does_not_cancel_workers():
    runner = Runner()
    runner.gate = asyncio.Event()
    h = Harness(runner)
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    await h.say("bill")
    h.decision.confirmations.append("yes")

    async def consume():
        async for _ in h.fg.handle("yes", h.state, h.key):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(h.store.active("s1")) == 1
    runner.gate.set()
    await h.store.drain()
    assert [t.status for t in h.store.tasks("s1")] == [TaskStatus.COMPLETED]


async def test_state_json_serializable_and_sessions_isolated():
    a, b = Harness(), Harness()
    b.store = a.store  # shared store, different keys/states
    b.fg.store = a.store
    b.key = "s2"
    a.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    await a.say("my bill")
    a.decision.confirmations.append("yes")
    await a.say("yes")
    a.assert_serializable()
    assert b.state == {**new_state(), "lang": "en"}
    out = await b.say("hi")
    assert not any("result-" in c for c in out)
    assert a.store.collect_completed("s2") == []


async def test_history_is_capped():
    h = Harness()
    for i in range(40):
        await h.say(f"hello {i}")
    assert len(h.state["history"]) <= 30


async def test_call_start_greets_without_decision_or_user_history():
    h = Harness(lang="pt")
    out = await h.say(CALL_START)
    assert len(out) == 1 and out[0].startswith("SAY[")
    assert "greet" in h.llm.said[-1].lower()
    assert h.decision.analyze_calls == 0
    assert [m["role"] for m in h.state["history"]] == ["assistant"]
    assert await h.say(CALL_START) == []  # never greets twice


async def test_greeting_falls_back_to_portuguese_when_llm_down():
    h = Harness(lang="pt")
    h.llm.fail = True
    out = await h.say(CALL_START)
    assert out[0].startswith("Olá")


async def test_portuguese_flow_uses_portuguese_fixed_strings():
    h = Harness(lang="pt")
    h.llm.fail = True
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    out = await h.say("Quero saber porque a minha fatura está tão alta")
    assert "Está correto" in out[0] and "fatura" in out[0]
    h.decision.confirmations.append("yes")
    out = await h.say("sim")
    assert out[0] == tr("ack", "pt")
    assert out[-1] == tr("closing", "pt")


async def test_language_switches_to_english_per_utterance():
    h = Harness(lang="pt")
    h.llm.fail = True
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))
    out = await h.say("Hello, I want to know why my bill is so high")
    assert h.state["lang"] == "en" and "Is that right" in out[0]


def test_detect_language():
    assert detect("Olá, queria saber o estado da minha encomenda") == "pt"
    assert detect("Hi, where is my order?") == "en"
    assert detect("12345", "en") == "en" and detect("12345", "pt") == "pt"


async def test_cancel_in_portuguese():
    h = Harness(Runner(delay=5), lang="pt", max_wait=0.05)
    await propose_both(h)
    h.decision.confirmations.append("yes")
    await h.say("sim")
    assert len(h.store.active("s1")) == 2
    await h.say("deixa estar, cancela isso")
    assert h.store.active("s1") == []
