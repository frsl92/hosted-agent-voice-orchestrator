/* Visualisation of what happens inside the agents: flow graph / task board, per-turn timeline, Decision-1 calls. */
const Viz = (() => {
  const THRESH = { billing: .6, order_status: .6, other_topic: .6, primary_topic: .6, confirmation: .6, wants_to_end: .7, resolved: .7 };
  const NODES = {
    router: { x: 210, y: 78, up: true, label: "Router", icon: "🧭", color: "#3b9eff" },
    billing: { x: 70, y: 236, label: "Billing", icon: "🧾", color: "#e3b341" },
    orders: { x: 350, y: 236, label: "Orders", icon: "📦", color: "#4cc9a4" },
  };
  const R = 34;
  const TOPIC_ICON = { billing: "🧾", order_status: "📦" };
  const STATUS = {
    queued: ["⏳", "Queued"], running: ["", "Running"], completed: ["✔", "Completed"],
    delivered: ["✔✔", "Told to customer"], failed: ["✖", "Failed"], cancelled: ["⊘", "Cancelled"],
  };
  const PHASES = [["idle", "Idle", "listening"], ["proposing", "Proposing", "waiting for yes/no"], ["running", "Running", "tasks in progress"]];

  let el = {}, onCounts = () => {}, kind = "handoff", gen = 0, chain = Promise.resolve();
  let g, counts, tasks, phase, plan, turns, turnEl, decN, lastDec, cardCount;

  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const short = (v, n = 160) => { const s = typeof v === "string" ? v : JSON.stringify(v); return s.length > n ? s.slice(0, n) + "…" : s; };
  const pct = p => (p * 100).toFixed(p >= .995 || p < .0005 ? 0 : 1) + "%";

  function init(opts) { el = opts; onCounts = opts.onCounts || onCounts; reset("handoff"); }

  function reset(k) {
    gen++; kind = k || kind; chain = Promise.resolve();
    g = { active: null, speaking: null, path: [], queue: [], ended: false, refused: null, edges: {} };
    counts = { handoffs: 0, decisions: 0, tasks: 0 };
    tasks = new Map(); phase = "idle"; plan = null; turns = 0; turnEl = null; decN = 0; lastDec = null; pendingDerived = []; cardCount = 0;
    el.timeline.innerHTML = ""; el.decisions.innerHTML = `<div class="pane-empty">Every call to Microsoft-Decision-1 shows up here, with the exact input it received and what it answered.</div>`;
    el.timeline.innerHTML = `<div class="pane-empty">Turns will be listed here as they happen.</div>`;
    if (kind === "handoff") buildGraph(); else renderBackground();
    emitCounts();
  }

  function emitCounts() { onCounts({ ...counts, kind }); }

  // ---------------------------------------------------------------- handoff graph
  function edgeGeom(a, b) {
    const A = NODES[a], B = NODES[b], dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy), ux = dx / len, uy = dy / len;
    const nx = -uy, ny = ux, off = 16;
    const s = { x: A.x + ux * R + nx * 6, y: A.y + uy * R + ny * 6 }, e = { x: B.x - ux * (R + 6) + nx * 6, y: B.y - uy * (R + 6) + ny * 6 };
    const c = { x: (s.x + e.x) / 2 + nx * off, y: (s.y + e.y) / 2 + ny * off };
    return { s, e, c, mid: { x: .25 * s.x + .5 * c.x + .25 * e.x, y: .25 * s.y + .5 * c.y + .25 * e.y } };
  }
  const key = (a, b) => a + ">" + b;

  function buildGraph() {
    const ids = Object.keys(NODES);
    let edges = "";
    for (const a of ids) for (const b of ids) if (a !== b) {
      const q = edgeGeom(a, b);
      edges += `<path id="e-${key(a, b)}" class="edge" d="M${q.s.x},${q.s.y} Q${q.c.x},${q.c.y} ${q.e.x},${q.e.y}" marker-end="url(#arr)"/>` +
        `<text id="c-${key(a, b)}" class="edge-count" x="${q.mid.x + (q.mid.x - 210) * .06}" y="${q.mid.y + 4}"></text>`;
    }
    const nodes = ids.map(id => {
      const n = NODES[id];
      return `<g class="node" id="n-${id}" style="--nc:${n.color}"><circle class="pulse" cx="${n.x}" cy="${n.y}" r="${R}"/>` +
        `<circle class="body" cx="${n.x}" cy="${n.y}" r="${R}"/><text class="nicon" x="${n.x}" y="${n.y + 8}">${n.icon}</text>` +
        `<text class="nlabel" x="${n.x}" y="${n.up ? n.y - R - 20 : n.y + R + 16}">${n.label}</text><text class="nstate" id="ns-${id}" x="${n.x}" y="${n.up ? n.y - R - 8 : n.y + R + 29}"></text></g>`;
    }).join("");
    el.flow.innerHTML = `<svg class="graph" viewBox="0 0 420 300" role="img" aria-label="Agent handoff graph">` +
      `<defs><marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#5d7aa6"/></marker></defs>` +
      `${edges}${nodes}<g id="tokenLayer"></g></svg><div class="flow-meta" id="flowMeta"></div>`;
    paintGraph();
  }

  function paintGraph() {
    for (const id of Object.keys(NODES)) {
      const n = el.flow.querySelector("#n-" + id); if (!n) continue;
      n.classList.toggle("active", g.active === id);
      n.classList.toggle("speaking", g.speaking === id);
      n.classList.toggle("refused", g.refused === id);
      const st = el.flow.querySelector("#ns-" + id);
      st.textContent = g.speaking === id ? "speaking" : g.active === id ? (g.ended ? "call ended" : "has the call") : "";
    }
    for (const [k, v] of Object.entries(g.edges)) {
      const p = el.flow.querySelector("#e-" + k), t = el.flow.querySelector("#c-" + k);
      if (p) p.classList.add("used"); if (t) t.textContent = "×" + v;
    }
    const meta = el.flow.querySelector("#flowMeta"); if (!meta) return;
    const path = g.path.map(a => `<span class="pill ${a}">${NODES[a]?.label || esc(a)}</span>`).join('<span class="arrow">→</span>');
    const queue = g.queue.map(q => `<span class="pill queue" title="Topic waiting in the queue">⏭ ${esc(typeof q === "string" ? q : q.topic || JSON.stringify(q))}</span>`).join("");
    meta.innerHTML = (path ? `<span class="muted">Path</span><span class="path">${path}</span>` : `<span class="muted">Waiting for the first turn…</span>`) +
      (queue ? `<span class="muted">Queued</span>${queue}` : "") + (g.ended ? `<span class="pill ended">call ended</span>` : "");
  }

  function animateToken(a, b, myGen) {
    return new Promise(res => {
      const layer = el.flow.querySelector("#tokenLayer"), path = el.flow.querySelector("#e-" + key(a, b));
      if (!layer || !path) return res();
      const q = edgeGeom(a, b), tok = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      tok.setAttribute("r", 7); tok.setAttribute("class", "token"); layer.appendChild(tok);
      path.classList.add("flash");
      const t0 = performance.now(), D = 750;
      const step = now => {
        if (myGen !== gen) { tok.remove(); return res(); }
        const t = Math.min(1, (now - t0) / D), u = 1 - t;
        tok.setAttribute("cx", u * u * q.s.x + 2 * u * t * q.c.x + t * t * q.e.x);
        tok.setAttribute("cy", u * u * q.s.y + 2 * u * t * q.c.y + t * t * q.e.y);
        if (t < 1) requestAnimationFrame(step); else { tok.remove(); path.classList.remove("flash"); res(); }
      };
      requestAnimationFrame(step);
    });
  }

  // run graph changes in order so fast bursts of events still read as a sequence
  function enqueue(fn) {
    const myGen = gen;
    chain = chain.then(() => (myGen === gen ? fn(myGen) : null)).catch(() => {});
  }

  function graphEvent(k, d) {
    if (k === "turn.start") {
      enqueue(() => {
        g.active = d.agent; g.speaking = null; g.queue = d.queue || []; g.refused = null;
        if (!g.path.length) g.path = [d.agent];
        paintGraph();
      });
    } else if (k === "decision.guard") {
      enqueue(() => { g.queue = d.queue || g.queue; paintGraph(); });
    } else if (k === "handoff") {
      enqueue(async myGen => {
        if (d.refused) { g.refused = d.target; paintGraph(); await sleep(900); g.refused = null; paintGraph(); return; }
        counts.handoffs++; emitCounts();
        await animateToken(d.source, d.target, myGen);
        if (myGen !== gen) return;
        const ek = key(d.source, d.target); g.edges[ek] = (g.edges[ek] || 0) + 1;
        g.active = d.target; g.path.push(d.target); paintGraph();
      });
    } else if (k === "agent.start") {
      enqueue(() => { g.active = d.agent; g.speaking = d.agent; if (d.ends_call) g.ended = true; paintGraph(); });
    } else if (k === "agent.end") {
      enqueue(() => { g.speaking = null; paintGraph(); });
    } else if (k === "turn.end") {
      enqueue(() => { g.speaking = null; g.queue = d.queue || []; if (d.ended) g.ended = true; paintGraph(); });
    }
  }

  // ---------------------------------------------------------------- background view
  function renderBackground() {
    const idx = PHASES.findIndex(p => p[0] === phase);
    const steps = PHASES.map(([id, name, sub], i) =>
      `<div class="step ${i === idx ? "active" : i < idx ? "done" : ""}">${i < idx ? "✔ " : ""}${name}<small>${sub}</small></div>`).join('<span class="muted">›</span>');
    const planHtml = plan && plan.length ? `<div class="plan"><b>Proposed plan — waiting for the customer to confirm</b>${plan.map(p =>
      `${TOPIC_ICON[p.topic] || "•"} ${esc(p.topic)} ${paramChips(p.params)}`).join("<br>")}</div>` : "";
    const list = [...tasks.values()];
    const cards = list.map(t => {
      const [ic, name] = STATUS[t.status] || ["", t.status];
      return `<div class="task s-${t.status}"><div class="th"><span class="ti">${TOPIC_ICON[t.topic] || "🛠"}</span>${esc(t.topic)}` +
        `<span class="status-badge s-${t.status}">${t.status === "running" ? '<span class="spin"></span>' : ic} ${name}</span></div>` +
        `<div class="tp">${paramChips(t.params)}</div>` +
        (t.result !== undefined ? `<div class="tr">${esc(short(t.result, 220))}</div>` : "") +
        (t.error ? `<div class="tr" style="color:var(--bad)">${esc(t.error)}</div>` : "") +
        `<div class="tm">${esc(t.id)}${t.ms != null ? " · " + t.ms + " ms" : ""}</div></div>`;
    }).join("");
    el.flow.innerHTML = `<div class="stepper">${steps}</div>${planHtml}` +
      `<h3>Background tasks</h3>` + (cards ? `<div class="tasks">${cards}</div>` : `<div class="pane-empty">No tasks yet. They are created once the customer confirms a plan.</div>`);
  }
  const paramChips = p => Object.entries(p || {}).map(([k, v]) => `<span class="pill">${esc(k)}: ${esc(v)}</span>`).join(" ");

  function backgroundEvent(k, d) {
    let changed = true;
    if (k === "turn.start") phase = d.phase || phase;
    else if (k === "turn.end") phase = d.phase || phase;
    else if (k === "phase") {
      phase = d.phase; plan = d.phase === "proposing" ? d.proposed || [] : null;
    } else if (k.startsWith("task.")) {
      const id = d.task_id; let t = tasks.get(id);
      if (!t) { t = { id, topic: d.topic, params: d.params || {}, status: "queued" }; tasks.set(id, t); counts.tasks = tasks.size; emitCounts(); }
      if (d.params && !Object.keys(t.params).length) t.params = d.params;
      if (k === "task.queued") t.status = "queued";
      else if (k === "task.start") t.status = "running";
      else if (k === "task.done") { t.status = "completed"; t.result = d.result; t.ms = d.ms; }
      else if (k === "task.cancelled") { t.status = "cancelled"; t.ms = d.ms; }
      else if (k === "task.failed") { t.status = "failed"; t.error = d.error; t.ms = d.ms; }
      else if (k === "task.delivered") { if (t.status === "completed") t.status = "delivered"; }
    } else changed = false;
    if (changed) renderBackground();
  }

  // ---------------------------------------------------------------- timeline
  function describeStep(k, d) {
    switch (k) {
      case "decision.analyze": return ["🧠", "decision", "Decision-1 analysed the turn", `billing ${pct(d.billing ?? 0)} · order status ${pct(d.order_status ?? 0)} · primary ${d.primary_topic ?? "–"}${d.wants_to_end ? " · wants to end" : ""}${d.ok === false ? " · unclear" : ""}`];
      case "decision.guard": return ["🛡", "decision", d.ok ? "Guard: stay with this agent" : "Guard: customer moved on", `other topic ${pct(d.other_topic ?? 0)} · resolved ${pct(d.resolved ?? 0)}`];
      case "decision.confirmation": return ["🙋", "decision", `Confirmation: ${d.verdict}`, d.wants_to_end ? "customer also wants to end" : ""];
      case "llm.extract": return ["🔎", "decision", "LLM extracted details", short({ order: d.order_id, period: d.invoice_period }, 120)];
      case "handoff": return d.refused ? ["⛔", "err", `Handoff refused ${d.source} → ${d.target}`, ""] : ["🔀", "handoff", `Handoff ${d.source} → ${d.target}`, ""];
      case "agent.start": return ["🗣", "speak", `${d.agent} starts speaking`, d.directive ? short(d.directive, 140) : ""];
      case "agent.end": return ["✅", "speak", `${d.agent} done`, d.first_token_ms != null ? `first token ${d.first_token_ms} ms` : "", d.ms];
      case "agent.error": return ["⚠", "err", `${d.agent} error`, d.error];
      case "plan.error": return ["⚠", "err", "Planning error", d.error];
      case "tool.call": return ["🛠", d.error ? "err" : "tool", `Tool ${d.name}`, d.error ? d.error : `${short(d.args, 80)} → ${short(d.result, 120)}`, d.ms];
      case "phase": return ["🔄", "task", `Phase → ${d.phase}`, d.proposed ? short(d.proposed.map(p => p.topic).join(" + ")) : (d.task_ids || []).join(", ")];
      case "task.queued": return ["⏳", "task", `Task queued: ${d.topic}`, short(d.params, 100)];
      case "task.start": return ["▶", "task", `Task started: ${d.topic}`, ""];
      case "task.done": return ["✔", "task", `Task done: ${d.topic}`, short(d.result, 140), d.ms];
      case "task.delivered": return ["🔔", "task", `Result delivered: ${d.topic}`, d.failed ? "failure reported" : ""];
      case "task.cancelled": return ["⊘", "task", `Task cancelled: ${d.topic}`, ""];
      case "task.failed": return ["✖", "err", `Task failed: ${d.topic}`, d.error];
      default: return null;
    }
  }

  function timelineEvent(k, d) {
    if (k === "turn.start") {
      if (!turns) el.timeline.innerHTML = "";
      turns++;
      turnEl = document.createElement("div"); turnEl.className = "turn";
      turnEl.innerHTML = `<div class="turn-head"><div class="tn">Turn ${turns} · ${esc(d.agent)}${d.phase ? " · phase " + esc(d.phase) : ""}</div><div class="tu">${d.user === "(call connected)" ? "<i>Call connected — greeting</i>" : "“" + esc(d.user) + "”"}</div></div><div class="steps"></div>`;
      el.timeline.prepend(turnEl); return;
    }
    if (k === "decision.call") return;
    const s = describeStep(k, d);
    if (k !== "turn.end" && !s) return;
    if (!turnEl) {
      if (!turns) el.timeline.innerHTML = "";
      turnEl = document.createElement("div"); turnEl.className = "turn";
      turnEl.innerHTML = `<div class="turn-head"><div class="tn">Background activity</div></div><div class="steps"></div>`;
      el.timeline.prepend(turnEl);
    }
    const steps = turnEl.querySelector(".steps");
    if (k === "turn.end") {
      steps.insertAdjacentHTML("beforeend", `<div class="reply">${esc(d.reply || "(no reply)")}<div class="sm muted">${d.ms != null ? d.ms + " ms" : ""}${d.handoffs ? ` · ${d.handoffs} handoff(s)` : ""}${d.ended ? " · call ended" : ""}</div></div>`);
      return;
    }
    const [icon, cls, title, detail, ms] = s;
    steps.insertAdjacentHTML("beforeend", `<div class="stp ${cls}"><span class="si">${icon}</span><span class="st">${esc(title)}${detail ? `<span class="sd">${esc(detail)}</span>` : ""}</span><span class="sm">${ms != null ? ms + " ms" : ""}</span></div>`);
  }

  // ---------------------------------------------------------------- decisions
  function renderTranscript(state) {
    const lines = String(state || "").split("\n").filter(Boolean);
    let last = -1; lines.forEach((l, i) => { if (/^customer:/i.test(l)) last = i; });
    return `<div class="tx">${lines.map((l, i) => {
      const c = /^customer:/i.test(l);
      return `<div class="${c ? "c" : "a"}${i === last ? " latest" : ""}">${esc(l)}</div>`;
    }).join("")}</div>`;
  }

  function renderAnswer(name, a) {
    if (!a) return "";
    const th = THRESH[name];
    if (a.type === "noul" || a.noul !== undefined) {
      const p = a.noul ?? 0, hit = th != null && p >= th;
      return `<div class="ans"><div class="an">${esc(name)}<span class="av">${pct(p)} ${th != null ? (hit ? "✔ yes" : "✘ no") : ""}</span></div>` +
        `<div class="bar ${hit ? "hit" : ""}"><i style="width:${p * 100}%"></i>${th != null ? `<span class="th" style="left:${th * 100}%" title="threshold ${th}"></span>` : ""}</div></div>`;
    }
    const probs = Object.entries(a.probabilities || {}).sort((x, y) => y[1] - x[1]);
    return `<div class="ans"><div class="an">${esc(name)}<span class="pill">${esc(a.choice)}</span><span class="av">confidence ${pct(a.confidence ?? 0)}</span></div>` +
      (probs.length ? `<div class="probs">${probs.map(([l, p]) => `<span>${esc(l)}</span><div class="bar choice"><i style="width:${p * 100}%"></i></div><span>${pct(p)}</span>`).join("")}</div>` : "") + `</div>`;
  }

  function decisionCall(d, t) {
    counts.decisions++; decN++; emitCounts();
    if (decN === 1) el.decisions.innerHTML = "";
    const req = d.request || {}, qs = req.questions || {}, answers = d.answers || {};
    const qHtml = Object.entries(qs).map(([n, q]) => `<div class="q"><span class="qn">${esc(n)}</span><span class="pill qt">${esc(q.type)}</span><span class="qi">${esc(q.instructions)}</span>` +
      (q.criteria ? `<ul>${(Array.isArray(q.criteria) ? q.criteria : Object.entries(q.criteria).map(([a, b]) => `${a}: ${b}`)).map(c => `<li>${esc(typeof c === "string" ? c : JSON.stringify(c))}</li>`).join("")}</ul>` : "") + `</div>`).join("");
    const ansHtml = d.error ? `<div class="err-box">Call failed: ${esc(d.error)}</div>` :
      (d.questions || Object.keys(answers)).map(n => renderAnswer(n, answers[n])).join("");
    const card = document.createElement("div"); card.className = "dec";
    card.innerHTML = `<div class="dec-head"><span class="dn">#${decN}</span><span class="dp">${esc(d.purpose || "decision")}</span>` +
      `<span class="pill">${esc(d.model || req.model || "Microsoft-Decision-1")}</span><span class="dm">${d.ms != null ? d.ms + " ms" : ""}${d.attempts > 1 ? " · " + d.attempts + " attempts" : ""}</span></div>` +
      `<div class="dec-body"><div class="dec-col"><h4>Input · conversation sent</h4>${renderTranscript(req.state)}<h4>Input · questions asked</h4>${qHtml || '<span class="muted">–</span>'}</div>` +
      `<div class="dec-col"><h4>Output · answers</h4>${ansHtml}<div class="derived-slot"></div></div></div>` +
      `<details class="raw"><summary>Raw request / response</summary><pre>${esc(JSON.stringify({ request: req, answers, attempts: d.attempts, ms: d.ms }, null, 2))}</pre></details>`;
    el.decisions.prepend(card);
    lastDec = { purpose: d.purpose || "", card };
    claimPending();
  }

  const DERIVED = { "decision.analyze": p => p === "analyze_turn", "decision.guard": p => p.startsWith("on_topic"), "decision.confirmation": p => p === "confirmation" };
  let pendingDerived = [];

  function paintDerived(card, k, d) {
    const label = { "decision.analyze": "Routing decision", "decision.guard": "Guard result", "decision.confirmation": "Confirmation" }[k];
    const body = k === "decision.analyze" ? `topics: ${(d.topics || []).join(", ") || "none"} · primary: ${d.primary_topic ?? "–"}${d.wants_to_end ? " · wants to end" : ""}${d.ok === false ? " · unclear" : ""}`
      : k === "decision.guard" ? `${d.ok ? "stay with " + d.agent : "leave " + d.agent}${(d.queue || []).length ? " · queue: " + d.queue.join(", ") : ""}`
      : `verdict: ${d.verdict}${d.wants_to_end ? " · wants to end" : ""}`;
    card.querySelector(".derived-slot").innerHTML = `<div class="derived"><b>${label}:</b> ${esc(body)}</div>`;
  }

  // the derived event normally follows its decision.call; if the log lines arrive in the other order, wait for the card
  function derivedFor(k, d) {
    if (!DERIVED[k]) return;
    if (lastDec && DERIVED[k](lastDec.purpose) && !lastDec.card.querySelector(".derived")) paintDerived(lastDec.card, k, d);
    else pendingDerived.push([k, d]);
  }

  function claimPending() {
    pendingDerived = pendingDerived.filter(([k, d]) => {
      if (!lastDec || !DERIVED[k](lastDec.purpose) || lastDec.card.querySelector(".derived")) return true;
      paintDerived(lastDec.card, k, d); return false;
    }).slice(-6);
  }
  // ---------------------------------------------------------------- entry point
  function onTrace(ev) {
    if (ev.source !== "agent") return;
    const k = ev.kind, d = ev.data || {};
    if (k === "decision.call") { decisionCall(d, ev.t); return; }
    derivedFor(k, d);
    timelineEvent(k, d);
    if (kind === "handoff") graphEvent(k, d); else backgroundEvent(k, d);
  }

  return { init, reset, onTrace };
})();
