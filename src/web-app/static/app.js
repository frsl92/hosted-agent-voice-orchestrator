"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: null, agent: null, mode: "text", sample: null, scriptIdx: 0,
  ws: null, session: null, connected: false, responding: false, callActive: false, muted: false,
  sessionState: "stopped", sessionMsg: "", startTimer: null,
  t0: null, traces: [], activeLabel: "–",
  ctx: null, micStream: null, micNode: null, micAnalyser: null, outAnalyser: null, playHead: 0, sources: new Set(),
  agentBubble: null, agentSegs: [], agentRaw: "", pendingBubble: null, waiters: [], orb: "idle", orbTimer: null, turnToken: 0, gen: 0,
  mic: { deviceId: "", aec: false },
};

// ------------------------------------------------------------------ setup

async function init() {
  const res = await fetch("/api/config");
  if (res.status === 401) { location.href = "/login"; return; }
  state.config = await res.json();
  $("project").textContent = `Project ${state.config.project} · Foundry hosted agents · Voice Live · Microsoft-Decision-1`;
  state.agent = state.config.agents[0].id;
  Viz.init({ flow: $("flowVisual"), timeline: $("timeline"), decisions: $("decisions"), onCounts: onCounts });
  Usage.init({ body: $("usageBody"), badge: $("usageBadge"), configBtn: $("usageConfig"), modal: $("pricingModal"), cfgRows: $("cfgRows"), cfgSave: $("cfgSave"), cfgCancel: $("cfgCancel"), cfgReset: $("cfgReset") });

  renderSeg("agentSeg", state.config.agents, () => state.agent, (id) => { state.agent = id; changed(); });
  renderSeg("modeSeg", state.config.modes, () => state.mode, (id) => { state.mode = id; changed(); });
  initVoicePicker();
  initMicPicker();

  $("startBtn").onclick = startSession;
  $("stopBtn").onclick = () => stopSession();
  $("logoutBtn").onclick = async () => { await fetch("/logout", { method: "POST" }); location.href = "/login"; };
  $("composer").onsubmit = (e) => { e.preventDefault(); if (!isReady()) return; const v = $("textInput").value; $("textInput").value = ""; sendText(v); };
  $("cancelBtn").onclick = () => send({ type: "cancel" });
  $("muteBtn").onclick = toggleMute;
  $("scriptNext").onclick = scriptNext;
  $("scriptAll").onclick = scriptAll;
  $("scriptClose").onclick = () => { state.sample = null; $("script").hidden = true; renderSamples(); };
  $("traceDownload").onclick = downloadTrace;
  $("traceCopy").onclick = copyTrace;
  document.querySelectorAll("#filters input[data-src]").forEach((cb) => (cb.onchange = renderTrace));
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => selectTab(t.dataset.tab)));
  window.addEventListener("beforeunload", () => { if (state.ws) state.ws.close(); });
  resetConversation();
}

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === "pane" + name[0].toUpperCase() + name.slice(1)));
  if (name === "trace") { const b = $("trace"); b.scrollTop = b.scrollHeight; }
}

function renderSeg(id, items, current, onPick) {
  const box = $(id);
  box.innerHTML = "";
  for (const it of items) {
    const b = document.createElement("button");
    b.textContent = it.name;
    b.title = it.tagline || it.description || "";
    b.className = it.id === current() ? "active" : "";
    b.onclick = () => {
      if (it.id === current()) return;
      onPick(it.id);
      renderSeg(id, items, current, onPick);
    };
    box.appendChild(b);
  }
}

const agentCfg = () => state.config.agents.find((a) => a.id === state.agent);

// Voice + style are only meaningful for the voice experience; the locale is forced to pt-PT server-side.
const voiceCfg = () => state.config.voices.find((v) => v.id === state.voice);

