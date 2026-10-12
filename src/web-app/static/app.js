"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: null, agent: null, sample: null, scriptIdx: 0,
  ws: null, connected: false, responding: false,
  t0: null, traces: [], handoffs: 0,
  micOn: false, ctx: null, micStream: null, micNode: null, playHead: 0, sources: new Set(),
  agentBubble: null, waiters: [],
};

// ------------------------------------------------------------------ setup

async function init() {
  state.config = await (await fetch("/api/config")).json();
  $("project").textContent = `Project ${state.config.project} · Foundry hosted agents · Voice Live · Microsoft-Decision-1`;
  renderAgents();
  selectAgent(state.config.agents[0].id);
  $("startBtn").onclick = startSession;
  $("endBtn").onclick = () => endSession("Session ended.");
  $("composer").onsubmit = (e) => { e.preventDefault(); sendText($("textInput").value); $("textInput").value = ""; };
  $("micBtn").onclick = toggleMic;
  $("cancelBtn").onclick = () => send({ type: "cancel" });
  $("speakToggle").onchange = () => send({ type: "audio_out", enabled: $("speakToggle").checked });
  $("scriptNext").onclick = scriptNext;
  $("scriptAll").onclick = scriptAll;
  $("scriptClose").onclick = () => { state.sample = null; $("script").hidden = true; renderSamples(); };
  $("traceClear").onclick = clearTrace;
  $("traceDownload").onclick = downloadTrace;
  document.querySelectorAll("#filters input[data-src]").forEach((cb) => (cb.onchange = renderTrace));
  setStatus("Not connected", "");
}

function agentCfg() { return state.config.agents.find((a) => a.id === state.agent); }

function renderAgents() {
  const box = $("agents");
  box.innerHTML = "";
  for (const a of state.config.agents) {
    const el = document.createElement("div");
    el.className = "card" + (a.id === state.agent ? " selected" : "");
    el.innerHTML = `<b></b><small></small><div class="desc"></div>`;
    el.querySelector("b").textContent = a.name;
    el.querySelector("small").textContent = a.tagline;
    el.querySelector(".desc").textContent = a.description;
    el.onclick = () => selectAgent(a.id);
    box.appendChild(el);
  }
}

function selectAgent(id) {
  if (state.agent === id) return;
  if (state.connected) endSession("Session ended (agent changed).");
  state.agent = id;
  state.sample = null;
  $("script").hidden = true;
  renderAgents();
  renderSamples();
}

function renderSamples() {
  const box = $("samples");
  box.innerHTML = "";
  for (const s of agentCfg().samples) {
    const el = document.createElement("div");
    el.className = "card" + (state.sample && state.sample.id === s.id ? " selected" : "");
    el.innerHTML = `<b></b><div class="desc"></div>`;
    el.querySelector("b").textContent = s.title;
    el.querySelector(".desc").textContent = s.description;
    el.onclick = () => selectSample(s);
    box.appendChild(el);
  }
}

// ------------------------------------------------------------------ sample scripts

function selectSample(s) {
  state.sample = s;
  state.scriptIdx = 0;
  $("script").hidden = false;
  $("scriptTitle").textContent = s.title;
  renderSamples();
  renderScript();
}

function renderScript() {
  const ol = $("scriptTurns");
  ol.innerHTML = "";
  state.sample.turns.forEach((t, i) => {
    const li = document.createElement("li");
    li.textContent = t;
    li.className = i < state.scriptIdx ? "done" : i === state.scriptIdx ? "next" : "";
    li.title = "Click to send this line";
    li.onclick = () => sendText(t);
    ol.appendChild(li);
  });
  const finished = state.scriptIdx >= state.sample.turns.length;
  $("scriptNext").disabled = !state.connected || finished;
  $("scriptAll").disabled = !state.connected || finished;
}

function scriptNext() {
  if (!state.sample || state.scriptIdx >= state.sample.turns.length) return;
  const text = state.sample.turns[state.scriptIdx++];
  renderScript();
  sendText(text);
}

