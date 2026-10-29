"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const usd = (x, d = 4) => {
  if (x == null) return "–";
  if (x > 0 && x < 0.0001) return "<$0.0001";
  if (x > 0 && x < 0.01) return `$${x.toFixed(Math.max(d, 4))}`;
  return `$${x.toFixed(d)}`;
};
const money = (x) => (x == null ? "–" : x >= 1000 ? `$${Math.round(x).toLocaleString()}` : `$${x.toFixed(2)}`);

let CFG = null;
let current = null;      // run id on screen
let es = null;           // EventSource
let refetchTimer = null;
const STEPS = [["scan", "Scan"], ["analyze", "Understand"], ["testgen", "Write tests"], ["eval", "Evaluate"], ["repair", "Repair"], ["decision", "Decide"], ["patch", "Patch"], ["runtime", "Run code"], ["audit", "Audit"]];
const MODEL_LABEL = { lightning: "Lightning", super: "Super", ultra: "Ultra" };

/* theme ----------------------------------------------------------------- */
(function theme() {
  let t = null;
  try { t = localStorage.getItem("sy-theme"); } catch (_) {}
  if (t) document.documentElement.dataset.theme = t;
  $("#themeBtn").onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("sy-theme", next); } catch (_) {}
  };
})();

/* boot ------------------------------------------------------------------ */
async function boot() {
  CFG = await (await fetch("/api/config")).json();
  const live = CFG.live;
  $("#startBtn").disabled = !live;
  $("#codeWrap").hidden = !CFG.needs_code;
  $("#liveNote").textContent = live
    ? `Runs on ${CFG.models.map((m) => m.label).join(", ")} via Token Factory${CFG.tavily ? " · Tavily on" : ""}${CFG.tracing ? " · LangSmith tracing on" : ""}.`
    : "Live runs are off on this server. Recorded runs below show complete migrations.";
  $("#tests").oninput = (e) => ($("#testsOut").textContent = e.target.value);
  $("#target").oninput = (e) => ($("#targetOut").textContent = `${e.target.value}%`);
  $("#runForm").onsubmit = startRun;
  $("#auditForm").onsubmit = runAudit;
  await loadRuns();
  const fromHash = location.hash.slice(1);
  window.addEventListener("hashchange", () => {
    const h = location.hash.slice(1);
    if (h === "tour") window.Tour?.open();
    else { window.Tour?.close(); if (h && h !== current && (window._runs || []).some((r) => r.id === h)) openRun(h); }
  });
  if (fromHash === "tour") {
    window.Tour?.open();
    const featured = window._runs?.find((r) => r.featured);
    if (featured) openRun(featured.id, { keepHash: true });
  } else if (fromHash) {
    const [rid, tab] = fromHash.split(":");          // #<run> or #<run>:<tab> (sites, patch, savings, spend, log)
    openRun(rid, { tab, keepHash: !!tab });
  }
  else {
    const featured = window._runs?.find((r) => r.featured);
    if (featured) openRun(featured.id);
  }
  heroStats();
}

/* hero chips show the latest recorded run's real numbers (never invented) */
function heroStats() {
  const r = (window._runs || []).find((x) => x.featured && x.summary);
  if (!r) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el) (el.querySelector("[data-v]") || el.firstElementChild || el).textContent = v; };
  set("hero-stat-1", `${r.summary.migrated}/${r.summary.sites}`);
  set("hero-stat-2", `${r.summary.saving_pct}%`);
  if (r.usage) set("hero-stat-3", usd(r.usage.cost, 2));
}

async function loadRuns() {
  const runs = await (await fetch("/api/runs")).json();
  window._runs = runs;
  const ul = $("#runList");
  if (!runs.length) { ul.innerHTML = `<li class="muted small">No runs yet.</li>`; return; }
  ul.innerHTML = runs.map((r) => {
    const s = r.summary;
    const meta = s ? `${s.migrated}/${s.sites} calls switched · ${s.saving_pct}% cheaper` : r.status;
    const name = shortSource(r.source).replace(/^github\.com\//, "");
    return `<li><button data-id="${esc(r.id)}" class="${r.id === current ? "active" : ""}">
      <span class="rl-src">${esc(name)}${r.featured ? '<span class="badge">recorded</span>' : ""}</span>
      <span class="rl-meta">${esc(meta)}</span></button></li>`;
  }).join("");
  $$("button", ul).forEach((b) => (b.onclick = () => openRun(b.dataset.id)));
}

async function startRun(ev) {
  ev.preventDefault();
  const body = {
    source: $("#source").value.trim(), tests: +$("#tests").value, target: +$("#target").value / 100,
    budget: +$("#budget").value, tavily: $("#tavily").checked, code: $("#code").value,
  };
  $("#startBtn").disabled = true;
  try {
    const r = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "failed to start");
    await loadRuns();
    openRun(j.id);
  } catch (e) {
    alert(e.message);
  } finally {
    $("#startBtn").disabled = !CFG.live;
  }
}