function initVoicePicker() {
  const saved = JSON.parse(localStorage.getItem("voicePick") || "{}");
  state.voice = state.config.voices.some((v) => v.id === saved.voice) ? saved.voice : state.config.defaultVoice;
  state.style = saved.style || "";
  $("voiceSel").innerHTML = "";
  for (const v of state.config.voices) {
    const o = document.createElement("option");
    o.value = v.id;
    o.textContent = `${v.name} (${v.gender}, ${v.tier}${v.status === "Preview" ? ", preview" : ""})`;
    $("voiceSel").appendChild(o);
  }
  $("voiceSel").value = state.voice;
  renderStyles();
  $("voiceSel").onchange = () => { state.voice = $("voiceSel").value; state.style = ""; renderStyles(); voicePicked(); };
  $("styleSel").onchange = () => { state.style = $("styleSel").value; voicePicked(); };
}

function renderStyles() {
  const styles = voiceCfg().styles;
  const sel = $("styleSel");
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = styles.length ? "Default" : "Not available";
  sel.appendChild(none);
  for (const s of styles) {
    const o = document.createElement("option");
    o.value = s;
    o.textContent = s;
    sel.appendChild(o);
  }
  if (!styles.includes(state.style)) state.style = "";
  sel.value = state.style;
  sel.disabled = !styles.length;
}

function voicePicked() {
  localStorage.setItem("voicePick", JSON.stringify({ voice: state.voice, style: state.style }));
  if (state.mode === "voice") resetConversation();
}

// Microphone choice and browser echo cancellation, remembered in this browser; applied when a session starts.
function initMicPicker() {
  Object.assign(state.mic, JSON.parse(localStorage.getItem("micPick") || "{}"));
  $("aecChk").checked = !!state.mic.aec;
  $("aecChk").onchange = () => { state.mic.aec = $("aecChk").checked; saveMic(); };
  $("micSel").onchange = () => { state.mic.deviceId = $("micSel").value; saveMic(); };
  if (navigator.mediaDevices) navigator.mediaDevices.addEventListener("devicechange", refreshMics);
  refreshMics();
}

const saveMic = () => localStorage.setItem("micPick", JSON.stringify(state.mic));

async function refreshMics() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  const sel = $("micSel");
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = ""; def.textContent = "System default";
  sel.appendChild(def);
  devices.forEach((d, i) => {
    if (d.deviceId === "default" || d.deviceId === "communications") return;
    const o = document.createElement("option");
    o.value = d.deviceId; o.textContent = d.label || `Microphone ${i + 1}`;
    sel.appendChild(o);
  });
  if (![...sel.options].some((o) => o.value === state.mic.deviceId)) state.mic.deviceId = "";
  sel.value = state.mic.deviceId;
}

function changed() {
  renderAgentInfo();
  resetConversation();
}

function renderAgentInfo() {
  const a = agentCfg();
  const m = state.config.modes.find((x) => x.id === state.mode);
  $("agentInfo").innerHTML = "";
  const b = document.createElement("b"); b.textContent = a.name;
  const t = document.createElement("span"); t.className = "tag"; t.textContent = a.tagline;
  const d = document.createElement("p"); d.textContent = a.description;
  const mm = document.createElement("small"); mm.textContent = `${m.name} experience: ${m.description}`;
  $("agentInfo").append(b, t, d, mm);
}

function renderSamples() {
  const box = $("samples");
  box.innerHTML = "";
  for (const s of state.config.samples) {
    const el = document.createElement("div");
    el.className = "card" + (state.sample && state.sample.id === s.id ? " selected" : "");
    el.innerHTML = `<b></b><div class="desc"></div>`;
    el.querySelector("b").textContent = s.title;
    el.querySelector(".desc").textContent = s.description;
    el.onclick = () => selectSample(s);
    box.appendChild(el);
  }
}

// ------------------------------------------------------------------ conversation lifecycle

const isReady = () => state.sessionState === "ready";
const EMPTY_TEXT = "Press <b>Start session</b> to begin a conversation. Text and voice are locked until the agent is ready.";
const EMPTY_VOICE = "Captions appear here as you talk. They are for reference only — the conversation is voice.";

