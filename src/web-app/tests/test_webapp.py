import asyncio
import time
import types

import pytest
from azure.ai.voicelive.models import ServerEventType
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as webapp
import auth
from logtail import agent_session_id, parse_record
from samples import AGENTS, SAMPLES
from session import CALL_START, VoiceBridge, response_text, usage_of


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
    assert len(AGENTS) == 2
    assert len(SAMPLES) >= 5
    assert len({s["id"] for s in SAMPLES}) == len(SAMPLES)
    for s in SAMPLES:
        assert s["title"] and s["description"] and s["turns"]
        assert all(isinstance(t, str) and t.strip() for t in s["turns"])


def test_token_roundtrip_and_password_change():
    token = auth.make_token("secret")
    assert auth.token_valid(token, "secret")
    assert not auth.token_valid(token, "other")
    assert not auth.token_valid(None, "secret") and not auth.token_valid("garbage", "secret")
    assert not auth.token_valid(token, "secret", now=time.time() + auth.TTL_SECONDS + 10)
    assert not auth.token_valid(token, "")


def test_password_matches():
    assert auth.password_matches("abc", "abc")
    assert not auth.password_matches("abd", "abc")


def test_login_throttle():
    t = auth.LoginThrottle()
    for i in range(auth.MAX_FAILURES):
        assert not t.blocked("ip", now=100 + i)
        t.record_failure("ip", now=100 + i)
    assert t.blocked("ip", now=110)
    assert not t.blocked("ip", now=100 + auth.FAILURE_WINDOW + 10)
    assert not t.blocked("other", now=110)


def test_http_requires_password(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    with TestClient(webapp.app) as client:
        assert client.get("/healthz").status_code == 200
        assert "Password" in client.get("/login").text
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"
        assert client.get("/api/config").status_code == 401
        assert client.get("/static/app.js").status_code == 401

        assert client.post("/login", json={"password": "nope"}).status_code == 401
        assert client.post("/login", json={"password": "pw"}).status_code == 200

        cfg = client.get("/api/config").json()
        assert [a["id"] for a in cfg["agents"]] == [a["id"] for a in AGENTS]
        assert len(cfg["samples"]) == len(SAMPLES) and {m["id"] for m in cfg["modes"]} == {"text", "voice"}
        index = client.get("/")
        assert index.status_code == 200 and "Call center agent tester" in index.text
        assert client.get("/static/app.js").status_code == 200

        client.post("/logout")
        assert client.get("/api/config").status_code == 401


def test_login_is_throttled(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    with TestClient(webapp.app) as client:
        codes = [client.post("/login", json={"password": "bad"}).status_code for _ in range(auth.MAX_FAILURES + 1)]
        assert codes[-1] == 429
        assert client.post("/login", json={"password": "pw"}).status_code == 429


def test_no_password_configured_refuses_everything(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    with TestClient(webapp.app) as client:
        assert client.post("/login", json={"password": ""}).status_code == 401
        assert client.get("/api/config").status_code == 401


def test_websocket_requires_cookie_and_valid_params(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    with TestClient(webapp.app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/session?agent=handoff-agent&mode=text"):
                pass
        assert exc.value.code == 4401

        client.post("/login", json={"password": "pw"})
        for query in ("agent=nope&mode=text", "agent=handoff-agent&mode=hologram"):
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(f"/ws/session?{query}"):
                    pass
            assert exc.value.code == 4404

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/ws/session?agent=handoff-agent&mode=text", headers={"origin": "https://evil.example"}
            ):
                pass
        assert exc.value.code == 4403


def test_bridge_modes():
    text = VoiceBridge(None, None, None, endpoint="", project_name="", project_endpoint="", agent_name="a", mode="text")
    voice = VoiceBridge(None, None, None, endpoint="", project_name="", project_endpoint="", agent_name="a", mode="voice")
    assert not text.audio_out and voice.audio_out
    assert text._session_config().modalities == ["text"]
    cfg = voice._session_config()
    assert "audio" in cfg.modalities and cfg.input_audio_transcription.model == "azure-speech"
    assert cfg.input_audio_transcription.language.startswith("pt-PT") and "en-US" in cfg.input_audio_transcription.language
    assert cfg.voice.name.startswith("pt-PT")


def test_samples_are_portuguese():
    text = " ".join(t for s in SAMPLES for t in s["turns"]).lower()
    assert "encomenda" in text and "fatura" in text and "obrigado" in text
    assert "order" not in text.replace("encomenda", "")


class _Recorder:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def send_bytes(self, data):
        pass


class _Conn:
    def __init__(self):
        self.items = []
        self.responses = 0
        self.conversation = types.SimpleNamespace(item=types.SimpleNamespace(create=self._create))
        self.response = types.SimpleNamespace(create=self._respond)

    async def _create(self, item):
        self.items.append(item.content[0].text)

    async def _respond(self):
        self.responses += 1


def test_session_greets_once_after_update():
    sink = _Recorder()
    bridge = VoiceBridge(sink, None, None, endpoint="", project_name="", project_endpoint="", agent_name="a", mode="text")
    bridge._conn = _Conn()
    event = types.SimpleNamespace(type=ServerEventType.SESSION_UPDATED)

    async def go():
        await bridge._on_event(event)
        await bridge._on_event(event)

    asyncio.run(go())
    assert bridge._conn.items == [CALL_START] and bridge._conn.responses == 1
    assert [m for m in sink.sent if m["type"] == "greeting"] == [{"type": "greeting"}]