/* free audit -------------------------------------------------------------- */
async function runAudit(ev) {
  ev.preventDefault();
  const source = $("#auditSource").value.trim();
  stopReplay();
  if (es) { es.close(); es = null; }
  current = null;
  $$("#runList button").forEach((b) => b.classList.remove("active"));
  $("#empty").hidden = true;
  const view = $("#runView");
  view.hidden = false;
  view.innerHTML = `<div class="card"><div class="pending"><span class="spin"></span> Cloning the Python files of <code>${esc(source)}</code> and auditing every LLM call…</div></div>`;
  $("#auditBtn").disabled = true;
  try {
    const r = await fetch("/api/audit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "audit failed");
    view.innerHTML = auditPage(j);
  } catch (e) {
    view.innerHTML = `<div class="card"><h3>Audit failed</h3><p class="bad">${esc(e.message)}</p></div>`;
  } finally {
    $("#auditBtn").disabled = false;
  }
}

function auditPage(a) {
  const sevClass = { CRITICAL: "bad", HIGH: "bad", MEDIUM: "warn", LOW: "muted" };
  const counts = Object.entries(a.counts).filter(([, n]) => n);
  const testable = a.calls.filter((c) => !c.skip);
  const rows = a.findings.map((x) => `<div class="rt-row"><span class="${sevClass[x.severity]}">${esc(x.severity)}</span>
      <div class="rt-body"><b>${esc(x.rule)} ${esc(x.title)}</b> · <code>${esc(x.file)}:${+x.line}</code>${x.function ? ` <span class="muted">${esc(x.function)}</span>` : ""}
      <div class="small">${esc(x.detail)}</div><div class="small muted">Fix: ${esc(x.fix)}</div></div></div>`).join("");
  const calls = a.calls.map((c) => `<tr><td><code>${esc(c.file)}:${+c.line}</code></td><td>${esc(c.function || "(module)")}</td><td><code>${esc(c.sdk)}</code></td><td><code>${esc(c.model || "?")}</code></td><td class="small muted">${c.skip ? "agent loop: not testable with single prompts" : ""}</td></tr>`).join("");
  return `<div class="card" style="margin-bottom:14px"><p class="eyebrow">FREE SECURITY AUDIT</p><h2 style="margin:4px 0">${esc(a.source)}</h2>
      <p class="muted small">${a.calls.length} LLM call${a.calls.length === 1 ? "" : "s"} in ${a.files} Python file${a.files === 1 ? "" : "s"} (${a.lines} lines). Static analysis only: the code was read, never run.</p>
      <p>${counts.length ? counts.map(([s, n]) => `<span class="${sevClass[s]}">${n} ${s.toLowerCase()}</span>`).join(" · ") : '<span class="ok">No findings.</span>'}</p></div>
    ${rows ? `<div class="card runtime" style="margin-bottom:14px"><h3>Findings</h3>${rows}</div>` : ""}
    ${calls ? `<div class="card" style="margin-bottom:14px"><h3>LLM calls found</h3><table class="simple"><thead><tr><th>Where</th><th>Function</th><th>SDK</th><th>Model</th><th></th></tr></thead><tbody>${calls}</tbody></table></div>` : ""}
    <div class="card"><h3>Next</h3><p class="small">Attack each prompt with injection probes on Nemotron, and test which calls can move to a cheaper model:</p>
      <pre class="mono small apply-cmd">pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.2.0"
switchyard setup
switchyard audit --probes        # in the app's folder
switchyard run${testable.length ? `                   # ${testable.length} call${testable.length === 1 ? "" : "s"} to test` : ""}</pre></div>`;
}

/* run view -------------------------------------------------------------- */
async function openRun(id, opts = {}) {
  stopReplay();
  current = id;
  if (!opts.keepHash) history.replaceState(null, "", `#${id}`);
  $$("#runList button").forEach((b) => b.classList.toggle("active", b.dataset.id === id));
  if (es) { es.close(); es = null; }
  const state = await (await fetch(`/api/runs/${id}`)).json();
  $("#empty").hidden = true;
  const view = $("#runView");
  view.hidden = false;
  view.innerHTML = "";
  view.append($("#tplRun").content.cloneNode(true));
  $$(".tabs button", view).forEach((b) => (b.onclick = () => {
    $$(".tabs button", view).forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab", view).forEach((p) => (p.hidden = p.dataset.pane !== b.dataset.tab));
  }));
  render(state);
  setupRunHeader(view, state);
  if (opts.tab) $(`.tabs button[data-tab="${opts.tab}"]`, view)?.click();
  if (state.status === "running") {
    es = new EventSource(`/api/runs/${id}/events`);
    es.onmessage = () => scheduleRefetch(id);
    es.addEventListener("end", () => { es.close(); es = null; scheduleRefetch(id, 0); loadRuns(); });
  }
}

function scheduleRefetch(id, delay = 700) {
  clearTimeout(refetchTimer);
  refetchTimer = setTimeout(async () => {
    if (id !== current) return;
    const s = await (await fetch(`/api/runs/${id}`)).json();
    render(s);
  }, delay);
}

/* Build one view model from the event log (live) and final site records (done). */
function fold(state) {
  const sites = new Map();
  const ensure = (id) => { if (!sites.has(id)) sites.set(id, { id, models: {}, results: {}, phase: "queued" }); return sites.get(id); };
  let phase = "scan";
  for (const ev of state.events || []) {
    if (ev.kind === "scan") for (const h of ev.sites) Object.assign(ensure(h.id), { header: h });
    else if (ev.kind === "site") { ensure(ev.id).phase = ev.phase; phase = ev.phase.split(":")[0]; }
    else if (ev.kind === "analysis") ensure(ev.id).analysis = ev.analysis;
    else if (ev.kind === "investigation") ensure(ev.id).investigation = ev;
    else if (ev.kind === "sources") Object.assign(ensure(ev.id), { sources: ev.sources, query: ev.query });
    else if (ev.kind === "cases") ensure(ev.id).cases = ev.cases;
    else if (ev.kind === "result") { const s = ensure(ev.id); (s.results[ev.model] ||= {})[ev.case] = ev.passed; }
    else if (ev.kind === "model") ensure(ev.id).models[ev.model] = ev;
    else if (ev.kind === "decision") Object.assign(ensure(ev.id), { decision: ev.decision, savings: ev.savings, phase: "done" });
    else if (ev.kind === "site_error") Object.assign(ensure(ev.id), { error: ev.error, phase: "error" });
    else if (ev.kind === "runtime") ensure(ev.id).runtime = ev;
    else if (ev.kind === "step" && ev.step === "rewrite") phase = "patch";
    else if (ev.kind === "step" && ev.step === "runtime") phase = "runtime";
    else if (ev.kind === "step" && ev.step === "audit") phase = "audit";
    else if (ev.kind === "done") phase = "done";
  }
  for (const rec of state.sites || []) {
    const s = ensure(rec.site.id);
    s.full = rec;
    s.header ||= { id: rec.site.id, file: rec.site.file, line: rec.site.line, function: rec.site.function, model: rec.site.model || rec.site.model_expr };
    if (rec.analysis) s.analysis = rec.analysis;
    if (rec.decision) s.decision = rec.decision;
    if (rec.savings) s.savings = rec.savings;
    if (rec.error) s.error = rec.error;
    if (rec.sources) s.sources = rec.sources;
    if (rec.investigation) s.investigation = rec.investigation;
    for (const [mk, e] of Object.entries(rec.evals || {})) {
      s.models[mk] = e;
      s.results[mk] = Object.fromEntries(e.results.map((r) => [r.case, r.passed]));
    }
    if (rec.cases) s.cases = rec.cases;
  }
  for (const r of state.patch?.runtime || []) ensure(r.id).runtime = r;
  return { sites: [...sites.values()], phase };
}

function render(state) {
  const view = $("#runView");
  const k = (name) => $(`[data-k="${name}"]`, view);
  const vm = fold(state);
  window._shown = replay ? replay.full : state;
  const running = state.status === "running";
  const st = k("status");
  st.textContent = { running: "Migrating…", done: "Migration ready", error: "Run failed", budget: "Stopped at budget" }[state.status] || state.status;
  st.classList.toggle("err", state.status === "error" || state.status === "budget");
  k("source").textContent = shortSource(state.source);
  const dur = state.finished ? `${Math.round(state.finished - state.started)}s` : "";
  const sc = state.scan;
  k("meta").innerHTML = [
    sc ? `${sc.calls} LLM call${sc.calls === 1 ? "" : "s"} in ${sc.files} Python files (${sc.loc.toLocaleString()} lines)` : "",
    state.commit ? `commit <code>${esc(state.commit)}</code>` : "",
    dur ? `took ${dur}` : "",
    state.error ? `<span class="bad">${esc(state.error)}</span>` : "",
  ].filter(Boolean).join(" · ");
  const pl = k("patchLink");
  pl.hidden = !(state.patch && state.patch.changed?.length);
  if (!pl.hidden) pl.href = `/api/runs/${state.id}/patch`;

  // stats
  const sum = state.summary;
  const usage = state.usage;
  const doneSites = vm.sites.filter((s) => s.decision);
  const migrated = doneSites.filter((s) => s.decision.action === "migrate");
  const saving = sum ? sum.saving_pct : null;
  k("stats").innerHTML = [
    stat(vm.sites.length || (sc ? sc.calls : "–"), "LLM calls found"),
    stat(`${migrated.length}/${doneSites.length || "–"}`, "switched to Nemotron"),
    stat(saving != null ? `${saving}%` : "…", "cheaper per request", true),
    stat(sum ? sum.repaired : migrated.filter((s) => s.decision.hint).length, "prompts adapted by Ultra"),
    stat(usage ? usd(usage.cost, 3) : "…", "spent running Switchyard"),
  ].join("");

  // steps
  const order = STEPS.map((s) => s[0]);
  const ph = vm.phase === "investigate" ? "analyze" : vm.phase;
  const idx = state.status === "running" ? Math.max(0, order.indexOf(ph)) : order.length;
  k("steps").innerHTML = STEPS.map(([key, label], i) =>
    `<li class="${i < idx ? "on" : i === idx && running ? "active" : ""}">${label}</li>`).join("");

  // sites
  k("sites").innerHTML = vm.sites.map((s) => siteCard(s, state)).join("") || `<div class="pending"><span class="spin"></span> Scanning…</div>`;
  $$(".dot,.case-row", k("sites")).forEach((d) => (d.onclick = () => showCase(state, d.dataset.site, +d.dataset.case)));

  k("patch").innerHTML = patchView(state);
  k("savings").innerHTML = savingsView(state, vm);
  bindCalc(view, state, vm);
  k("spend").innerHTML = spendView(state);
  k("log").textContent = (state.events || []).map(logLine).filter(Boolean).join("\n");
}

const stat = (v, l, hl) => `<div class="stat${hl ? " hl" : ""}"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`;

function chip(mk) {
  if (!mk) return `<span class="chip m-keep">keep original</span>`;
  if (mk.startsWith("source:")) return `<span class="chip m-source" title="The app's current model, run on the same tests for comparison">${esc(mk.slice(7))} · original</span>`;
  const base = mk.split("+")[0];
  return `<span class="chip m-${base}">Nemotron ${MODEL_LABEL[base] || base}${mk.includes("+") ? " + adapted prompt" : ""}</span>`;
}

function siteCard(s, state) {
  const h = s.header || {};
  const a = s.analysis;
  const d = s.decision;
  const loc = `${esc(h.file)}:${h.line}`;
  const route = d
    ? `<div class="route"><span class="from">${esc(h.model || "dynamic")}</span><span class="arrow">→</span>${d.action === "migrate" ? chip(d.choice) : chip(null)}</div>`
    : s.error ? `<span class="bad small">error</span>` : `<div class="pending"><span class="spin"></span>${esc(phaseLabel(s.phase))}</div>`;
  const keys = [...(CFG?.models || []).map((m) => m.key)];
  const rows = [];
  for (const mk of keys) for (const v of [mk, `${mk}+hint`]) if (s.models[v] || (v === mk && a)) rows.push(v);
  for (const k of Object.keys(s.models)) if (k.startsWith("source:")) rows.push(k);
  const nCases = s.cases?.length || 0;
  const table = rows.length ? `<table class="models"><thead><tr><th>Model</th><th>Pass rate</th><th>Score</th><th>p50</th><th title="Token Factory cost for 1,000 requests at the measured token counts">Per 1k calls</th><th>Cases</th></tr></thead><tbody>${rows.map((mk) => {
    const m = s.models[mk];
    const res = s.results[mk] || {};
    const chosen = d && d.choice === mk;
    const n = m?.results ? m.results.length : nCases;
    const perCall = m && n ? m.cost / n : null;
    const pr = m?.pass_rate;
    const cls = pr == null ? "" : pr >= (state.settings?.target ?? .85) ? "" : pr >= .5 ? "mid" : "low";
    const dots = (s.cases || []).map((c) => {
      const v = res[c.id];
      return `<button class="dot ${v === true ? "p" : v === false ? "f" : ""}" title="${esc(c.name)}" data-site="${esc(s.id)}" data-case="${c.id}" data-m="${esc(mk)}"></button>`;
    }).join("");
    return `<tr class="${chosen ? "chosen" : ""}" data-row="${esc(s.id)}:${esc(mk)}"><td>${chip(mk)}</td>
      <td>${m ? `<div class="pr"><div class="bar ${cls}"><i style="width:${Math.round(pr * 100)}%"></i></div><span class="small">${pct(pr)}</span></div>` : `<span class="spin"></span>`}</td>
      <td>${m ? m.score.toFixed(2) : ""}</td><td>${m ? `${m.latency_p50.toFixed(2)}s` : ""}</td>
      <td>${perCall != null ? usd(perCall * 1000, 3) : ""}</td><td><div class="dots">${dots}</div></td></tr>`;
  }).join("")}</tbody></table>` : "";
  const why = d ? `<div class="why ${d.action === "migrate" ? "go" : "keep"}">${esc(d.reason)}${s.savings?.saving_pct ? ` · <b>${s.savings.saving_pct}% cheaper</b> than ${esc(s.savings.source_model || "the original")}` : ""}</div>` : "";
  const vs = d?.vs_source;
  const h2h = vs && vs.chosen_score != null ? `<div class="kv h2h">Head-to-head on the same tests: ${chip(d.choice)} <b>${vs.chosen_score.toFixed(2)}</b> vs ${chip(`source:${vs.model}`)} <b>${vs.score.toFixed(2)}</b> <span class="${vs.delta >= -0.02 ? "ok" : "warn"}">(${vs.delta >= 0 ? "+" : ""}${vs.delta.toFixed(2)})</span></div>`
    : vs?.errors ? `<div class="kv h2h">The original <code>${esc(vs.model)}</code> couldn't be run on ${vs.errors} test${vs.errors === 1 ? "" : "s"} (API error), so there's no head-to-head.</div>`
    : vs ? `<div class="kv h2h">The original <code>${esc(vs.model)}</code> scored <b>${vs.score.toFixed(2)}</b> on the same tests; no Nemotron model came close enough.</div>` : "";
  const hint = d?.hint ? `<div class="hint"><b>Prompt adapted by Ultra:</b> ${esc(d.hint)}</div>` : "";
  const src = s.sources?.length ? `<div class="kv small">Real inputs from <span class="chip tav">Tavily</span>: ${s.sources.slice(0, 3).map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc((x.title || x.url).slice(0, 48))}</a>`).join(", ")}</div>` : "";
  const desc = a ? `<div class="desc"><p>${esc(a.summary)}</p>
    <div class="kv">Task <b>${esc(a.task_type)}</b> · output <b>${esc(a.output?.format || "text")}</b>${a.output?.labels?.length ? ` (${esc(a.output.labels.join(", "))})` : ""}${a.output?.json_keys?.length ? ` · keys <b>${esc(a.output.json_keys.join(", "))}</b>` : ""}</div>
    ${a.parsing ? `<div class="kv">Consumed by: ${esc(a.parsing)}</div>` : ""}
    ${investigationBlock(s.investigation)}
    ${src}${why}${h2h}${hint}${runtimeLine(s.runtime)}${s.error ? `<div class="why keep bad">${esc(s.error)}</div>` : ""}</div>` : `<div class="desc pending"><span class="spin"></span> Reading the call site…</div>`;
  const caseList = s.cases?.length ? `<details class="cases"><summary>${s.cases.length} tests written by Ultra</summary><div class="case-list">${s.cases.map((c) =>
    `<div class="case-row" data-site="${esc(s.id)}" data-case="${c.id}"><span class="muted">#${c.id}</span><span>${esc(c.name)} <span class="kind">${esc(c.kind === "tricky" ? "tricky" : "")}</span><br><span class="muted small">${esc(c.why || "")}</span></span><span>${rows.map((mk) => { const v = (s.results[mk] || {})[c.id]; return `<span class="dot ${v === true ? "p" : v === false ? "f" : ""}" style="display:inline-block" title="${esc(mk)}"></span>`; }).join(" ")}</span></div>`).join("")}</div></details>` : "";
  return `<article class="site"><div class="site-h"><div><span class="fn">${esc(h.function || "(module)")}</span> <span class="loc muted">${loc}</span></div>${route}</div>
    <div class="site-b">${desc}<div>${table}</div></div>${caseList}</article>`;
}

function toolStep(st) {
  const a = st.args || {};
  if (st.tool === "search") return `searched <code>${esc(String(a.pattern || "").slice(0, 40))}</code>`;
  if (st.tool === "read_file") return `read <code>${esc(a.path)}${a.start ? `:${a.start}${a.end ? `-${a.end}` : ""}` : ""}</code>`;
  if (st.tool === "list_files") return "listed the files";
  return esc(st.tool);
}

function investigationBlock(inv) {
  if (!inv || !(inv.downstream || inv.critical?.length || inv.steps?.length)) return "";
  const steps = inv.steps?.length ? `<div class="small muted inv-steps">${inv.steps.map(toolStep).join(" → ")}</div>` : "";
  const who = inv.mode === "agent" ? `${inv.steps?.length || 0} tool call${inv.steps?.length === 1 ? "" : "s"}` : "static search";
  return `<details class="inv"><summary><span class="chip m-super">Super</span> investigated the codebase <span class="muted small">· ${who}${inv.callers?.length ? ` · ${inv.callers.length} caller${inv.callers.length === 1 ? "" : "s"}` : ""}</span></summary>
    ${steps}
    ${inv.downstream ? `<div class="kv">What the app does with it: ${esc(inv.downstream)}</div>` : ""}
    ${inv.critical?.length ? `<div class="kv">Logic depends on: ${inv.critical.map((c) => `<b>${esc(c)}</b>`).join(" · ")}</div>` : ""}
    ${inv.input_examples?.length ? `<div class="kv">Real inputs found: ${inv.input_examples.slice(0, 3).map((x) => `<code>${esc(x.slice(0, 80))}</code>`).join(" ")}</div>` : ""}
  </details>`;
}

const RT = {
  verified: ["ok", "✓", "Ran the patched code"],
  failed: ["bad", "✗", "Patched code failed"],
  unverified: ["warn", "!", "Ran, not confirmed"],
  skipped: ["muted", "–", "Not run"],
};

function runtimeLine(r) {
  if (!r) return "";
  const [cls, icon, label] = RT[r.status] || RT.skipped;
  const extra = r.status === "verified" ? ` · returned <code>${esc(r.returned)}</code> in ${r.seconds}s${r.how === "agent" ? ` · call written by <span class="chip m-super">Super</span>${r.attempts > 1 ? ` (fixed after ${r.attempts - 1} failed attempt${r.attempts > 2 ? "s" : ""})` : ""}` : ""}`
    : ` · ${esc(r.reason)}`;
  return `<div class="runtime ${cls}"><b>${icon} ${label}</b>${extra}</div>`;
}

function auditView(state) {
  const au = state.audit;
  const skipped = state.skipped || [];
  if (!au && !skipped.length) return "";
  const f = (au && au.findings) || [];
  const sevClass = { CRITICAL: "bad", HIGH: "bad", MEDIUM: "warn", LOW: "muted" };
  const counts = ["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((s) => [s, f.filter((x) => x.severity === s).length]).filter(([, n]) => n);
  const rows = f.slice(0, 40).map((x) => `<div class="rt-row"><span class="${sevClass[x.severity]}">${esc(x.severity)}</span>
      <div class="rt-body"><b>${esc(x.rule)} ${esc(x.title)}</b> · <code>${esc(x.file)}:${x.line}</code>${x.function ? ` <span class="muted">${esc(x.function)}</span>` : ""}
      <div class="small">${esc(x.detail)}</div>
      <div class="small muted">Fix: ${esc(x.fix)}${x.fix_verified ? ' <span class="ok">✓ re-tested, the attack no longer works</span>' : ""}</div>
      ${x.evidence ? `<details class="small"><summary>What the model answered</summary><pre class="mono small" style="white-space:pre-wrap">${esc(x.evidence)}</pre></details>` : ""}</div></div>`).join("");
  const probed = au && au.probed ? `<p class="small">Injection, system-prompt-leak and exfiltration probes ran on the new model of ${au.probed} switched call(s) — ${f.some((x) => x.rule.startsWith("PR")) ? "see the PR findings." : '<span class="ok">none got through.</span>'}</p>` : "";
  const skip = skipped.length ? `<div class="small" style="margin-top:10px"><b>Not tested</b>${skipped.map((k) => `<div><code>${esc(k.file)}:${k.line}</code> ${esc(k.function || "")}: <span class="muted">${esc(k.reason)}</span></div>`).join("")}</div>` : "";
  return `<div class="card runtime" style="margin-bottom:14px"><h3>Security audit</h3>
    ${au ? `<p class="small">${counts.length ? counts.map(([s, n]) => `<span class="${sevClass[s]}">${n} ${s.toLowerCase()}</span>`).join(" · ") : '<span class="ok">No findings.</span>'}
      <span class="muted"> · static checks for leaked keys, model output reaching eval/shell/SQL, user input in system prompts, missing timeouts and token caps.</span></p>` : ""}
    ${probed}${au && au.note ? `<p class="small warn">${esc(au.note)}</p>` : ""}${rows}${skip}</div>`;
}

function runtimeView(state) {
  const rt = state.patch?.runtime;
  if (!rt) {
    const off = state.settings && state.settings.execute === false;
    return state.patch ? `<div class="card"><h3>Ran the patched code</h3><p class="small muted">${off ? "Code execution was off for this run (no API key, <code>--no-exec</code>, or the public demo)." : "Not run."}</p></div>` : "";
  }
  const n = rt.filter((r) => r.status === "verified").length;
  const rows = rt.map((r) => {
    const [cls, icon] = RT[r.status] || RT.skipped;
    const reqs = (r.requests || []).map((q) => `<div class="mono small">POST ${esc(q.url)} · model <b>${esc(q.model || "?")}</b></div>`).join("");
    return `<details class="rt-row"><summary><span class="${cls}">${icon}</span> <code>${esc(r.file)}:${r.line}</code> <b>${esc(r.function || "")}</b> <span class="muted small">${esc(r.reason || "")}</span></summary>
      <div class="rt-body">${r.case ? `<div class="small muted">Test case: ${esc(r.case)} · call ${r.how === "agent" ? "written by Nemotron Super" : "built from the signature"}${r.attempts ? ` · ${r.attempts} attempt${r.attempts > 1 ? "s" : ""}` : ""}</div>` : ""}
      ${r.code ? `<pre class="mono small">${esc(r.code)}</pre>` : ""}${reqs}
      ${r.preview ? `<div class="small"><b>Returned</b></div><pre class="mono small">${esc(r.preview)}</pre>` : ""}</div></details>`;
  }).join("");
  return `<div class="card" style="margin-bottom:14px"><h3>Ran the patched code · ${n}/${rt.length} verified</h3>
    <p class="small muted">Switchyard loaded each migrated module (definitions only, no demo code), called the switched function with a test case, and recorded every HTTP request. Verified = the function returned normally and its request went to Token Factory with the chosen model.</p>${rows}</div>`;
}

function phaseLabel(p) {
  if (!p) return "Queued";
  if (p === "queued") return "Queued";
  if (p === "investigate") return "Super is reading the codebase…";
  if (p === "analyze") return "Understanding the call…";
  if (p === "testgen") return "Ultra is writing tests…";
  if (p.startsWith("eval")) return "Testing models…";
  if (p.startsWith("repair")) return `Ultra is adapting the prompt for ${p.split(":")[1]}…`;
  return p;
}

function showCase(state, siteId, caseId) {
  const rec = (state.sites || []).find((x) => x.site.id === siteId);
  if (!rec || !rec.cases) return;
  const c = rec.cases.find((x) => x.id === caseId);
  const outs = Object.entries(rec.evals || {}).map(([mk, e]) => [mk, e.results.find((r) => r.case === caseId)]).filter((x) => x[1]);
  $("#caseBody").innerHTML = `<h2>#${c.id} ${esc(c.name)} <span class="muted small">${esc(c.kind)}</span></h2>
    <p class="muted small">${esc(c.why)}</p>
    <div class="io"><div><b class="small">Inputs</b><pre>${esc(Object.entries(c.inputs).map(([k, v]) => `{{${k}}}\n${v}`).join("\n\n"))}</pre></div>
    <div><b class="small">Reference answer (Ultra, reasoning on)</b><pre>${esc(c.reference)}</pre></div></div>
    <div class="out">${outs.map(([mk, r]) => `<div><div class="o-h">${chip(mk)}<span class="${r.passed ? "ok" : "bad"}">${r.passed ? "pass" : "fail"} · score ${r.score.toFixed(2)} · ${r.latency.toFixed(2)}s</span></div>
      <pre>${esc(r.output || r.error || "")}</pre><div class="small muted">${esc(r.reason || "")}${(r.checks || []).filter((x) => !x.ok).map((x) => ` · ✗ ${esc(x.name)}${x.detail ? ` (${esc(x.detail)})` : ""}`).join("")}</div></div>`).join("")}</div>`;
  $("#caseDlg").showModal();
}

function patchView(state) {
  const p = state.patch;
  if (!p) return `<p class="muted">${state.status === "running" ? "The patch is written after every call site is tested." : "No patch was produced."}</p>`;
  const compile = (p.compile || []).map((c) => `<div>${c.ok ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'} <code>${esc(c.file)}</code> ${c.ok ? "parses and compiles" : esc(c.error)}</div>`).join("") || `<div class="muted">No files changed.</div>`;
  const routes = (p.routes || []).map((r) => `<tr><td><code>${esc(r.file)}:${r.line}</code><br><span class="muted small">${esc(r.function || "")}</span></td><td><code>${esc(r.from)}</code></td><td><code>${esc(r.to)}</code>${r.hint ? '<br><span class="small m-ultra">+ adapted prompt</span>' : ""}</td><td><code>${esc(r.env)}</code></td></tr>`).join("");
  const manual = (p.manual || []).length ? `<div class="card"><h3>Needs a manual step</h3>${p.manual.map((m) => `<div class="small"><code>${esc(m.file)}:${m.line}</code> (${esc(m.sdk)} SDK) → <code>${esc(m.model)}</code>${m.reason ? `<br><span class="muted">${esc(m.reason)}</span>` : ""}</div>`).join("")}<p class="small muted">These calls passed the tests but use an API shape Switchyard doesn't rewrite automatically, so it reports the target model instead.</p></div>` : "";
  const notes = (state.notes || []).length ? `<div class="card"><h3>Endpoint notes</h3>${state.notes.map((n) => `<div class="small">• ${esc(n)}</div>`).join("")}<p class="small muted">Switchyard fell back automatically and kept going.</p></div>` : "";
  const pr = state.pr_body ? `<details class="card pr-body" style="margin-bottom:14px"><summary><b>Pull-request description</b> <span class="muted small">written by Switchyard · used by <code>switchyard pr</code></span></summary>
    <div class="card-h" style="margin-top:10px"><span class="muted small">Markdown</span><button class="copy-btn" data-copy="pr">Copy</button></div>
    <pre class="mono small" style="white-space:pre-wrap;margin:0;max-height:420px;overflow:auto">${esc(state.pr_body)}</pre></details>` : "";
  return `<div class="panel-grid">
    <div class="card"><h3>Verification</h3>${compile}${p.tests ? `<div>${p.tests.ok ? '<span class="ok">✓</span> project tests pass' : '<span class="bad">✗</span> project tests failed'}</div>` : ""}
      <p class="small muted">Strategy: ${p.mode === "dedicated-client" ? "calls that stay on the original provider keep their client; migrated calls use a dedicated Token Factory client." : "every call migrates, so the existing clients point at Token Factory."} Each model can be overridden with an env var, so rollback is one line.</p></div>
    <div class="card"><div class="card-h"><h3>.env for the migrated app</h3><button class="copy-btn" data-copy="env">Copy</button></div><pre class="mono small" style="white-space:pre-wrap;margin:0">${esc(p.env || "")}</pre></div>
    ${p.changed?.length ? `<div class="card"><div class="card-h"><h3>Apply it to your repo</h3><button class="copy-btn" data-copy="apply">Copy</button></div>
      <pre class="mono small apply-cmd">${esc(applyCmd(state))}</pre>
      <p class="small muted">Run it from the folder Switchyard scanned. Review the diff first; every model sits behind an env var, so rolling back one call is a one-line change.</p></div>` : ""}
    ${manual}${notes}
  </div>
  ${runtimeView(state)}
  ${auditView(state)}
  ${pr}
  ${routes ? `<div class="card" style="margin-bottom:14px"><table class="simple"><thead><tr><th>Call site</th><th>Was</th><th>Now</th><th>Override</th></tr></thead><tbody>${routes}</tbody></table></div>` : ""}
  <div class="diff-wrap"><button class="copy-btn diff-copy" data-copy="diff">Copy patch</button><div class="diff">${diffHtml(p.diff || "")}</div></div>`;
}