// Stop whatever is open and wipe every pane. Nothing connects until the user presses Start session.
function resetConversation() {
  state.gen++;
  closeSession();
  state.muted = false;
  state.traces = []; state.t0 = null; state.activeLabel = "–";
  state.scriptIdx = 0;
  $("chat").innerHTML = `<div class="empty">${EMPTY_TEXT}</div>`;
  $("captions").innerHTML = `<div class="empty">${EMPTY_VOICE}</div>`;
  $("trace").innerHTML = "";
  ["mActive", "mFirst", "mTotal"].forEach((i) => ($(i).textContent = "–"));
  $("decCount").textContent = "0";
  $("mCount").textContent = "0";
  Viz.reset(agentCfg().kind);
  Usage.reset();
  $("mCountLabel").textContent = agentCfg().kind === "handoff" ? "Handoffs" : "Tasks";
  renderAgentInfo();

  const voice = state.mode === "voice";
  $("textView").hidden = voice;
  $("voiceView").hidden = !voice;
  $("voicePicker").hidden = !voice;
  setSessionState("stopped", "");
}

// Moves between "stopped" -> "starting" -> "ready" and locks/unlocks every input accordingly.
function setSessionState(s, msg, err) {
  clearTimeout(state.startTimer);
  state.sessionState = s;
  state.sessionMsg = msg || "";
  const voice = state.mode === "voice";
  state.callActive = voice && s !== "stopped";
  if (s === "stopped") {
    setStatus("No session", err ? "err" : "");
    if (voice) setOrb("idle", "Press <b>Start session</b> to begin");
  } else if (s === "starting") {
    setStatus("Starting session…", "busy");
    if (voice) setOrb("thinking", "Starting the session…");
  } else {
    setStatus(`Session active · ${agentCfg().name}`, "on");
    if (voice) setOrb("listening", state.sample ? "Listening — say the highlighted line" : "Listening — go ahead and speak");
  }
  renderSession();
}

function renderSession() {
  const s = state.sessionState;
  const voice = state.mode === "voice";
  const ready = s === "ready";
  $("sessionBar").className = "sessionbar " + s;
  $("sbTitle").textContent = s === "stopped" ? "No active session" : s === "starting" ? "Starting session…" : `Session active · ${agentCfg().name}`;
  $("sbMsg").textContent = state.sessionMsg || (
    s === "stopped" ? "Choose an agent and an experience, optionally load a sample, then press Start session."
    : s === "starting" ? "Connecting to the agent and waiting for the operator's greeting. Input unlocks as soon as it is ready — the first start can take ~10–15 s."
    : voice ? "Speak whenever you like. The captions are for reference only."
    : "Type a message, or run a sample conversation.");
  $("startBtn").hidden = s !== "stopped";
  $("stopBtn").hidden = s === "stopped";
  $("stopBtn").textContent = s === "starting" ? "✕ Cancel" : "■ Stop session";
  $("textInput").disabled = voice || !ready;
  $("textInput").placeholder = ready ? "Type a message…" : s === "starting" ? "Starting session… please wait" : "Start a session to chat";
  $("sendBtn").disabled = voice || !ready;
  $("cancelBtn").disabled = !ready || !state.responding;
  $("muteBtn").hidden = !(voice && ready);
  $("muteBtn").textContent = state.muted ? "🔇 Unmute" : "🎤 Mute";
  $("muteBtn").classList.toggle("on", state.muted);
  if (state.sample) renderScript();
}

// Opens a brand-new session (wiping the previous conversation) and waits for the agent's greeting.
async function startSession() {
  if (state.sessionState !== "stopped") return;
  resetConversation();
  const gen = state.gen;
  setSessionState("starting");
  // Fallback so a lost greeting can never leave the UI locked forever.
  state.startTimer = setTimeout(() => { if (gen === state.gen && state.sessionState === "starting") markReady(true); }, 60000);
  try {
    if (state.mode === "voice") await openMic(gen);
    else await ensureSession();
  } catch (err) {
    if (gen !== state.gen || (err && err.message === "closed")) return; // onclose already reported it
    failSession(`Could not start the session: ${(err && err.message) || err}`);
  }
}

function markReady(timedOut) {
  if (state.sessionState !== "starting") return;
  setSessionState("ready");
  if (timedOut) note("The agent is slow to greet — you can start talking anyway.");
  if (state.mode === "text") $("textInput").focus();
}

