from __future__ import annotations

import json

from agent_framework import AgentSession, Message

from conftest import Harness, analysis
from main import BackgroundCallCenterAgent


async def test_wrapper_stream_and_nonstream_with_session_state():
    h = Harness()
    agent = BackgroundCallCenterAgent(h.fg)
    session = agent.create_session()
    h.decision.analyses.append(analysis(billing=0.9, primary="billing"))

    resp = await agent.run([Message("user", ["my bill please"])], session=session)
    assert resp.text
    assert session.state["phase"] == "proposing"
    json.dumps(session.state)

    h.decision.confirmations.append("yes")
    texts = []
    async for upd in agent.run([Message("user", ["yes"])], stream=True, session=session):
        texts.append(upd.text)
    assert any("result-billing" in t for t in texts)
    assert texts[1].startswith(" ")
    assert session.state["phase"] == "idle"


async def test_wrapper_sessions_get_get_session():
    agent = BackgroundCallCenterAgent(Harness().fg)
    s = agent.get_session("svc-1", session_id="abc")
    assert isinstance(s, AgentSession) and s.service_session_id == "svc-1"