function applyCmd(state) {
  return `curl -L ${location.origin}/api/runs/${state.id}/patch -o switchyard.patch\ngit apply --check switchyard.patch && git apply switchyard.patch`;
}

function shortSource(src) {
  const s = String(src || "").replace(/^https?:\/\/(www\.)?/, "").replace(/\.git$/, "");
  if (/^[a-z0-9.-]+\.[a-z]{2,}\//i.test(s)) return s.replace(/\/tree\/[^/]+\//, " › ");
  const parts = s.split(/[\\/]+/).filter(Boolean);           // local path: last two segments
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : s;
}

function cliCmd(state) {
  const st = state.settings || {};
  const src = /\s/.test(state.source) ? `"${state.source}"` : state.source;
  return `switchyard run ${src} --tests ${st.tests ?? 8} --target ${st.target ?? 0.85} --budget ${st.budget ?? 2}${st.tavily === false ? " --no-tavily" : ""}`;
}

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(ta); ta.select(); try { document.execCommand("copy"); } catch (_) {} ta.remove();
  }
  if (btn) { const o = btn.textContent; btn.textContent = "Copied ✓"; btn.classList.add("done"); setTimeout(() => { btn.textContent = o; btn.classList.remove("done"); }, 1400); }
}

/* one delegated handler for every copy button inside the run view */
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b || !window._shown) return;
  const s = window._shown;
  const text = { diff: s.patch?.diff, env: s.patch?.env, apply: s.patch && applyCmd(s), cli: cliCmd(s), pr: s.pr_body }[b.dataset.copy];
  if (text) copyText(text, b);
});