function failSession(msg) {
  state.gen++;
  closeSession();
  setSessionState("stopped", msg, true);
}

// Ends the session but leaves the transcript, flow and traces on screen for inspection.
function stopSession(msg) {
  state.gen++;
  closeSession();
  state.responding = false;
  state.muted = false;
  setSessionState("stopped", msg || "Session stopped. Press Start session to open a new one — this conversation stays on screen until then.");
  note("Session stopped.");
}

function closeSession() {
  const ws = state.ws;
  state.ws = null; state.session = null; state.connected = false; state.responding = false;
  state.agentBubble = state.pendingBubble = null; state.agentSegs = []; state.agentRaw = "";
  if (ws) { ws.onclose = ws.onerror = ws.onmessage = null; try { ws.close(); } catch (_) {} }
  stopMic();
  stopPlayback();
  state.waiters.splice(0).forEach((r) => r());
}

function ensureSession() {
  if (state.session) return state.session;
  const gen = state.gen;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const voiceQ = state.mode === "voice" ? `&voice=${encodeURIComponent(state.voice)}&style=${encodeURIComponent(state.style)}` : "";
  const url = `${proto}://${location.host}/ws/session?agent=${encodeURIComponent(state.agent)}&mode=${state.mode}${voiceQ}`;
  state.session = new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    ws.onmessage = (e) => {
      if (gen !== state.gen) return;
      if (typeof e.data !== "string") return playPcm(e.data);
      const m = JSON.parse(e.data);
      if (m.type === "ready") { state.connected = true; resolve(); }
      onMessage(m);
    };
    ws.onclose = (e) => {
      if (gen !== state.gen) return;
      const wasConnected = state.connected;
      state.session = null; state.ws = null; state.connected = false; state.responding = false;
      stopMic();
      state.waiters.splice(0).forEach((r) => r());
      if (e.code === 4401) { location.href = "/login"; return; }
      if (wasConnected) note("The session ended.");
      setSessionState("stopped", wasConnected ? "The session ended. Press Start session to open a new one." : "Could not connect to the agent. Press Start session to try again.", !wasConnected);
      reject(new Error("closed"));
    };
    ws.onerror = () => { if (gen === state.gen) setStatus("Connection error", "err"); };
  });
  state.session.catch(() => {});
  return state.session;
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) { state.ws.send(JSON.stringify(obj)); return true; }
  return false;
}

async function sendText(text) {
  text = (text || "").trim();
  if (!text || state.mode !== "text" || !isReady()) return false;
  if (state.responding) { note("The agent is still answering — wait or press ■ to stop it."); return false; }
  try { await ensureSession(); } catch (_) { return false; }
  state.responding = true;
  $("cancelBtn").disabled = false;
  if (state.sample) renderScript();
  state.pendingBubble = addBubble("agent pending", "Agent", "thinking…");
  setStatus("Agent working…", "busy");
  return send({ type: "text", text });
}

// ------------------------------------------------------------------ sample scripts

// Picking a sample only loads its script; it never starts (or restarts) a session.
function selectSample(s) {
  state.sample = s;
  state.scriptIdx = 0;
  $("script").hidden = false;
  $("scriptTitle").textContent = s.title;
  renderSamples();
  renderScript();
}

function renderScript() {
  const voice = state.mode === "voice";
  const ready = isReady();
  const ol = $("scriptTurns");
  ol.innerHTML = "";
  ol.className = voice || !ready ? "" : "clickable";
  $("scriptHint").textContent = voice
    ? "Say these lines in order once the session is active. The next line is highlighted as you speak."
    : ready ? "Click a line to send it, or use the buttons to step through the whole conversation."
    : "Loaded. Press Start session — the lines unlock once the agent is ready.";
  $("scriptNext").hidden = $("scriptAll").hidden = voice;
  state.sample.turns.forEach((t, i) => {
    const li = document.createElement("li");
    li.textContent = t;
    li.className = i < state.scriptIdx ? "done" : i === state.scriptIdx ? "next" : "";
    if (!voice && ready) li.onclick = () => { state.scriptIdx = i + 1; renderScript(); sendText(t); };
    ol.appendChild(li);
  });
  const finished = state.scriptIdx >= state.sample.turns.length;
  $("scriptNext").disabled = $("scriptAll").disabled = finished || state.responding || !ready;
}