async function scriptAll() {
  $("scriptAll").disabled = $("scriptNext").disabled = true;
  while (state.connected && state.sample && state.scriptIdx < state.sample.turns.length) {
    const text = state.sample.turns[state.scriptIdx++];
    renderScript();
    if (!sendText(text)) break;
    await waitForAgentDone();
    await new Promise((r) => setTimeout(r, 700));
  }
  if (state.sample) renderScript();
}

function waitForAgentDone() {
  return new Promise((resolve) => state.waiters.push(resolve));
}

// ------------------------------------------------------------------ session

function setStatus(text, cls) {
  $("statusText").textContent = text;
  $("statusDot").className = "dot " + cls;
}

function setConnectedUi(on) {
  state.connected = on;
  $("startBtn").disabled = on;
  $("endBtn").disabled = !on;
  $("textInput").disabled = !on;
  $("sendBtn").disabled = !on;
  $("micBtn").disabled = !on;
  $("cancelBtn").disabled = !on;
  if (state.sample) renderScript();
}

function startSession() {
  clearChat();
  clearTrace();
  state.t0 = null;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/session?agent=${encodeURIComponent(state.agent)}&audio=${$("speakToggle").checked}`;
  setStatus("Connecting…", "busy");
  $("startBtn").disabled = true;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  state.ws = ws;
  ws.onopen = () => { setConnectedUi(true); $("textInput").focus(); };
  ws.onmessage = (e) => (typeof e.data === "string" ? onMessage(JSON.parse(e.data)) : playPcm(e.data));
  ws.onclose = () => { if (state.ws === ws) finishSession("Disconnected."); };
  ws.onerror = () => setStatus("Connection error", "err");
}

function endSession(note) {
  const ws = state.ws;
  state.ws = null;
  if (ws) ws.close();
  finishSession(note);
}

function finishSession(note) {
  stopMic();
  stopPlayback();
  state.ws = null;
  state.responding = false;
  state.agentBubble = null;
  state.waiters.splice(0).forEach((r) => r());
  setConnectedUi(false);
  setStatus("Not connected", "");
  if (note) addNote(note);
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) { state.ws.send(JSON.stringify(obj)); return true; }
  return false;
}

function sendText(text) {
  text = (text || "").trim();
  if (!text || !state.connected) return false;
  if (state.responding) { addNote("The agent is still answering — wait or press ■ to stop it."); return false; }
  return send({ type: "text", text });
}

// ------------------------------------------------------------------ incoming messages

function onMessage(m) {
  switch (m.type) {
    case "ready":
      setStatus(`Connected · ${m.agent}`, "on");
      $("sessionInfo").textContent = `session ${m.voice_session_id}`;
      break;
    case "user_text":
    case "user_transcript":
      addBubble("user", "You", m.text);
      break;
    case "agent_start":
      state.responding = true;
      setStatus("Agent speaking…", "busy");
      state.agentBubble = addBubble("agent partial", "Agent", "");
      break;
    case "agent_delta":
      if (!state.agentBubble) state.agentBubble = addBubble("agent partial", "Agent", "");
      state.agentBubble.body.textContent += m.text;
      scrollChat();
      break;
    case "agent_done":
      state.responding = false;
      setStatus(`Connected · ${state.agent}`, "on");
      if (state.agentBubble) {
        if (m.text) state.agentBubble.body.textContent = m.text;
        if (!state.agentBubble.body.textContent) state.agentBubble.el.remove();
        state.agentBubble.el.classList.remove("partial");
        state.agentBubble = null;
      }
      state.waiters.splice(0).forEach((r) => r());
      break;
    case "interrupt":
      stopPlayback();
      break;
    case "busy":
      addNote("The agent is still answering.");
      break;
    case "error":
      addBubble("err", "Error", m.message);
      state.responding = false;
      state.waiters.splice(0).forEach((r) => r());
      break;
    case "trace":
      addTrace(m);
      break;
  }
}

// ------------------------------------------------------------------ chat

function clearChat() { $("chat").innerHTML = ""; }
function scrollChat() { const c = $("chat"); c.scrollTop = c.scrollHeight; }
function addNote(text) {
  const el = document.createElement("div");
  el.className = "note";
  el.textContent = text;
  $("chat").appendChild(el);
  scrollChat();
}
function addBubble(cls, who, text) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  const w = document.createElement("span");
  w.className = "who";
  w.textContent = who;
  const body = document.createElement("span");
  body.textContent = text;
  el.append(w, body);
  $("chat").appendChild(el);
  scrollChat();
  return { el, body };
}

// ------------------------------------------------------------------ audio

async function ensureAudio() {
  if (!state.ctx) {
    state.ctx = new AudioContext({ sampleRate: 24000 });
    await state.ctx.audioWorklet.addModule("/static/mic-worklet.js");
  }
  if (state.ctx.state === "suspended") await state.ctx.resume();
}

async function toggleMic() {
  if (state.micOn) { stopMic(); return; }
  try {
    await ensureAudio();
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const src = state.ctx.createMediaStreamSource(state.micStream);
    state.micNode = new AudioWorkletNode(state.ctx, "mic-processor");
    state.micNode.port.onmessage = (e) => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(e.data);
    };
    src.connect(state.micNode);
    state.micOn = true;
    $("micBtn").classList.add("on");
    $("speakToggle").checked = true;
    send({ type: "audio_out", enabled: true });
    addNote("Microphone on — just speak. Replies are spoken.");
  } catch (err) {
    addBubble("err", "Microphone", `Could not start the microphone: ${err.message}`);
  }
}

function stopMic() {
  if (state.micNode) { state.micNode.disconnect(); state.micNode = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  state.micOn = false;
  $("micBtn").classList.remove("on");
}

function playPcm(buf) {
  if (!state.ctx || !$("speakToggle").checked) {
    if (!state.ctx) ensureAudio().then(() => playPcm(buf));
    return;
  }
  const pcm = new Int16Array(buf);
  const audio = state.ctx.createBuffer(1, pcm.length, 24000);
  const ch = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = state.ctx.createBufferSource();
  src.buffer = audio;
  src.connect(state.ctx.destination);
  const now = state.ctx.currentTime;
  if (state.playHead < now + 0.02) state.playHead = now + 0.05;
  src.start(state.playHead);
  state.playHead += audio.duration;
  state.sources.add(src);
  src.onended = () => state.sources.delete(src);
}

function stopPlayback() {
  state.sources.forEach((s) => { try { s.stop(); } catch (_) {} });
  state.sources.clear();
  state.playHead = 0;
}

// ------------------------------------------------------------------ trace

function addTrace(ev) {
  if (state.t0 === null) state.t0 = ev.t;
  state.traces.push(ev);
  state.traces.sort((a, b) => a.t - b.t);
  updateMetrics(ev);
  renderTrace();
}

function updateMetrics(ev) {
  const d = ev.data || {};
  if (ev.source === "agent") {
    if (ev.kind === "handoff" && !d.refused) { state.handoffs++; $("mHandoffs").textContent = state.handoffs; }
    if (ev.kind === "handoff" && !d.refused) $("mActive").textContent = d.target;
    if (ev.kind === "turn.start" || ev.kind === "turn.end") if (d.agent) $("mActive").textContent = d.agent;
    if (ev.kind === "phase") $("mActive").textContent = d.phase;
  }
  if (ev.source === "voice") {
    if (ev.kind === "first.token") $("mFirst").textContent = (d.ms / 1000).toFixed(1) + "s";
    if (ev.kind === "response.done" && d.total_ms) $("mTotal").textContent = (d.total_ms / 1000).toFixed(1) + "s";
  }
}

function fmt(v) {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

function summarize(ev) {
  const d = ev.data || {};
  const k = ev.kind;
  if (ev.source === "log") return d.message || "";
  if (k === "handoff") return `${d.source} → ${d.target}${d.refused ? "  (refused)" : ""}`;
  if (k === "decision.analyze")
    return `billing ${fmt(d.billing)} · orders ${fmt(d.order_status)} · primary ${d.primary_topic} · end ${fmt(d.wants_to_end)}` +
      (d.topics && d.topics.length ? ` → [${d.topics.join(", ")}]` : "") + (d.ok === false ? " (not ok)" : "");
  if (k === "decision.guard") return `[${d.agent}] other-topic ${fmt(d.other_topic)} · resolved ${fmt(d.resolved)}`;
  if (k === "decision.confirmation") return `verdict: ${d.verdict}`;
  if (k === "decision.call") return `${d.model} · ${(d.questions || []).join(", ")} · ${d.ms} ms` + (d.error ? ` · ERROR ${d.error}` : "");
  if (k === "tool.call") {
    const args = Object.entries(d.args || {}).map(([a, b]) => `${a}=${fmt(b)}`).join(", ");
    return `${d.name}(${args}) → ${d.error ? "ERROR " + d.error : d.result}` + (d.ms != null ? `  · ${d.ms} ms` : "");
  }
  if (k === "turn.start") return `[${d.agent}${d.phase ? "/" + d.phase : ""}] “${d.user}”`;
  if (k === "turn.end") return `[${d.agent}] ${d.ms} ms${d.handoffs ? ` · ${d.handoffs} handoff(s)` : ""}${d.ended ? " · call ended" : ""} — “${d.reply}”`;
  if (k === "agent.start") return `${d.agent}${d.directive ? " · directive: " + d.directive : ""}`;
  if (k === "agent.end") return `${d.agent} · first token ${d.first_token_ms} ms · total ${d.ms} ms`;
  if (k === "phase") return `phase → ${d.phase}${d.proposed ? " · " + d.proposed.map((p) => p.topic + " " + JSON.stringify(p.params)).join("; ") : ""}`;
  if (k === "llm.extract") return `order_id=${d.order_id || "∅"} period=${d.invoice_period} topics=[${(d.topics || []).join(", ")}]`;
  if (k.startsWith("task.")) return `${d.topic} #${d.task_id}${d.ms != null ? " · " + d.ms + " ms" : ""}${d.result ? " — " + d.result : ""}${d.error ? " — " + d.error : ""}`;
  if (k === "first.token" || k === "first.audio") return `${d.ms} ms after input`;
  if (k === "response.done") return `${d.status}${d.total_ms ? " · " + d.total_ms + " ms" : ""}${d.usage && d.usage.total_tokens ? " · " + d.usage.total_tokens + " tokens" : ""}`;
  if (k === "user.text" || k === "user.transcript") return `“${d.text}”`;
  if (k === "session.created") return `${d.session_id} → agent session ${d.agent_session_id}`;
  const entries = Object.entries(d).map(([a, b]) => `${a}=${fmt(b)}`).join(" ");
  return entries;
}