/* run header: CLI equivalent + replay controls (built once per opened run) */
function setupRunHeader(view, state) {
  const head = $(".run-head", view);
  const actions = $(".run-actions", view);
  const cli = document.createElement("div");
  cli.className = "cli-line";
  cli.innerHTML = `<span class="muted small">CLI</span><code>${esc(cliCmd(state))}</code><button class="copy-btn" data-copy="cli">Copy</button>`;
  $(".run-title", head).after(cli);
  if (state.status !== "running" && (state.events || []).length > 3) {
    const wrap = document.createElement("div");
    wrap.className = "replay-ctl";
    wrap.innerHTML = `<button class="btn replay-btn" type="button">▶ Replay run</button><button class="btn speed-btn" type="button" title="Replay speed" hidden>1×</button>`;
    actions.prepend(wrap);
    const btn = $(".replay-btn", wrap), sp = $(".speed-btn", wrap);
    btn.onclick = () => (replay ? stopReplay(true) : startReplay(state, btn, sp));
    sp.onclick = () => { if (!replay) return; replay.speed = replay.speed === 1 ? 4 : 1; sp.textContent = `${replay.speed}×`; };
  }
}

/* replay ---------------------------------------------------------------- */
let replay = null;
const REPLAY_SECONDS = 24;

function partialState(full, n) {
  const events = full.events.slice(0, n);
  const scanned = events.some((e) => e.kind === "scan");
  return { ...full, status: "running", events, sites: [], patch: null, summary: null, usage: null, finished: null,
           scan: scanned ? full.scan : null, error: null };
}

