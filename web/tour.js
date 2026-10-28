"use strict";
/* Judge tour: a 7-chapter scrollytelling walk through one real recorded run.
   Every number comes from the run's JSON; nothing is invented. */
(function () {
  const $t = (s, el = document) => el.querySelector(s);
  const $$t = (s, el = document) => [...el.querySelectorAll(s)];
  const RM = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let built = null;      // run id the tour was built for
  let io = null;

  const CHAPTERS = [
    ["problem", "The problem"],
    ["closeup", "One call, up close"],
    ["tests", "Tests Ultra wrote"],
    ["bakeoff", "The bake-off"],
    ["repair", "Repair"],
    ["patch", "The patch"],
    ["bill", "The bill"],
  ];

  async function pickRun() {
    const runs = window._runs || (await (await fetch("/api/runs")).json());
    let r = runs.find((x) => x.featured && x.summary) || runs.find((x) => x.featured);
    let banner = "";
    if (!r) {
      r = runs.find((x) => x.summary);
      if (r) banner = r.id.startsWith("mock-") ? "Showing a simulated run (no recorded run yet)" : "Showing the newest run (no recorded run is featured yet)";
    } else if (r.id.startsWith("mock-")) banner = "Showing a simulated run (no recorded run yet)";
    if (!r) return { state: null, banner: "No recorded run yet" };
    const state = await (await fetch(`/api/runs/${r.id}`)).json();
    return { state, banner };
  }

  function pickSite(state) {
    const done = (state.sites || []).filter((s) => s.status === "done" && s.decision);
    return done.find((s) => s.decision.hint) ||
      done.find((s) => s.decision.action === "migrate" && s.decision.model === "lightning") ||
      done.find((s) => s.decision.action === "migrate") || done[0] || null;
  }

  const modelOf = (s) => s.site.model || s.site.model_expr || "dynamic";
  const fnOf = (s) => s.site.function || "(module)";
  const tmpl = (text) => esc(text).replace(/\{\{([^{}]+)\}\}/g, '<mark class="var">{{$1}}</mark>');
  const kpi = (v, l, cls = "") => `<div class="t-kpi ${cls}"><b data-count="${esc(v)}">${esc(v)}</b><span>${esc(l)}</span></div>`;

  /* chapter builders ----------------------------------------------------- */
  function chProblem(st) {
    const sites = st.sites || [];
    const models = {};
    sites.forEach((s) => { const m = modelOf(s); models[m] = (models[m] || 0) + 1; });
    const top = Object.entries(models).sort((a, b) => b[1] - a[1]);
    const sc = st.scan || {};
    return {
      title: `${sc.calls ?? sites.length} LLM calls. ${top.length === 1 ? "One model for all of them." : `${top.length} frontier models, no routing.`}`,
      text: `Switchyard read ${sc.files ?? "–"} Python files (${(sc.loc ?? 0).toLocaleString()} lines) without running any of them, and found every OpenAI / Anthropic SDK call, with its prompt template, parameters and output schema. Today each one goes to ${top.map(([m]) => `<code>${esc(m)}</code>`).join(", ")}, whether it routes a request or writes an essay.`,
      visual: `<div class="t-kpis">${kpi(sc.calls ?? sites.length, "LLM calls found")}${kpi(sc.files ?? "–", "Python files")}${kpi(top.length, top.length === 1 ? "model used" : "models used")}</div>
        <ul class="t-calls">${sites.map((s) => `<li><span class="t-fn">${esc(fnOf(s))}</span><span class="t-loc">${esc(s.site.file)}:${s.site.line}</span><span class="t-track"></span><code class="t-model">${esc(modelOf(s))}</code></li>`).join("")}</ul>`,
    };
  }

  function chCloseup(st, s) {
    const a = s.analysis || {};
    const out = a.output || {};
    return {
      title: `${fnOf(s)}: ${a.summary || "one call site"}`,
      text: `<b>Nemotron Super</b> read this call and wrote down its contract: a <b>${esc(a.task_type || "?")}</b> task whose output is <b>${esc(out.format || "text")}</b>${out.labels?.length ? ` (one of ${out.labels.map((x) => `<code>${esc(x)}</code>`).join(", ")})` : ""}${out.json_keys?.length ? ` with keys ${out.json_keys.map((x) => `<code>${esc(x)}</code>`).join(", ")}` : ""}. ${a.parsing ? `The app consumes it like this: ${esc(a.parsing)}.` : ""} That contract is what every cheaper model is held to.`,
      visual: `<div class="t-code"><div class="t-cap">${esc(s.site.file)}:${s.site.line} · original call</div><pre>${esc(s.site.snippet || "")}</pre></div>
        <div class="t-prompt"><div class="t-cap">Prompt template · <mark class="var">{{runtime values}}</mark></div>${(s.site.messages || []).map((m) => `<div class="t-msg"><span class="t-role">${esc(m.role)}</span><div>${tmpl(m.content)}</div></div>`).join("")}</div>`,
    };
  }

  function chTests(st, s) {
    const cases = s.cases || [];
    const tricky = cases.filter((c) => c.kind === "tricky").length;
    return {
      title: `${cases.length} tests, ${tricky} of them designed to trip a model up.`,
      text: `<b>Nemotron Ultra</b>, with reasoning on, wrote typical and tricky cases for this exact call and the ideal answer for each.${(s.sources || []).length ? ` <b>Tavily</b> supplied real-world source material so inputs look like production traffic.` : ""} Open any case to see the input and the reference answer.`,
      visual: `<div class="t-cases">${cases.map((c) => `<details class="t-case"><summary><span class="t-cid">#${c.id}</span><span class="t-cname">${esc(c.name)}</span>${c.kind === "tricky" ? '<span class="t-tag">tricky</span>' : ""}<span class="t-why">${esc(c.why || "")}</span></summary>
          <div class="t-case-b"><div><div class="t-cap">Input</div><pre>${esc(Object.entries(c.inputs || {}).map(([k, v]) => `{{${k}}}\n${v}`).join("\n\n"))}</pre></div>
          <div><div class="t-cap">Reference answer</div><pre>${esc(c.reference)}</pre></div></div></details>`).join("") || '<p class="muted">No cases recorded.</p>'}</div>
        ${(s.sources || []).length ? `<div class="t-sources"><span class="chip tav">Tavily</span> ${s.sources.slice(0, 4).map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc((x.title || x.url).slice(0, 50))}</a>`).join(" · ")}</div>` : ""}`,
    };
  }

  function chBakeoff(st, s) {
    const target = st.settings?.target ?? 0.85;
    const order = ["lightning", "lightning+hint", "super", "super+hint", "ultra", "ultra+hint"].filter((k) => s.evals?.[k]);
    const maxLat = Math.max(0.001, ...order.map((k) => s.evals[k].latency_p50));
    const per1k = (e) => (e.results?.length ? (e.cost / e.results.length) * 1000 : null);
    const maxCost = Math.max(1e-9, ...order.map((k) => per1k(s.evals[k]) || 0));
    const d = s.decision;
    const rows = order.map((k) => {
      const e = s.evals[k];
      const c = per1k(e);
      const pass = e.pass_rate >= target;
      return `<div class="t-row ${d.choice === k ? "chosen" : ""}">
        <div class="t-rh">${chip(k)}${d.choice === k ? '<span class="t-win">chosen</span>' : ""}</div>
        <div class="t-metric"><span>pass rate</span><div class="t-bar ${pass ? "" : "under"}"><i style="--w:${Math.round(e.pass_rate * 100)}%"></i><em class="t-target" style="left:${target * 100}%"></em></div><b>${pct(e.pass_rate)}</b></div>
        <div class="t-metric"><span>score</span><div class="t-bar alt"><i style="--w:${Math.round(e.score * 100)}%"></i></div><b>${e.score.toFixed(2)}</b></div>
        <div class="t-metric"><span>p50 latency</span><div class="t-bar lat"><i style="--w:${Math.round((e.latency_p50 / maxLat) * 100)}%"></i></div><b>${e.latency_p50.toFixed(2)}s</b></div>
        <div class="t-metric"><span>per 1k calls</span><div class="t-bar cost"><i style="--w:${c == null ? 0 : Math.max(2, Math.round((c / maxCost) * 100))}%"></i></div><b>${c == null ? "–" : usd(c, 3)}</b></div>
      </div>`;
    }).join("");
    return {
      title: d.action === "migrate" ? `${chipText(d.choice)} wins.` : "No Nemotron model cleared the bar, so this call stays put.",
      text: `Every candidate answered every case. Hard checks run in code (valid JSON, schema, labels, exact fields), and a <b>Nemotron Super</b> grader compares each answer with the reference. The rule: the <b>cheapest</b> model whose pass rate is at least <b>${pct(target)}</b> <i>and</i> whose mean score is within 0.05 of the best model wins.${d.reason ? ` In this case: ${esc(d.reason)}.` : ""}`,
      visual: `<div class="t-bake">${rows}</div><p class="t-note">Line on each pass-rate bar = the ${pct(target)} quality bar.</p>`,
    };
  }

  function chipText(k) {
    const base = String(k || "").split("+")[0];
    return `Nemotron ${MODEL_LABEL[base] || base}${String(k).includes("+") ? " with an adapted prompt" : ""}`;
  }

  function chRepair(st) {
    const s = (st.sites || []).find((x) => x.decision?.hint && x.evals);
    if (!s) {
      return { title: "No repair was needed in this run.", text: "When a cheaper model nearly passes, <b>Nemotron Ultra</b> reads its failures and writes a short prompt addition, then the cheaper model is tested again. In this run every switched call passed as-is, so no prompts were changed.", visual: `<div class="t-empty">Nothing to repair: every migrated call passed with its original prompt.</div>` };
    }
    const base = s.decision.model;
    const before = s.evals[base]?.results || [];
    const after = s.evals[`${base}+hint`]?.results || [];
    const flip = before.find((r) => !r.passed && after.find((a) => a.case === r.case)?.passed) || before.find((r) => !r.passed) || before[0];
    const fixed = after.find((a) => a.case === flip?.case);
    const c = (s.cases || []).find((x) => x.id === flip?.case);
    const b = s.evals[base], h = s.evals[`${base}+hint`];
    return {
      title: `${MODEL_LABEL[base] || base} went from ${pct(b?.pass_rate)} to ${pct(h?.pass_rate)} with one sentence.`,
      text: `On <b>${esc(fnOf(s))}</b>, ${MODEL_LABEL[base] || base} failed ${before.filter((r) => !r.passed).length} of ${before.length} cases. <b>Nemotron Ultra</b> read the failures and wrote the prompt addition shown here; the cheaper model was then re-tested on the same cases. The new prompt ships in the patch, so the app gets the cheap model <i>and</i> the passing behaviour.`,
      visual: `<div class="t-hint"><div class="t-cap">Prompt addition written by Ultra</div><p>${esc(s.decision.hint)}</p></div>
        ${c ? `<div class="t-cap" style="margin-top:14px">Case #${c.id} · ${esc(c.name)}</div>` : ""}
        <div class="t-ba"><div class="t-b"><div class="t-ba-h"><span class="bad">✗ Before</span> ${chip(base)}</div><pre>${esc(flip?.output || flip?.error || "")}</pre><div class="t-why">${esc(flip?.reason || "")}${(flip?.checks || []).filter((x) => !x.ok).map((x) => ` · ✗ ${esc(x.name)}`).join("")}</div></div>
        <div class="t-a"><div class="t-ba-h"><span class="${fixed?.passed ? "ok" : "bad"}">${fixed?.passed ? "✓" : "✗"} After</span> ${chip(`${base}+hint`)}</div><pre>${esc(fixed?.output || fixed?.error || "")}</pre><div class="t-why">${esc(fixed?.reason || "")}</div></div></div>`,
    };
  }

  function chPatch(st, s) {
    const p = st.patch;
    if (!p) return { title: "No patch was produced.", text: "Nothing cleared the bar, so there is nothing to change.", visual: "" };
    const file = s?.site.file;
    const chunks = (p.diff || "").split(/(?=^--- a\/)/m);
    const mine = chunks.filter((c) => file && c.startsWith(`--- a/${file}`)).join("") || chunks[0] || "";
    const routes = (p.routes || []).filter((r) => !file || r.file === file);
    const okAll = (p.compile || []).every((c) => c.ok);
    return {
      title: (p.changed || []).length === 1 ? `1 file changed. ${okAll ? "It compiles." : "It failed to compile."}` : `${(p.changed || []).length} files changed. ${okAll ? "All of them compile." : "Some failed to compile."}`,
      text: `Edits are made at exact positions in the parsed code, then every touched file is re-parsed and byte-compiled. ${p.mode === "dedicated-client" ? "Some calls stay on their original provider, so their client is left alone and migrated calls get a dedicated Token Factory client." : "Every call migrates, so the existing OpenAI clients simply point at Token Factory."} Each model sits behind an env var, so rolling back one call is a one-line change.`,
      visual: `<div class="t-checks">${(p.compile || []).map((c) => `<span class="${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✗"} ${esc(c.file)}</span>`).join("")}</div>
        <div class="diff t-diff">${diffHtml(mine)}</div>
        ${routes.length ? `<div class="t-env">${routes.map((r) => `<code>${esc(r.env)}=${esc(r.to)}</code>`).join("")}</div>` : ""}`,
    };
  }

  function chBill(st) {
    const sum = st.summary || {};
    const u = st.usage || { by_role: {}, cost: 0, calls: 0 };
    const done = (st.sites || []).filter((s) => s.decision);
    const roleName = { analyze: "Understand call sites", testgen: "Write tests + references", judge: "Grade outputs", repair: "Adapt prompts" };
    const roles = Object.entries(u.by_role || {}).sort((a, b) => b[1].cost - a[1].cost);
    const maxRole = Math.max(1e-9, ...roles.map(([, r]) => r.cost));
    return {
      title: `${sum.saving_pct ?? 0}% cheaper per request, for ${usd(u.cost, 3)} of testing.`,
      text: `${sum.migrated ?? 0} of ${sum.sites ?? done.length} calls moved to Nemotron${sum.kept ? `; ${sum.kept} stayed on their original model because nothing cleared the bar` : ""}. Switchyard itself runs on Nemotron: <b>Super</b> for analysis and grading, <b>Ultra</b> with reasoning only for tests and prompt repair, and the full bill for this run is broken down here.`,
      visual: `<div class="t-kpis">${kpi(`${sum.saving_pct ?? 0}%`, "cheaper per request", "hl")}${kpi(`${sum.migrated ?? 0}/${sum.sites ?? done.length}`, "calls switched")}${kpi(usd(u.cost, 3), "cost of this run")}</div>
        <table class="simple t-table"><thead><tr><th>Call</th><th>Was</th><th>Now</th><th>Saving</th></tr></thead><tbody>${done.map((s) => `<tr><td><b>${esc(fnOf(s))}</b></td><td><code>${esc(modelOf(s))}</code></td><td>${s.decision.action === "migrate" ? chip(s.decision.choice) : chip(null)}</td><td><b>${s.savings?.saving_pct ? `${s.savings.saving_pct}%` : "–"}</b></td></tr>`).join("")}</tbody></table>
        <div class="t-roles"><div class="t-cap">Where this run's tokens went</div>${roles.map(([role, r]) => `<div class="t-metric"><span>${esc(role.startsWith("candidate:") ? `Candidates: ${MODEL_LABEL[role.split(":")[1]] || role}` : roleName[role] || role)}</span><div class="t-bar role-${esc(r.model)}"><i style="--w:${Math.max(2, Math.round((r.cost / maxRole) * 100))}%"></i></div><b>${usd(r.cost, 4)}</b></div>`).join("")}</div>`,
    };
  }

  /* build ---------------------------------------------------------------- */
  async function build() {
    const body = $t("#tourBody");
    body.innerHTML = `<div class="t-loading"><span class="spin"></span> Loading the recorded run…</div>`;
    const { state, banner } = await pickRun();
    $t("#tourBanner").hidden = !banner;
    $t("#tourBanner").textContent = banner;
    if (!state) {
      body.innerHTML = `<section class="t-chap t-none"><div class="t-text"><h2>No recorded run yet.</h2><p>Once a migration run is recorded, this tour walks through it chapter by chapter. Start one from the app.</p><a class="btn-glow" href="#app" data-scroll="#app" data-focus="#source">Start a migration</a></div></section>`;
      built = null;
      return;
    }
    built = state.id;
    const s = pickSite(state);
    const ch = [chProblem(state)];
    if (s) ch.push(chCloseup(state, s), chTests(state, s), chBakeoff(state, s));
    ch.push(chRepair(state), chPatch(state, s), chBill(state));
    const ids = s ? CHAPTERS : CHAPTERS.filter(([id]) => !["closeup", "tests", "bakeoff"].includes(id));
    $t("#tourRail").innerHTML = ids.map(([id, label], i) => `<li><a href="#tour" data-chap="${id}"><span>${String(i + 1).padStart(2, "0")}</span>${label}</a></li>`).join("");
    body.innerHTML = `<header class="t-intro"><p class="hero-kicker"><span class="pulse-dot"></span> Judge tour · about 3 minutes</p>
        <h1>One real migration, start to finish.</h1>
        <p>This walks through the recorded run on <b>${esc(shortSource(state.source))}</b>: what Switchyard found, how it tested each call on NVIDIA Nemotron via Nebius Token Factory, what it changed, and what it saves. Scroll, or use the chapter list.</p>
        <div class="t-scroll-hint" aria-hidden="true"><span></span></div></header>` +
      ch.map((c, i) => `<section class="t-chap" id="chap-${ids[i][0]}" data-chap="${ids[i][0]}">
        <div class="t-text"><div class="t-num">${String(i + 1).padStart(2, "0")} · ${esc(ids[i][1])}</div><h2>${esc(c.title)}</h2><p>${c.text}</p></div>
        <div class="t-visual"><div class="t-card">${c.visual}</div></div></section>`).join("") +
      `<section class="t-end"><h2>Try it on your repo.</h2><p>Point Switchyard at any Python codebase that uses the OpenAI SDK. You get tests for every call, a decision per call, and a patch you can review and apply.</p>
        <div class="hero-cta"><a class="btn-glow" href="#app" data-scroll="#app" data-focus="#source">Try it on your repo</a>
        <a class="btn-ghost" href="#${esc(state.id)}" data-open-run="${esc(state.id)}">Open the full run</a>
        <a class="btn-ghost" href="https://github.com/ayushagarwalhere/switchyard" target="_blank" rel="noopener">GitHub repo</a></div>
        <pre class="t-cmd">pip install -e . &amp;&amp; switchyard run https://github.com/owner/repo</pre></section>`;
    observe();
  }

  function observe() {
    io?.disconnect();
    const rail = $$t("#tourRail a");
    io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          if (e.target.dataset.chap && e.intersectionRatio > 0.25) {
            rail.forEach((a) => a.classList.toggle("on", a.dataset.chap === e.target.dataset.chap));
            const idx = rail.findIndex((a) => a.dataset.chap === e.target.dataset.chap);
            $t("#tourProgress i").style.width = `${((idx + 1) / rail.length) * 100}%`;
          }
        }
      });
    }, { threshold: [0.12, 0.3] });
    $$t("#tourBody .t-chap, #tourBody .t-intro, #tourBody .t-end").forEach((el) => io.observe(el));
    $$t("#tourBody [data-count]").forEach(countUp);
  }

  function countUp(el) {
    const raw = el.dataset.count;
    const m = raw.match(/^(\$?)(\d+(?:\.\d+)?)(%?)$/);
    if (!m || RM) return;
    const end = parseFloat(m[2]); const dec = (m[2].split(".")[1] || "").length;
    const host = el.closest(".t-chap") || el;
    const run = () => {
      const t0 = performance.now();
      const step = (now) => {
        const k = Math.min(1, (now - t0) / 1100); const e = 1 - Math.pow(1 - k, 3);
        el.textContent = `${m[1]}${(end * e).toFixed(dec)}${m[3]}`;
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };
    const o = new IntersectionObserver((es) => { if (es[0].isIntersecting) { run(); o.disconnect(); } }, { threshold: 0.3 });
    o.observe(host);
  }

  function scrollTop() {
    if (window.__lenis) window.__lenis.scrollTo(0, { immediate: true });
    else window.scrollTo(0, 0);
  }

  const Tour = {
    async open() {
      document.body.classList.add("touring");
      $t("#tourView").hidden = false;
      scrollTop();
      if (!built) await build();
    },
    close(keepScroll) {
      if (!document.body.classList.contains("touring")) return;
      document.body.classList.remove("touring");
      $t("#tourView").hidden = true;
      if (location.hash === "#tour") history.replaceState(null, "", location.pathname + (typeof current !== "undefined" && current ? `#${current}` : ""));
      if (!keepScroll) scrollTop();
    },
  };
  window.Tour = Tour;

  document.addEventListener("click", (e) => {
    const chap = e.target.closest("[data-chap]");
    if (chap && chap.tagName === "A") {
      e.preventDefault();
      const el = document.getElementById(`chap-${chap.dataset.chap}`);
      if (el) (window.__lenis ? window.__lenis.scrollTo(el, { offset: -40 }) : el.scrollIntoView({ behavior: RM ? "auto" : "smooth" }));
      return;
    }
    const open = e.target.closest("[data-open-run]");
    if (open) {
      e.preventDefault();
      Tour.close(true);
      openRun(open.dataset.openRun);
      requestAnimationFrame(() => window.SY_scrollTo ? window.SY_scrollTo("#main") : null);
      return;
    }
    if (e.target.closest("[data-tour-close]")) { e.preventDefault(); Tour.close(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("touring")) Tour.close(); });
})();
