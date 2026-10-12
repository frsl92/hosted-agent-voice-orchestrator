/* Token usage and cost per model, for the current conversation. Prices (USD per 1M tokens) are editable and kept in localStorage. */
const Usage = (() => {
  const STORE = "vt_pricing_v1";
  const DEFAULTS = {
    "Decision-1": { input: 0.042, output: 0 },
    "gpt-5.6-luna": { input: 0.22, output: 1.2 },
  };
  const WARNING = "Assumes input cache tokens are 0, so values are not 100% accurate.";

  let el = {}, pricing = load(), rows = new Map(), calls = [];

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (n) => Number(n || 0).toLocaleString("en-US");
  const rate = (n) => "$" + Number(n || 0).toFixed(Number(n) !== 0 && Number(n) < 0.1 ? 3 : 2);
  const money = (x) => (x === 0 ? "$0.00" : x < 0.01 ? "$" + x.toFixed(6) : "$" + x.toFixed(4));

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
      const merged = structuredClone(DEFAULTS);
      for (const [m, p] of Object.entries(saved)) merged[m] = { input: +p.input || 0, output: +p.output || 0 };
      return merged;
    } catch (_) { return structuredClone(DEFAULTS); }
  }

  // Decision-1 deployments can be named anything; fold them into one row
  const canon = (model) => {
    const name = String(model || "unknown");
    if (/decision/i.test(name)) return "Decision-1";
    const known = Object.keys(pricing).find((k) => k.toLowerCase() === name.toLowerCase());
    return known || name;
  };

  function ensureRow(model) {
    if (!pricing[model]) pricing[model] = { input: 0, output: 0 };
    if (!rows.has(model)) rows.set(model, { calls: 0, input: 0, output: 0 });
    return rows.get(model);
  }

  function totals() {
    let cost = 0, input = 0, output = 0, n = 0;
    for (const [m, r] of rows) {
      const p = pricing[m] || { input: 0, output: 0 };
      cost += (r.input * p.input + r.output * p.output) / 1e6;
      input += r.input; output += r.output; n += r.calls;
    }
    return { cost, input, output, calls: n };
  }

  function init(opts) {
    el = opts;
    el.configBtn.onclick = openConfig;
    el.cfgCancel.onclick = () => (el.modal.hidden = true);
    el.cfgSave.onclick = saveConfig;
    el.cfgReset.onclick = () => { pricing = structuredClone(DEFAULTS); renderConfig(); };
    el.modal.onclick = (e) => { if (e.target === el.modal) el.modal.hidden = true; };
    reset();
  }

  function reset() {
    rows = new Map(); calls = [];
    for (const m of Object.keys(pricing)) ensureRow(m);
    render();
  }

  function onTrace(ev) {
    if (ev.source !== "agent" || ev.kind !== "usage") return;
    const d = ev.data || {};
    const r = ensureRow(canon(d.model));
    r.calls++; r.input += d.input_tokens || 0; r.output += d.output_tokens || 0;
    calls.unshift({ model: canon(d.model), purpose: d.purpose || "", input: d.input_tokens || 0, output: d.output_tokens || 0 });
    if (calls.length > 60) calls.pop();
    render();
  }

  function render() {
    const t = totals();
    el.badge.textContent = money(t.cost);
    const body = [...rows.entries()].map(([m, r]) => {
      const p = pricing[m] || { input: 0, output: 0 };
      const ci = (r.input * p.input) / 1e6, co = (r.output * p.output) / 1e6;
      return `<tr><td class="um"><b>${esc(m)}</b><small>${rate(p.input)} in · ${rate(p.output)} out / 1M</small></td>` +
        `<td>${num(r.calls)}</td><td>${num(r.input)}</td><td>${num(r.output)}</td>` +
        `<td class="uc" title="input ${money(ci)} + output ${money(co)}"><b>${money(ci + co)}</b></td></tr>`;
    }).join("");
    const recent = calls.length
      ? calls.map((c) => `<div class="ucall"><span class="pill">${esc(c.model)}</span><span class="up">${esc(c.purpose)}</span><span class="ut">${num(c.input)} in · ${num(c.output)} out</span></div>`).join("")
      : `<div class="pane-empty">Token usage appears here as the models are called.</div>`;
    el.body.innerHTML =
      `<div class="usum"><div><small>Total cost</small><b>${money(t.cost)}</b></div><div><small>Input tokens</small><b>${num(t.input)}</b></div>` +
      `<div><small>Output tokens</small><b>${num(t.output)}</b></div><div><small>Model calls</small><b>${num(t.calls)}</b></div></div>` +
      `<div class="uwarn" title="${esc(WARNING)}">⚠ ${esc(WARNING)}</div>` +
      `<table class="utable"><thead><tr><th>Model</th><th>Calls</th><th>Input</th><th>Output</th><th>Cost</th></tr></thead><tbody>${body}</tbody></table>` +
      `<h3>Calls</h3><div class="ucalls">${recent}</div>`;
  }

  function openConfig() { pricing = load(); for (const m of rows.keys()) ensureRow(m); renderConfig(); el.modal.hidden = false; }

  function renderConfig() {
    el.cfgRows.innerHTML = Object.entries(pricing).map(([m, p]) =>
      `<div class="cfg-row" data-model="${esc(m)}"><b>${esc(m)}</b>` +
      `<label>Input <span class="cur">$</span><input type="number" min="0" step="any" data-k="input" value="${p.input}" /></label>` +
      `<label>Output <span class="cur">$</span><input type="number" min="0" step="any" data-k="output" value="${p.output}" /></label></div>`).join("");
  }

  function saveConfig() {
    const next = {};
    el.cfgRows.querySelectorAll(".cfg-row").forEach((row) => {
      const v = (k) => Math.max(0, parseFloat(row.querySelector(`[data-k=${k}]`).value) || 0);
      next[row.dataset.model] = { input: v("input"), output: v("output") };
    });
    pricing = next;
    localStorage.setItem(STORE, JSON.stringify(pricing));
    for (const m of Object.keys(pricing)) ensureRow(m);
    el.modal.hidden = true;
    render();
  }

  return { init, reset, onTrace };
})();
