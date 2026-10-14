from agent_framework import AgentSession, Message

from host_agent import CallCenterAgent
from orchestrator import Orchestrator
from tests.test_orchestrator import FakeDecision, FakeRunner, analysis


def make_agent():
    return CallCenterAgent(
        Orchestrator(FakeDecision([analysis(billing=0.9, primary="billing")], verdicts=["yes"]), FakeRunner())
    )


async def test_run_non_stream_persists_state():
    agent = make_agent()
    session = agent.create_session(session_id="s1")
    resp = await agent.run([Message("user", ["why is my bill high"])], session=session)
    assert resp.text == "[router] ok."
    assert session.state["phase"] == "proposing"
    resp = await agent.run([Message("user", ["yes"])], session=session)
    assert resp.text == "[billing] ok.\n\n[router] ok."
    assert session.state["active_agent"] == "router"


async def test_run_stream_yields_updates():
    agent = make_agent()
    session = agent.create_session()
    await agent.run([Message("user", ["why is my bill high"])], session=session)
    stream = agent.run([Message("user", ["yes"])], stream=True, session=session)
    texts = [u.text async for u in stream]
    assert "".join(texts) == "[billing] ok.\n\n[router] ok."
    final = await stream.get_final_response()
    assert final.text == "[billing] ok.\n\n[router] ok."


def test_get_session():
    s = make_agent().get_session("svc", session_id="x")
    assert isinstance(s, AgentSession) and s.service_session_id == "svc"