function scriptNext() {
  if (!state.sample || state.scriptIdx >= state.sample.turns.length) return;
  const text = state.sample.turns[state.scriptIdx++];
  renderScript();
  sendText(text);
}

async function scriptAll() {
  const gen = state.gen;
  $("scriptAll").disabled = $("scriptNext").disabled = true;
  while (gen === state.gen && state.sample && state.scriptIdx < state.sample.turns.length) {
    const text = state.sample.turns[state.scriptIdx++];
    renderScript();
    if (!(await sendText(text))) break;
    await new Promise((r) => state.waiters.push(r));
    await new Promise((r) => setTimeout(r, 700));
  }
  if (gen === state.gen && state.sample) renderScript();
}

// ------------------------------------------------------------------ status helpers

function setStatus(text, cls) {
  $("statusText").textContent = text;
  $("statusDot").className = "dot " + cls;
}

function onCounts(c) {
  $("decCount").textContent = c.decisions;
  $("mCount").textContent = c.kind === "handoff" ? c.handoffs : c.tasks;
}

// ------------------------------------------------------------------ incoming messages

const chatEl = () => (state.mode === "voice" ? $("captions") : $("chat"));

function onMessage(m) {
  switch (m.type) {
    case "ready":
      break;
    case "greeting":
      // The agent greets the caller as soon as the session opens; nothing is shown for the trigger itself.
      state.responding = true;
      if (state.mode === "text") {
        state.pendingBubble = addBubble("agent pending", "Agent", "connecting you to an operator…");
      }
      break;
    case "user_text":
    case "user_transcript":
      if (m.type === "user_transcript" && !m.text.trim()) { // VAD blip that STT could not transcribe
        if (!state.responding && state.callActive) setOrb("listening", "Listening…");
        break;
      }
      addBubble("user", "You", m.text);
      if (m.type === "user_transcript") {
        advanceScript();
        setOrb("thinking", "Thinking…");
        state.responding = true;
      }
      break;
    case "agent_start":
      state.responding = true;
      $("cancelBtn").disabled = !isReady();
      clearPending();
      if (isReady()) setStatus("Agent answering…", "busy");
      state.agentBubble = addBubble("agent partial", "Agent", "");
      state.agentSegs = []; state.agentRaw = "";
      if (state.mode === "voice" && isReady()) setOrb("thinking", "Thinking…");
      break;
    case "agent_delta":
      if (!state.agentBubble) { clearPending(); state.agentBubble = addBubble("agent partial", "Agent", ""); state.agentSegs = []; state.agentRaw = ""; }
      appendAgentText(m.text);
      scrollChat();
      break;
    case "agent_done": {
      state.responding = false;
      $("cancelBtn").disabled = true;
      clearPending();
      if (state.agentBubble) {
        if (m.text && !state.agentRaw.trim()) setAgentText(m.text);
        if (!state.agentBubble.raw.trim()) state.agentBubble.el.remove();
        else state.agentBubble.el.classList.remove("partial");
        state.agentBubble = null; state.agentSegs = []; state.agentRaw = "";
      }
      if (isReady()) setStatus(`Session active · ${agentCfg().name}`, "on");
      if (state.mode === "voice") afterPlayback();
      state.waiters.splice(0).forEach((r) => r());
      if (state.sessionState === "starting") {
        // The greeting is done: unlock input once it has finished playing.
        const gen = state.gen;
        const wait = state.mode === "voice" && state.ctx ? Math.max(0, state.playHead - state.ctx.currentTime) * 1000 + 300 : 0;
        setTimeout(() => { if (gen === state.gen) markReady(); }, wait);
      }
      if (state.sample) renderScript();
      break;
    }
    case "interrupt":
      stopPlayback();
      if (state.mode === "voice" && isReady()) setOrb("hearing", "Listening…");
      break;
    case "busy":
      note("The agent is still answering.");
      break;
    case "error":
      clearPending();
      addBubble("err", "Error", m.message);
      state.responding = false;
      $("cancelBtn").disabled = true;
      state.waiters.splice(0).forEach((r) => r());
      if (state.sessionState === "starting") markReady(); // let the user retry rather than staying locked
      break;
    case "trace":
      addTrace(m);
      break;
  }
}

