from fastapi.testclient import TestClient

import app as webapp
from logtail import agent_session_id, parse_record
from samples import AGENTS, SAMPLES
from session import response_text, usage_of


def test_parse_trace_line():
    rec = {"message": 'INFO x: TRACE {"kind": "handoff", "t": 1234, "source": "router", "target": "billing"}'}
    ev = parse_record(rec)
    assert ev == {
        "source": "agent", "kind": "handoff", "t": 1234,
        "data": {"source": "router", "target": "billing"},
    }


def test_parse_plain_log_line_extracts_level():
    ev = parse_record({"message": "2026-10-12 13:53:18 WARNING foo: bar", "stream": "stderr",
                       "timestamp": "2026-10-12T13:53:18Z"})
    assert ev["source"] == "log" and ev["kind"] == "warning"
    assert ev["data"]["stream"] == "stderr"
    assert ev["t"] > 0


def test_parse_broken_trace_falls_back_to_log():
    ev = parse_record({"message": "TRACE {not json"})
    assert ev["source"] == "log"


def test_parse_non_message_record_ignored():
    assert parse_record({"session_state": "Running"}) is None


def test_agent_session_id():
    assert agent_session_id("sess_abc") == "vl_sess_abc"


def test_response_text_and_usage():
    resp = {
        "output": [{"content": [{"transcript": "Hello there."}, {"text": "More."}]}],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "other": 9},
    }
    assert response_text(resp) == "Hello there. More."
    assert usage_of(resp) == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    assert response_text(None) == "" and usage_of(None) == {}


def test_samples_are_valid():
    ids = {a["id"] for a in AGENTS}
    assert ids == set(SAMPLES) and len(ids) == 2
    for agent_id, samples in SAMPLES.items():
        assert len(samples) >= 5
        assert len({s["id"] for s in samples}) == len(samples)
        for s in samples:
            assert s["title"] and s["description"] and s["turns"]
            assert all(isinstance(t, str) and t.strip() for t in s["turns"])


def test_http_endpoints():
    with TestClient(webapp.app) as client:
        assert client.get("/healthz").status_code == 200
        cfg = client.get("/api/config").json()
        assert [a["id"] for a in cfg["agents"]] == [a["id"] for a in AGENTS]
        assert all(a["samples"] for a in cfg["agents"])
        assert "project" in cfg
        index = client.get("/")
        assert index.status_code == 200 and "Call center agent tester" in index.text
        assert client.get("/static/app.js").status_code == 200


def test_websocket_rejects_unknown_agent():
    with TestClient(webapp.app) as client:
        try:
            with client.websocket_connect("/ws/session?agent=nope") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "error"
        except Exception:
            pass