function isError(ev) {
  const d = ev.data || {};
  return ev.kind === "error" || ev.kind === "warning" || ev.kind === "agent.error" || ev.kind === "plan.error" || ev.kind === "task.failed" || !!d.error;
}

function renderTrace() {
  const allowed = new Set([...document.querySelectorAll("#filters input[data-src]")].filter((c) => c.checked).map((c) => c.dataset.src));
  const box = $("trace");
  const stick = $("autoscroll").checked;
  box.innerHTML = "";
  for (const ev of state.traces) {
    if (!allowed.has(ev.source)) continue;
    const row = document.createElement("div");
    row.className = `row ${ev.source}` + (isError(ev) ? " err" : "") + (ev.kind === "handoff" && !(ev.data || {}).refused ? " hl" : "");
    const top = document.createElement("div");
    top.className = "top";
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = ((ev.t - state.t0) / 1000).toFixed(2) + "s";
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = ev.source;
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = ev.kind;
    const sum = document.createElement("span");
    sum.className = "sum";
    sum.textContent = summarize(ev);
    top.append(ts, chip, kind, sum);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(ev, null, 2);
    row.append(top, pre);
    row.onclick = () => row.classList.toggle("open");
    box.appendChild(row);
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

function clearTrace() {
  state.traces = [];
  state.handoffs = 0;
  state.t0 = null;
  ["mActive", "mFirst", "mTotal"].forEach((i) => ($(i).textContent = "–"));
  $("mHandoffs").textContent = "0";
  renderTrace();
}

function downloadTrace() {
  const blob = new Blob([JSON.stringify({ agent: state.agent, events: state.traces }, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `trace-${state.agent}-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

init();