function advanceScript() {
  if (state.sample && state.scriptIdx < state.sample.turns.length) { state.scriptIdx++; renderScript(); }
}

function clearPending() {
  if (state.pendingBubble) { state.pendingBubble.el.remove(); state.pendingBubble = null; }
}

// A blank line in the agent stream marks a new message (one per sub-agent answer).
function setAgentText(full) {
  state.agentRaw = full;
  let parts = full.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  if (!parts.length) parts = [""];
  const segs = state.agentSegs;
  if (!segs.length) segs.push(state.agentBubble);
  while (segs.length < parts.length) {
    const b = segs.length === 0 ? state.agentBubble : addBubble("agent partial", "Agent", "");
    if (segs.length) segs[segs.length - 1].el.classList.remove("partial");
    segs.push(b);
  }
  parts.forEach((p, i) => { segs[i].body.textContent = p; segs[i].raw = p; });
  state.agentBubble = segs[segs.length - 1];
}
function appendAgentText(text) {
  setAgentText((state.agentRaw || "") + text);
}

// ------------------------------------------------------------------ chat

function scrollChat() { const c = chatEl(); c.scrollTop = c.scrollHeight; }
function dropEmpty() { const e = chatEl().querySelector(".empty"); if (e) e.remove(); }
function note(text) {
  dropEmpty();
  const el = document.createElement("div");
  el.className = "note";
  el.textContent = text;
  chatEl().appendChild(el);
  scrollChat();
}
function addBubble(cls, who, text) {
  dropEmpty();
  const el = document.createElement("div");
  el.className = "msg " + cls;
  const w = document.createElement("span");
  w.className = "who";
  w.textContent = who;
  const body = document.createElement("span");
  body.textContent = text;
  el.append(w, body);
  chatEl().appendChild(el);
  scrollChat();
  return { el, body };
}

// ------------------------------------------------------------------ voice session

// Asks for the microphone, opens the session and wires the mic. Audio is only sent once the session is
// ready (after the agent's greeting), so the caller cannot talk over it.
async function openMic(gen) {
  await ensureAudio();
  const q = new URLSearchParams(location.search);
  // Browser processing is off by default: noise suppression/AGC gated and clipped Bluetooth headset audio, and
  // browser echo cancellation can mute a Bluetooth mic completely. Voice Live applies Azure deep noise
  // suppression and echo cancellation on the server. The "echo cancel" box (or ?aec=1&ns=1&agc=1) overrides.
  const flag = (k, def) => (q.has(k) ? q.get(k) !== "0" : def);
  const audio = {
    channelCount: 1, echoCancellation: flag("aec", state.mic.aec), noiseSuppression: flag("ns", false), autoGainControl: flag("agc", false),
  };
  try {
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: state.mic.deviceId ? { ...audio, deviceId: { exact: state.mic.deviceId } } : audio,
    });
  } catch (err) {
    if (!state.mic.deviceId || err.name !== "OverconstrainedError") throw err;
    note("The selected microphone is not available — using the system default.");
    state.mic.deviceId = "";
    state.micStream = await navigator.mediaDevices.getUserMedia({ audio });
  }
  refreshMics();
  if (gen !== state.gen) { stopMic(); return; }
  await ensureSession();
  if (gen !== state.gen) return;
  const track = state.micStream.getAudioTracks()[0];
  sendDiag({
    event: "mic.start", label: track && track.label, ctxRate: state.ctx.sampleRate,
    settings: track && track.getSettings ? track.getSettings() : null,
  });
  if (track) {
    track.onmute = () => { sendDiag({ event: "mic.track", state: "muted" }); note("The microphone stopped delivering audio (device busy or switching profile)."); };
    track.onunmute = () => sendDiag({ event: "mic.track", state: "unmuted" });
    track.onended = () => {
      sendDiag({ event: "mic.track", state: "ended" });
      if (gen === state.gen && state.callActive) note("The microphone was disconnected. Stop the session, pick the mic in the header and start again.");
    };
  }
  const src = state.ctx.createMediaStreamSource(state.micStream);
  state.micNode = new AudioWorkletNode(state.ctx, "mic-processor");
  let lastMsg = 0, maxGap = 0, frames = 0, quiet = 0;
  state.micNode.port.onmessage = (e) => {
    if (!(e.data instanceof ArrayBuffer)) {
      const s = e.data;
      sendDiag({ event: "mic.stats", peak: +s.peak.toFixed(3), rms: +s.rms.toFixed(4), frames, maxGapMs: Math.round(maxGap),
        buffered: state.ws ? state.ws.bufferedAmount : null, muted: state.muted });
      frames = 0; maxGap = 0;
      quiet = isReady() && s.peak < 0.01 && !state.muted ? quiet + 1 : 0;
      if (quiet === 4) note("The microphone seems silent — pick another microphone in the header (Bluetooth headsets often deliver silence), or toggle \"echo cancel\", then restart the session.");
      return;
    }
    const now = performance.now();
    if (lastMsg) maxGap = Math.max(maxGap, now - lastMsg);
    lastMsg = now;
    frames++;
    if (isReady() && state.ws && state.ws.readyState === WebSocket.OPEN && !state.muted) state.ws.send(e.data);
  };
  state.micAnalyser = state.ctx.createAnalyser();
  state.micAnalyser.fftSize = 512;
  src.connect(state.micNode);
  src.connect(state.micAnalyser);
  levelLoop(gen);
}