function startReplay(full, btn, sp) {
  stopReplay();
  const evs = full.events;
  const total = Math.max(0.001, evs[evs.length - 1].t - evs[0].t);
  const uniform = (REPLAY_SECONDS * 1000) / evs.length;
  const seen = new Set();
  const view = $("#runView");
  replay = { id: current, i: 0, speed: 1, timer: null, btn, sp, full };
  btn.textContent = "■ Stop replay"; sp.hidden = false; sp.textContent = "1×";
  view.classList.add("replaying");
  // hold the layout so the page doesn't jump while the run view starts empty
  view.style.minHeight = `${view.offsetHeight}px`;
  const top = $(".run-head", view).getBoundingClientRect().top;
  if (top < 0 || top > window.innerHeight * 0.6) (window.SY_scrollTo ? window.SY_scrollTo("#runView") : view.scrollIntoView());
  const tick = () => {
    if (!replay || replay.id !== current) return;
    replay.i += 1;
    const i = replay.i;
    render(partialState(full, i));
    // pop newly resolved dots / rows so the bake-off visibly fills in
    $$(".sites .dot.p[data-m], .sites .dot.f[data-m]", view).forEach((d) => {
      const key = `${d.dataset.site}:${d.dataset.m}:${d.dataset.case}`;
      if (!seen.has(key)) { seen.add(key); d.classList.add("pop"); }
    });
    $$(".sites tr[data-row]", view).forEach((r) => { if (!seen.has(r.dataset.row)) { seen.add(r.dataset.row); r.classList.add("row-in"); } });
    if (i >= evs.length) { finishReplay(); return; }
    const dt = evs[i].t - evs[i - 1].t;
    const real = total > 2 ? (dt / total) * REPLAY_SECONDS * 1000 : uniform;
    const delay = Math.max(35, Math.min(1400, real * 0.6 + uniform * 0.4)) / replay.speed;
    replay.timer = setTimeout(tick, delay);
  };
  render(partialState(full, 0));
  replay.timer = setTimeout(tick, 400);
}