function sendDiag(data) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: "diag", ...data }));
}

function toggleMute() {
  state.muted = !state.muted;
  if (state.micStream) state.micStream.getAudioTracks().forEach((t) => (t.enabled = !state.muted));
  renderSession();
}

function setOrb(name, label) {
  state.orb = name;
  clearTimeout(state.orbTimer);
  $("orb").className = "orb " + name;
  if (label != null) $("orbLabel").innerHTML = label;
}

// after the last audio chunk has been scheduled, wait for it to finish playing before "listening" again
function afterPlayback() {
  const token = ++state.turnToken;
  const remaining = state.ctx ? Math.max(0, state.playHead - state.ctx.currentTime) : 0;
  clearTimeout(state.orbTimer);
  state.orbTimer = setTimeout(() => {
    if (token === state.turnToken && state.callActive) setOrb("listening", "Listening…");
  }, remaining * 1000 + 250);
}

function levelLoop(gen) {
  const buf = new Uint8Array(256);
  const tick = () => {
    if (gen !== state.gen || !state.callActive) { $("orb").style.setProperty("--lvl", 0); return; }
    const an = state.orb === "speaking" ? state.outAnalyser : state.micAnalyser;
    let lvl = 0;
    if (an) {
      an.getByteTimeDomainData(buf);
      let s = 0;
      for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; s += v * v; }
      lvl = Math.min(1, Math.sqrt(s / buf.length) * 4);
    }
    $("orb").style.setProperty("--lvl", lvl.toFixed(2));
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ------------------------------------------------------------------ audio

async function ensureAudio() {
  if (!state.ctx) {
    state.ctx = new AudioContext({ sampleRate: 24000 });
    await state.ctx.audioWorklet.addModule("/static/mic-worklet.js");
    state.outAnalyser = state.ctx.createAnalyser();
    state.outAnalyser.fftSize = 512;
    state.outAnalyser.connect(state.ctx.destination);
  }
  if (state.ctx.state === "suspended") await state.ctx.resume();
}

function stopMic() {
  if (state.micNode) { state.micNode.disconnect(); state.micNode = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  state.micAnalyser = null;
}

function playPcm(buf) {
  if (!state.ctx || state.mode !== "voice" || !state.callActive) return;
  if (state.orb !== "speaking") setOrb("speaking", "Agent is speaking…");
  state.turnToken++;
  const pcm = new Int16Array(buf);
  const audio = state.ctx.createBuffer(1, pcm.length, 24000);
  const ch = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = state.ctx.createBufferSource();
  src.buffer = audio;
  src.connect(state.outAnalyser);
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
  const last = state.traces[state.traces.length - 1];
  state.traces.push(ev);
  updateMetrics(ev);
  Viz.onTrace(ev);
  Usage.onTrace(ev);
  voiceOrbFromTrace(ev);
  if (last && ev.t < last.t) { state.traces.sort((a, b) => a.t - b.t); renderTrace(); }
  else if (allowedSources().has(ev.source)) { appendRow(ev); }
}

function voiceOrbFromTrace(ev) {
  if (state.mode !== "voice" || !state.callActive || ev.source !== "voice") return;
  if (ev.kind === "speech.started") { setOrb("hearing", "Hearing you…"); state.turnToken++; }
  else if (ev.kind === "speech.stopped") {
    setOrb("thinking", "Thinking…");
    // If nothing was understood no response follows; do not stay on "Thinking" forever.
    const token = ++state.turnToken;
    state.orbTimer = setTimeout(() => {
      if (token === state.turnToken && !state.responding && state.callActive && state.orb === "thinking") setOrb("listening", "Listening…");
    }, 6000);
  }
}

function updateMetrics(ev) {
  const d = ev.data || {};
  if (ev.source === "agent") {
    if (ev.kind === "handoff" && !d.refused) $("mActive").textContent = d.target;
    if ((ev.kind === "turn.start" || ev.kind === "turn.end") && d.agent) $("mActive").textContent = d.agent;
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
  if (k === "decision.call") return `${d.purpose} · ${(d.questions || []).join(", ")} · ${d.ms} ms` + (d.error ? ` · ERROR ${d.error}` : "");
  if (k === "usage") return `${d.model} · ${d.purpose} · ${d.input_tokens} in / ${d.output_tokens} out`;
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
  if (k === "session.created") return `${d.mode || ""} ${d.session_id} → agent session ${d.agent_session_id}`;
  return Object.entries(d).map(([a, b]) => `${a}=${fmt(b)}`).join(" ");
}

function isError(ev) {
  const d = ev.data || {};
  return ev.kind === "error" || ev.kind === "warning" || ev.kind === "agent.error" || ev.kind === "plan.error" || ev.kind === "task.failed" || !!d.error;
}

const allowedSources = () => new Set([...document.querySelectorAll("#filters input[data-src]")].filter((c) => c.checked).map((c) => c.dataset.src));

function appendRow(ev) {
  const box = $("trace");
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
  if ($("autoscroll").checked) box.scrollTop = box.scrollHeight;
}

function renderTrace() {
  const allowed = allowedSources();
  $("trace").innerHTML = "";
  for (const ev of state.traces) if (allowed.has(ev.source)) appendRow(ev);
}

// Everything needed to debug a session: ids (to find it in Foundry / ACA logs), settings and every trace event.
function buildTraceExport() {
  const created = state.traces.find((e) => e.kind === "session.created");
  const cd = (created && created.data) || {};
  return {
    exportedAt: new Date().toISOString(),
    agent: state.agent,
    mode: state.mode,
    session_id: cd.session_id || null,
    agent_session_id: cd.agent_session_id || null,
    voice: state.mode === "voice" ? { voice: state.voice, style: state.style, mic: state.mic } : undefined,
    events: state.traces,
  };
}

function downloadTrace() {
  const blob = new Blob([JSON.stringify(buildTraceExport(), null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `trace-${state.agent}-${state.mode}-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function copyTrace() {
  const btn = $("traceCopy");
  const text = JSON.stringify(buildTraceExport(), null, 2);
  let ok = true;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement("textarea"); // clipboard API unavailable (non-secure context / permissions)
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (_e) { ok = false; }
    ta.remove();
  }
  const label = btn.dataset.label || btn.textContent;
  btn.dataset.label = label;
  btn.textContent = ok ? `✓ Copied ${state.traces.length} events` : "✗ Copy failed";
  clearTimeout(btn._t);
  btn._t = setTimeout(() => (btn.textContent = label), 2000);
}

init();