function finishReplay() {
  if (!replay) return;
  const { full } = replay;
  stopReplay();
  render(full);
}

function stopReplay(restore) {
  if (!replay) return;
  clearTimeout(replay.timer);
  const { btn, sp, full, id } = replay;
  replay = null;
  if (btn) btn.textContent = "▶ Replay run";
  if (sp) sp.hidden = true;
  const rv = $("#runView");
  rv?.classList.remove("replaying");
  if (rv) rv.style.minHeight = "";
  if (restore && id === current) render(full);
}

function diffHtml(d) {
  if (!d) return `<div class="muted">No changes.</div>`;
  return d.split("\n").map((l) => {
    const cls = l.startsWith("+++") || l.startsWith("---") ? "file" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    return `<div class="${cls}">${esc(l) || " "}</div>`;
  }).join("");
}

function savingsView(state, vm) {
  const migrated = vm.sites.filter((s) => s.decision?.action === "migrate" && s.savings?.source_cost_per_call);
  if (!migrated.length) return `<p class="muted">${state.status === "running" ? "Savings appear as call sites are decided." : "No call sites were switched."}</p>`;
  const rows = vm.sites.filter((s) => s.decision).map((s) => {
    const sv = s.savings || {};
    return `<tr><td><b>${esc(s.header?.function || "")}</b> <span class="muted small">${esc(s.header?.file)}:${s.header?.line}</span></td>
      <td><code>${esc(sv.source_model || s.header?.model || "dynamic")}</code></td><td>${s.decision.action === "migrate" ? chip(s.decision.choice) : chip(null)}</td>
      <td>${sv.avg_tokens_in ?? "–"} / ${sv.avg_tokens_out ?? "–"}</td><td>${usd(sv.source_cost_per_call, 6)}</td><td>${usd(sv.new_cost_per_call, 6)}</td>
      <td><b>${sv.saving_pct ? `${sv.saving_pct}%` : "–"}</b></td></tr>`;
  }).join("");
  return `<div class="card calc"><h3>What this means at your volume</h3>
    <label class="small muted">Requests per month to each call site: <b id="volOut">100,000</b>
      <input type="range" id="vol" min="3" max="8" step="0.1" value="5"></label>
    <div class="money"><div class="stat"><div class="v" id="mBefore">–</div><div class="l">per month today</div></div>
      <div class="stat hl"><div class="v" id="mAfter">–</div><div class="l">per month on Nemotron</div></div>
      <div class="stat"><div class="v" id="mSave">–</div><div class="l">saved per year</div></div></div>
    <p class="small muted">Token counts are measured on the test set. Original-model prices are public list prices; unknown models are priced like gpt-4o. Calls kept on the original model are excluded.</p></div>
    <div class="card" style="margin-top:14px"><table class="simple"><thead><tr><th>Call site</th><th>Was</th><th>Now</th><th>Avg tokens in/out</th><th>Cost/call before</th><th>After</th><th>Saving</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function bindCalc(view, state, vm) {
  const r = $("#vol", view);
  if (!r) return;
  const migrated = vm.sites.filter((s) => s.decision?.action === "migrate" && s.savings?.source_cost_per_call);
  const before = migrated.reduce((a, s) => a + s.savings.source_cost_per_call, 0);
  const after = migrated.reduce((a, s) => a + s.savings.new_cost_per_call, 0);
  const upd = () => {
    const v = Math.round(10 ** +r.value);
    $("#volOut", view).textContent = v.toLocaleString();
    $("#mBefore", view).textContent = money(before * v);
    $("#mAfter", view).textContent = money(after * v);
    $("#mSave", view).textContent = money((before - after) * v * 12);
  };
  r.oninput = upd;
  upd();
}

function spendView(state) {
  const u = state.usage;
  if (!u) return `<p class="muted">Spend appears when the run finishes.</p>`;
  const roleName = { investigate: "Investigate the codebase", harness: "Write calls for the patched code", audit: "Attack prompts (security probes)", analyze: "Understand call sites", testgen: "Write tests + reference answers", judge: "Grade outputs", repair: "Adapt prompts" };
  const rows = Object.entries(u.by_role).sort((a, b) => b[1].cost - a[1].cost).map(([role, r]) => {
    const label = role.startsWith("candidate:") ? `Candidate runs: ${MODEL_LABEL[role.split(":")[1]] || role}` : roleName[role] || role;
    return `<tr><td>${esc(label)}</td><td>${chip(r.model)}</td><td>${r.calls}</td><td>${r.tokens_in.toLocaleString()}</td><td>${r.tokens_out.toLocaleString()}</td><td>${usd(r.cost, 4)}</td></tr>`;
  }).join("");
  return `<div class="card"><h3>Switchyard runs on Nemotron too</h3>
    <p class="small muted">Each job goes to the model that fits it: Super for fast analysis and grading, Ultra with reasoning only where quality matters most (writing tests, adapting prompts).</p>
    <table class="simple"><thead><tr><th>Job</th><th>Model</th><th>Calls</th><th>Tokens in</th><th>Tokens out</th><th>Cost</th></tr></thead><tbody>${rows}
    <tr><td><b>Total</b></td><td></td><td><b>${u.calls}</b></td><td><b>${u.tokens_in.toLocaleString()}</b></td><td><b>${u.tokens_out.toLocaleString()}</b></td><td><b>${usd(u.cost, 4)}</b></td></tr></tbody></table></div>`;
}

function logLine(ev) {
  const t = `[${String(ev.t.toFixed(1)).padStart(6)}s]`;
  switch (ev.kind) {
    case "step": return `${t} ${ev.message}`;
    case "scan": return `${t} found ${ev.calls} LLM calls in ${ev.files} files`;
    case "analysis": return `${t} ${ev.id} ${ev.analysis.task_type}: ${ev.analysis.summary}`;
    case "sources": return `${t} ${ev.id} Tavily: ${ev.sources.length} sources for "${ev.query}"`;
    case "cases": return `${t} ${ev.id} ${ev.cases.length} tests written`;
    case "model": return `${t} ${ev.id} ${ev.model.padEnd(15)} pass ${pct(ev.pass_rate)} score ${ev.score.toFixed(2)}`;
    case "decision": return `${t} ${ev.id} → ${ev.decision.choice || "keep"} (${ev.decision.reason})`;
    case "site_error": return `${t} ${ev.id} ERROR ${ev.error}`;
    case "patch": return `${t} patch: ${ev.changed.length} files, compile ${ev.ok ? "ok" : "FAILED"}`;
    case "done": return `${t} ${ev.status}${ev.error ? `: ${ev.error}` : ""}`;
    default: return "";
  }
}

boot();
