/* WisprOps console. Two backends with one interface:
   - live: the local WisprOps server watching your real services (when /api/state answers)
   - demo: the same agent running in this tab against a simulated production. Entered from the
     Demo section; it never touches live state. On static hosting (no server) it is all there is. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clock = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const hm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dur = (ms) => (ms == null ? "–" : ms < 90000 ? Math.round(ms / 1000) + "s" : ms < 5400000 ? Math.round(ms / 60000) + " min" : (ms / 3600000).toFixed(1) + " h");
  const W = window.WisprOps;
  const REPO = "https://github.com/ayushagarwalhere/wisprops"; // where people clone it from
  const OPEN = W.OPEN, CLOSED = W.CLOSED;
  const NUMW = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } } };

  let live = null, demo = null, backend = null, staticSite = false;
  let selected = null, speaking = null, playing = false, current = null;

  // ---------- backends ----------
  async function sha256(s) {
    if (window.crypto && crypto.subtle) { const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)); return Array.from(new Uint8Array(b)).map((x) => x.toString(16).padStart(2, "0")).join(""); }
    return null;
  }
  function demoBackend() {
    const sim = WisprOpsSim.createSim();
    const agent = W.createAgent({ config: sim.config, adapters: Object.assign({}, sim.adapters, { notify: async (n) => { if (n.kind === "page" || n.kind === "escalate") onPage(n); }, digest: sha256 }) });
    agent.addRunbook("checkout-api", "If checkout-api breaks at night, look at db_pool first: since the October Postgres migration it needs a big pool. Put it back first, ask questions later.", "dictated with Wispr Flow");
    let timer = null;
    const b = {
      mode: "demo", sim, agent,
      view: () => ({ state: agent.state, stats: agent.stats(), handoff: agent.handoff(0), settings: agent.settings, channels: {}, remote: { hosts: [] },
        services: sim.config.services.map((s) => ({ id: s.id, name: s.name, health: s.health, configLabel: s.configLabel, preset: "simulated", can: { config: true, rollback: true, restart: true }, snoozedUntil: agent.snoozedUntil(s.id) || null })) }),
      command: (id, text, meta) => agent.command(text, meta, id),
      postmortem: async (id) => agent.postmortem(agent.state.incidents.find((i) => i.id === id)),
      ack: (id) => agent.acknowledge(id, "console button"),
      runbook: async (svc, text, meta) => !!agent.addRunbook(svc, text, meta && meta.via === "wispr" ? "dictated with Wispr Flow" : meta && meta.via === "sim" ? "simulated Wispr" : "typed"),
      removeRunbook: async (svc, id) => agent.removeRunbook(svc, id),
      snooze: (svc, min) => agent.snooze(svc, min, "console button"),
      pauseOrders: (p) => agent.setOrdersPaused(p, "console button"),
      revoke: (oid) => agent.revokeOrder(oid, "console button"),
      doctor: async () => null,
      freeze: () => agent.freeze("console button"),
      certificate: async (id) => agent.certificate(id),
      addService: null, // the sandbox's two services are fixed; the live server can add yours
      start: () => { if (!timer) timer = setInterval(async () => { await agent.tick(); refresh(); }, 1200); },
      stop: () => { clearInterval(timer); timer = null; },
    };
    return b;
  }
  function liveBackend(first) {
    let cache = first, es = null;
    const post = async (p, body) => {
      const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.status === 401) { needSignIn(); return { reply: "Sign in first: open the link printed by “wisprops start” (or run “wisprops open”). Nothing was changed.", ok: false }; }
      return r.json();
    };
    const pull = async () => { cache = await (await fetch("api/state", { cache: "no-store" })).json(); refresh(); };
    return {
      mode: "live",
      view: () => cache,
      command: async (id, text, meta) => { const r = await post("api/command", { id, text, meta }); await pull(); return r; },
      postmortem: async (id) => (await fetch("api/postmortem?id=" + encodeURIComponent(id))).text(),
      ack: async (id) => { await post("api/ack", { id }); await pull(); },
      runbook: async (serviceId, text, meta) => { const r = await post("api/runbook", { serviceId, text, meta }); await pull(); return r.ok; },
      removeRunbook: async (serviceId, id) => { await post("api/runbook", { serviceId, remove: id }); await pull(); },
      snooze: async (serviceId, minutes) => { await post("api/snooze", { serviceId, minutes }); await pull(); },
      pauseOrders: async (paused) => { await post("api/orders/pause", { paused }); await pull(); },
      revoke: async (id) => { await post("api/revoke", { id }); await pull(); },
      doctor: async () => post("api/doctor", {}),
      freeze: async () => { await post("api/freeze", {}); await pull(); },
      certificate: async (id) => { const r = await fetch("api/certificate?id=" + encodeURIComponent(id)); return r.ok ? r.text() : ""; },
      addService: async (text) => { const r = await post("api/services", { text }); await pull(); return r; },
      testPage: async () => post("api/test-page", {}),
      saveSettings: async (body) => { const r = await post("api/settings", body); await pull(); return r; },
      start: () => {
        if (es) return;
        es = new EventSource("api/events");
        es.addEventListener("state", pull);
        es.addEventListener("page", (e) => { const n = JSON.parse(e.data); if (n.kind === "page" || n.kind === "escalate") onPage(n, true); else if (n.test) { beep(); toast("Test page sent. This tab rings like this when a real one arrives."); } });
        es.onerror = () => setMode();
        es.onopen = () => setMode();
      },
      stop: () => {},
    };
  }
  const inDemo = () => backend && backend.mode === "demo";
  function enterDemo() {
    if (inDemo()) return;
    demo = demoBackend(); backend = demo; selected = null; $("#banner").hidden = true;
    demo.start(); setMode(); refresh();
  }
  let scenarioRun = 0, scenarioDemo = null;
  const cancelScenario = () => { scenarioRun++; scenarioDemo = null; playing = false; };
  function exitDemo() {
    if (!live || !inDemo()) return;
    speechSynthesis && speechSynthesis.cancel();
    cancelScenario();
    demo.stop(); demo = null; backend = live; selected = null; $("#banner").hidden = true;
    setMode(); location.hash = "#/board";
  }
  function needSignIn() {
    const b = $("#banner");
    b.innerHTML = `<span>🔒 This browser is not signed in, so it can read but not change anything. Open the sign-in link printed by <code>wisprops start</code> (or run <code>wisprops open</code>) once.</span>`;
    b.hidden = false; b.classList.add("info");
  }
  function setMode() {
    const m = $("#mode"), bar = $("#demobar");
    if (inDemo()) {
      m.textContent = "demo sandbox"; m.className = "chip accent";
      bar.hidden = false;
      bar.innerHTML = staticSite
        ? `<span><b>Demo sandbox.</b> A simulated production running in this tab; nothing real is touched. Every scripted line is labelled “simulated Wispr”.</span><span class="row"><a class="btn tiny" href="#/demo">Scenarios</a><a class="btn tiny" href="#/docs">Run it on your services</a></span>`
        : `<span><b>Demo sandbox.</b> Simulated services in this tab. Your live services are still being watched in the background.</span><span class="row"><a class="btn tiny" href="#/demo">Scenarios</a><button class="tiny" id="exit-demo">Exit demo → my services</button></span>`;
      const x = $("#exit-demo"); if (x) x.onclick = exitDemo;
    } else {
      m.textContent = "live · watching your services"; m.className = "chip ok";
      bar.hidden = true;
    }
  }

  async function boot() {
    let first = null;
    if (!/[?&]sim=1/.test(location.search)) {
      try { const r = await fetch("api/state", { cache: "no-store" }); if (r.ok) { const j = await r.json(); if (j && j.wisprops) first = j; } } catch (e) { /* static hosting */ }
    }
    if (first) { live = liveBackend(first); backend = live; live.start(); if (first.session === false) needSignIn(); }
    else { staticSite = true; demo = demoBackend(); backend = demo; demo.start(); }
    setMode();
    if (staticSite && !location.hash) location.hash = "#/demo";
    route(true);
    if (staticSite && /[?&]play=1/.test(location.search)) setTimeout(() => runScenario("night"), 600);
  }

  // ---------- paging ----------
  let audio;
  function beep() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.28].forEach((d, k) => { const o = audio.createOscillator(), g = audio.createGain(); o.frequency.value = k ? 660 : 880; g.gain.value = 0.06; o.connect(g); g.connect(audio.destination); o.start(audio.currentTime + d); o.stop(audio.currentTime + d + 0.2); });
    } catch (e) { /* no audio */ }
  }
  let bannerFor = null;
  function onPage(n, fromLive) {
    const b = $("#banner");
    b.classList.remove("info");
    if (fromLive && inDemo()) {
      b.innerHTML = `<span>📟 Real page: ${esc(n.title)}: ${esc(n.body)}</span><span class="row"><button class="tiny" id="ack">Exit demo and answer</button></span>`;
      b.hidden = false;
      $("#ack").onclick = () => { b.hidden = true; cancelScenario(); exitDemo(); selected = n.incidentId || null; refresh(); focusTalk(); };
      beep();
      return;
    }
    const busy = $("#say").value.trim() || speaking;
    if (n.incidentId) { if (!busy || !selected) selected = n.incidentId; bannerFor = n.incidentId; }
    b.innerHTML = `<span>📟 ${esc(n.title)}: ${esc(n.body)}</span><span class="row">${n.phrase ? `<span class="chip">say “${esc(n.phrase)}”</span>` : ""}<button class="tiny" id="ack">Open the incident</button></span>`;
    b.hidden = false;
    $("#ack").onclick = () => { b.hidden = true; if (n.incidentId) selected = n.incidentId; location.hash = "#/board"; refresh(); focusTalk(); };
    beep();
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) { try { new Notification("WisprOps · " + n.title, { body: n.body }); } catch (e) { /* ignore */ } }
    if (noHands && !busy) {
      // No hands: read the page out loud and put the cursor where Wispr Flow will type.
      if (n.incidentId) selected = n.incidentId;
      focusTalk();
      announce(`${n.title}. ${n.body}${n.phrase ? ` To apply it, hold your Wispr key and say: ${n.phrase}.` : ""}`);
    }
    refresh();
  }

  // ---------- No hands mode ----------
  // Wispr dictations send themselves after a short pause, pages and replies are spoken, and the Talk
  // box keeps focus so the next dictation always lands in it. Typed text never auto-sends.
  let noHands = store.get("wisprops.nohands") === "1", autoT = null, announcing = false;
  function announce(text) {
    if (!("speechSynthesis" in window) || speaking) return;
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(String(text).replace(/[“”]/g, "").slice(0, 600));
    announcing = true;
    u.onend = u.onerror = () => { announcing = false; };
    speechSynthesis.speak(u);
  }
  function setNoHands(on) {
    noHands = !!on; store.set("wisprops.nohands", noHands ? "1" : "0");
    const b = $("#nohands"); b.setAttribute("aria-pressed", String(noHands)); b.classList.toggle("primary", noHands);
    b.textContent = noHands ? "🙌 No hands: on" : "🙌 No hands";
    if (noHands) focusTalk();
  }

  // ---------- router ----------
  const pages = {};
  function routeName() { return (location.hash.replace(/^#\/?/, "") || "board").split("?")[0]; }
  function route(force) {
    const name = pages[routeName()] ? routeName() : "board";
    const q = new URLSearchParams(location.hash.split("?")[1] || "");
    if (q.get("inc")) selected = q.get("inc");
    if (name === "demo" && !inDemo()) enterDemo();
    $$("#nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#/" + name));
    if (bannerFor) { const bi = backend.view().state.incidents.find((x) => x.id === bannerFor); if (bi && (!OPEN.includes(bi.status) || bi.ackAt)) { $("#banner").hidden = true; bannerFor = null; } }
    const key = name + "|" + backend.mode;
    if (force || current !== key) { const moved = current && current.split("|")[0] !== name; current = key; pages[name].mount(); if (moved) window.scrollTo(0, 0); } else if (pages[name].update) pages[name].update(); else pages[name].mount();
    renderTalk();
    renderFrozen();
    if (noHands && document.activeElement && !/TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName) && $("#modal").hidden) $("#say").focus({ preventScroll: true });
  }
  function renderFrozen() {
    const v = V(), f = v.state.frozen, el = $("#frozen");
    const hard = v.settings && v.settings.applyEnabled === false;
    el.hidden = !f && !hard;
    if (!el.hidden) el.innerHTML = hard ? `🧊 <b>Changes are switched off in the config</b> (<code>settings.applyEnabled</code>). WisprOps watches and pages, and applies nothing.`
      : `🧊 <b>Production is frozen</b> since ${esc(hm(f.at))} (“${esc(f.by)}”). Nothing is applied, by you or by a standing order. To lift it, hold your Wispr key and say <b>“unfreeze production”</b>.`;
  }
  window.addEventListener("hashchange", () => route(true));
  let rq = false;
  function refresh() { if (rq) return; rq = true; requestAnimationFrame(() => { rq = false; route(false); }); }
  const V = () => backend.view();

  // ---------- Wispr burst tracking (shared by every dictation box) ----------
  function trackWispr(ta, meterEl, opts) {
    const m = { insert: 0, keys: 0, duringReadback: false };
    let prev = ta.value.length;
    const paint = () => {
      if (!meterEl) return;
      if (!m.insert && !m.keys) { meterEl.textContent = (opts && opts.idle) || "Waiting for Wispr…"; meterEl.className = "muted small"; ta.classList.remove("live"); return; }
      const wispr = m.insert >= 6 && m.keys <= 1;
      meterEl.textContent = wispr ? `✓ Wispr burst · ${m.insert} chars at once` : `⌨ typed · ${m.keys} keystrokes${opts && opts.typedNote ? " " + opts.typedNote : ""}`;
      meterEl.className = "small " + (wispr ? "wispr-ok" : "muted");
      ta.classList.toggle("live", wispr);
    };
    ta.addEventListener("input", (e) => {
      const d = ta.value.length - prev; prev = ta.value.length;
      if (d > 0) { if (/^history/.test(e.inputType || "")) m.keys += d; else if (e.inputType === "insertFromPaste" || e.inputType === "insertFromDrop" || d > 2) m.insert = Math.max(m.insert, d); else m.keys += d; }
      if (d > 0) m.lastType = e.inputType || "";
      if (!ta.value) { m.insert = 0; m.keys = 0; }
      if (opts && opts.onInput) opts.onInput(d);
      paint();
    });
    paint();
    return {
      meta: () => ({ via: m.insert >= 6 && m.keys <= 1 ? "wispr" : "typed", insert: m.insert, keys: m.keys, duringReadback: m.duringReadback }),
      pasted: () => /^insertFrom(?:Paste|Drop)$|^insertReplacementText$/.test(m.lastType || ""),
      flagReadback: () => { m.duringReadback = true; },
      reset: () => { ta.value = ""; prev = 0; m.insert = 0; m.keys = 0; m.duringReadback = false; paint(); },
      set: (insert, keys) => { m.insert = insert; m.keys = keys; prev = ta.value.length; paint(); },
    };
  }

  // ---------- board ----------
  function statusChip(i) {
    const m = { triage: "warn", proposed: "warn", applying: "warn", verifying: "warn", resolved: "ok", "recovered on its own": "ok", escalated: "bad", "handed back": "bad", "stood down": "" };
    return `<span class="chip ${m[i.status] || ""}">${esc(i.status)}</span>`;
  }
  function svcCard(s, S) {
    const r = S.svc[s.id] || {}; const last = r.last || {};
    const cls = r.status === "ok" ? "ok" : !r.status || r.status === "unknown" ? "unknown" : "bad";
    return `<div class="card svc"><div class="spread"><span class="row"><span class="dot ${cls}"></span><span class="name">${esc(s.name)}</span></span><span class="row">${s.snoozedUntil ? `<span class="chip warn" title="No incidents or pages until then">💤 until ${esc(hm(s.snoozedUntil))}</span>` : ""}<span class="chip ${cls === "ok" ? "ok" : cls === "bad" ? "bad" : ""}">${esc(r.status || "starting")}</span></span></div>
      <div class="meta">${esc(s.health || "")} · ${last.at ? (last.ok ? `${last.status} in ${last.ms != null ? last.ms : "?"}ms` : esc(last.error || "HTTP " + last.status)) : "first check pending"}</div>
      <div class="meta">last healthy snapshot: ${r.golden ? `release ${esc(r.golden.version || "–")} · ${esc(clock(r.golden.at))}` : "not yet"}</div></div>`;
  }
  function incCard(i) {
    const fix = i.proposals.find((p) => p.state === "applied") || i.proposals[i.proposals.length - 1];
    const needsAck = i.woken && !i.ackAt && OPEN.includes(i.status);
    return `<div class="card click inc ${i.id === selected ? "sel" : ""}" data-inc="${i.id}" tabindex="0" role="button" aria-pressed="${i.id === selected}">
      <div class="spread"><span class="inc-title">#${i.n} ${esc(i.service)} · ${esc(i.cause ? i.cause.kind.replace(/_/g, " ") : "triage")}</span>
      <span class="row">${statusChip(i)}${i.orderId ? `<span class="chip accent">😴 ${esc(i.orderId)} · nobody woken</span>` : i.woken ? `<span class="chip">${i.escalatedAt ? "📟 escalated to backup" : "📟 paged"}</span>` : ""}${i.ackAt && i.woken ? `<span class="chip ok">ack ${esc(dur(i.mtta))}</span>` : ""}</span></div>
      <div class="cause">${esc(i.cause ? i.cause.summary : "Looking…")}</div>
      <div class="spread small muted" style="margin-top:8px"><span>${esc(i.source || "health check")} · opened ${esc(clock(i.opened))}${i.mttr ? ` · recovered in ${dur(i.mttr)}` : ""}${fix ? ` · fix ${fix.n}: ${esc(fix.state)}` : ""}</span>
      <span class="row">${needsAck ? `<button class="tiny" data-ack="${i.id}">I'm on it</button>` : ""}<button class="tiny" data-pm="${i.id}">Postmortem</button></span></div></div>`;
  }
  pages.board = {
    mount() {
      const S = V().state;
      const open = S.incidents.filter((i) => OPEN.includes(i.status));
      if (!selected && open.length) selected = open[open.length - 1].id;
      const demoMode = inDemo();
      $("#view").innerHTML = `
        <section class="hero">
          <div class="eyebrow">${demoMode ? "Demo sandbox · simulated production" : "On-call, approved by voice with Wispr Flow"}</div>
          <h1 id="hero-h"></h1>
          <p class="lede" id="hero-p"></p>
          ${demoMode ? `<div class="row" style="margin-top:10px"><button class="primary" data-play="night" ${playing ? "disabled" : ""}>▶ Play the demo (2 min)</button><a class="btn" href="#/demo">All scenarios</a><span class="small muted">Or break something below and answer the page yourself.</span></div>` : ""}
        </section>
        <div class="tally" id="tally"></div>
        <div class="board">
          <div class="stack" id="svc-col"></div>
          <div class="stack"><div class="eyebrow">Incidents</div><div class="stack" id="inc-list"></div></div>
        </div>`;
      $$("[data-play]").forEach((b) => (b.onclick = () => runScenario(b.dataset.play)));
      this.update();
    },
    update() {
      const v = V(), S = v.state, st = v.stats;
      const open = S.incidents.filter((i) => OPEN.includes(i.status));
      $("#hero-h").textContent = open.length ? `${open.length} incident${open.length === 1 ? "" : "s"} need${open.length === 1 ? "s" : ""} you.` : "All quiet.";
      $("#hero-p").textContent = open.length ? "Pick an incident, then hold your Wispr Flow key in the Talk box. Say “what happened?”, “I'm on it”, or say the fix back as shown in the Talk panel." : "WisprOps is watching. When something breaks it finds the cause, proposes one allowlisted fix, and waits for you to say it back with Wispr Flow: “approve fix one, db_pool back to 20”.";
      $("#tally").innerHTML = `
        <div class="card stat"><div class="n">${open.length}</div><div class="l">open incident${open.length === 1 ? "" : "s"}</div></div>
        <div class="card stat"><div class="n">${dur(st.mttrMs)}</div><div class="l">mean time to verified recovery</div></div>
        <div class="card stat"><div class="n">${st.woken}</div><div class="l">times you were paged</div></div>
        <div class="card stat"><div class="n">${st.underOrder}</div><div class="l">fixed under a standing order, nobody woken</div></div>`;
      if (!v.services.length) {
        $("#hero-h").textContent = "Nothing to watch yet.";
        $("#hero-p").textContent = "Add your first service. Hold your Wispr Flow key and describe it: what it's called, what runs it, and where its health check is. HTTP, a TCP port (Postgres, Redis) or a command all work.";
      }
      $("#svc-col").innerHTML = `<div class="eyebrow">Services</div>${v.services.length ? "" : `<div class="card"><h3>Add your first service</h3><p class="small muted">“Watch my api in docker container api, health check at localhost 3000 slash healthz.” Or a database: “watch postgres on localhost 5432.”</p><a class="btn primary" href="#/services">Add a service by voice →</a></div>`}${v.services.map((s) => svcCard(s, S)).join("")}
        ${inDemo() ? `<div class="eyebrow" style="margin-top:16px">Break something</div>${Object.entries(backend.sim.faults).map(([k, f]) => `<button class="fault" data-fault="${k}" ${playing ? "disabled" : ""}>⚡ ${esc(f.label)}</button>`).join("")}` : ""}`;
      $("#inc-list").innerHTML = S.incidents.length ? S.incidents.slice().reverse().slice(0, 50).map(incCard).join("") : `<div class="card empty">Waiting for the next page. Nothing is broken right now.</div>`;
      $$("[data-inc]").forEach((el) => { const pick = () => { selected = el.dataset.inc; refresh(); }; el.onclick = pick; el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } }; });
      $$("[data-fault]").forEach((b) => (b.onclick = () => breakIt(b.dataset.fault)));
      $$("[data-pm]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); openPostmortem(b.dataset.pm); }));
      $$("[data-ack]").forEach((b) => (b.onclick = async (e) => { e.stopPropagation(); await backend.ack(b.dataset.ack); refresh(); }));
    },
  };
  function breakIt(k) {
    const f = backend.sim.faults[k];
    f.run();
    if (f.alert) backend.agent.alert(f.svc, f.alert).then(refresh);
    toast(f.label + ". Watch the board.");
  }

  // ---------- talk panel ----------
  let talkTrack;
  function renderTalk() {
    const v = V();
    const i = v.state.incidents.find((x) => x.id === selected);
    $("#talk-title").textContent = i ? `#${i.n} ${i.service} · ${i.status}` : "Everything · no incident selected";
    $("#all").hidden = !i;
    const tl = $("#timeline");
    const atBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 40;
    const items = i ? i.timeline : (v.state.activity || []).slice(-60);
    const via = (t) => (t.who !== "you" ? "" : `<span class="via ${/wispr|sim/.test(t.via) ? "wispr" : ""}">${t.via === "sim" ? "simulated Wispr" : t.via === "wispr" ? `Wispr${t.insert ? ` · ${t.insert} chars in one burst` : ""}` : t.via === "telegram" ? "Telegram" : t.via === "button" || t.via === "console button" ? "button" : `typed${t.keys ? ` · ${t.keys} keys` : ""}`}</span>`);
    const EV = { changes: "changed", logs: "logs", rule: "your rule", memory: "seen before", alert: "alert", runbook: "runbook", release: "release", config: "config", probe: "health" };
    const evid = (t) => (t.evidence && t.evidence.length ? `<ul class="evidence">${t.evidence.map((e) => `<li class="ev-${esc(e.kind)}"><span class="ev-k">${esc(EV[e.kind] || e.kind)}</span>${esc(e.text)}</li>`).join("")}</ul>` : "");
    tl.innerHTML = items.length ? items.map((t) => `<li class="${t.who}"><span class="t">${esc(clock(t.at))}</span><span class="who ${t.who}">${t.who === "you" ? "you" : t.who === "agent" ? "WisprOps" : "system"}</span>${esc(t.text)}${via(t)}${evid(t)}</li>`).join("")
      : `<li class="muted">${i ? "" : "Nothing selected. Commands here apply to everything: “status”, “snooze checkout api for thirty minutes”, “pause standing orders”, “what happened last night?”. When something breaks, the page lands here."}</li>`;
    if (atBottom) tl.scrollTop = tl.scrollHeight;
    const p = i && i.proposals.find((x) => x.state === "pending");
    const sayBack = p && p.sayBack && p.phrase === p.sayBack;
    $("#pending").innerHTML = p ? `<div class="phrase"><div class="small muted">Fix ${p.n} is waiting. ${sayBack ? "To apply it, hold your Wispr Flow key and say it back. Every fact you say is checked against the fix:" : "To apply it, hold your Wispr Flow key and say:"}</div><div class="say">${esc(p.phrase && p.phrase !== "approve fix " + p.n ? p.phrase : "approve fix " + (NUMW[p.n] || p.n))}</div><div class="small muted" style="margin-top:4px">${esc(p.describe)}</div><div class="small muted">Blast radius: ${esc(p.blast)}</div></div>`
      : i && i.timeline.some((t) => t.offerOrder) && i.status === "resolved" && !v.state.orders.some((o) => o.fromIncident === i.id)
        ? `<div class="phrase"><div class="small muted">Optional. Let WisprOps handle this exact failure next time without waking you:</div><div class="say">grant standing order for seven days</div></div>`
        : i && CLOSED.includes(i.status) && !(i.debriefs && i.debriefs.length)
          ? `<div class="phrase"><div class="small muted">In the morning, hold your Wispr key and talk it through for a minute. Start with “debrief:”. Causes, action items with owners, and “must stay at” rules go straight into the postmortem.</div><div class="say">debrief: …</div></div>` : "";
    $("#rb").disabled = !p;
    const chips = i ? [["what happened?", "explain"], ...(i.woken && !i.ackAt && OPEN.includes(i.status) ? [["I'm on it", "ack"]] : []), ...(p ? [["read it back", "rb"], ["I'll handle it", "standdown"]] : [])] : [["status", "status"], ["what changed?", "changes"], ["what happened last night?", "handoff"], ...(v.state.frozen ? [] : [["freeze production", "freeze"]])];
    $("#quick").innerHTML = chips.map(([label, k]) => `<button class="tiny ghost" data-quick="${k}" data-text="${esc(label)}">${esc(label)}</button>`).join("");
    $$("[data-quick]").forEach((b) => (b.onclick = () => (b.dataset.quick === "rb" ? readBack() : send(b.dataset.text, { via: "button", insert: 0, keys: 0 }))));
  }
  function readBack() {
    const i = V().state.incidents.find((x) => x.id === selected);
    const p = i && i.proposals.find((x) => x.state === "pending");
    if (!p || !("speechSynthesis" in window)) return toast(p ? "This browser cannot read aloud." : "Nothing to read back.");
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(backend.mode === "demo" ? backend.agent.readback(p) : `Fix ${p.n}. ${p.describe} Blast radius: ${p.blast} To apply it, say: ${p.phrase || "approve fix " + p.n}.`);
    speaking = p.n;
    $("#rb").classList.add("pulse");
    u.onend = u.onerror = () => { speaking = null; $("#rb").classList.remove("pulse"); };
    speechSynthesis.speak(u);
  }
  function wireTalk() {
    const ta = $("#say");
    talkTrack = trackWispr(ta, $("#meter"), { typedNote: "(approvals need Wispr)", onInput: (d) => {
      if (speaking && d > 0) talkTrack.flagReadback();
      if (announcing && d > 0) { speechSynthesis.cancel(); announcing = false; }
      clearTimeout(autoT);
      const m = talkTrack.meta();
      if (noHands && m.via === "wispr" && !talkTrack.pasted() && ta.value.trim()) {
        // Wispr Flow may still be adding words; send once it has been quiet for a moment.
        $("#meter").textContent = `✓ Wispr burst · ${m.insert} chars · sending…`;
        autoT = setTimeout(() => { if (talkTrack.meta().via === "wispr" && ta.value.trim()) send(); }, 1100);
      }
    } });
    ta.addEventListener("keydown", (e) => { clearTimeout(autoT); if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    $("#send").onclick = () => send();
    $("#rb").onclick = readBack;
    $("#all").onclick = () => { selected = null; refresh(); };
    $("#talkfab").onclick = focusTalk;
    $("#nohands").onclick = () => { setNoHands(!noHands); toast(noHands ? "No hands is on: hold your Wispr key and speak. Dictations send themselves, and pages are read aloud." : "No hands is off. Enter sends."); };
    setNoHands(noHands);
    // "/" anywhere jumps to the Talk box, so one key and your voice are enough.
    document.addEventListener("keydown", (e) => { if (e.key === "/" && !/TEXTAREA|INPUT/.test(document.activeElement.tagName)) { e.preventDefault(); focusTalk(); } });
    // "g" then a letter jumps between pages; "?" lists them.
    const GO = { b: "board", s: "services", c: "changes", a: "receipts", o: "orders", h: "handoff", n: "analytics", w: "wispr", d: "demo", t: "settings", k: "docs" };
    let gAt = 0;
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || /TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName)) return;
      if (e.key === "?") { toast("Keys: / talk · g b board · g s services · g c changes · g a audit · g o orders · g h handoff · g n analytics · g w wispr · g t settings · g d demo"); return; }
      if (e.key === "g") { gAt = Date.now(); return; }
      if (gAt && Date.now() - gAt < 1200 && GO[e.key]) { gAt = 0; location.hash = "#/" + GO[e.key]; }
    });
  }
  function focusTalk() { const ta = $("#say"); ta.scrollIntoView({ behavior: "smooth", block: "center" }); ta.focus({ preventScroll: true }); }
  async function send(text, m, target) {
    const b = target || backend;
    if (m && m.via === "sim" && b.mode !== "demo") return;
    const ta = $("#say");
    text = text != null ? text : ta.value.trim();
    if (!text) return;
    const payload = m || Object.assign(talkTrack.meta(), { duringReadback: talkTrack.meta().duringReadback || !!speaking });
    if (speaking) { speechSynthesis.cancel(); speaking = null; }
    if (!m) talkTrack.reset();
    clearTimeout(autoT);
    const r = await b.command(selected, text, payload);
    if (r && r.select) selected = r.select;
    if (r && r.go && pages[r.go]) location.hash = "#/" + r.go;
    if (r && r.postmortem) openPostmortem(r.postmortem);
    if (r && r.reply) toast(r.reply.length > 160 ? r.reply.slice(0, 157) + "…" : r.reply);
    if (r && r.say && /Fix \d/.test(r.say)) readBack();
    else if (r && r.say && "speechSynthesis" in window) { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(r.say)); }
    else if (noHands && r && r.reply && !m) announce(r.reply);
    const v = V();
    if (!selected) { const open = v.state.incidents.filter((i) => OPEN.includes(i.status)); if (open.length === 1) selected = open[0].id; }
    refresh();
    if (noHands && !m) $("#say").focus({ preventScroll: true });
  }

  // ---------- services ----------
  const runbookTracks = {};
  pages.services = {
    mount() {
      const v = V();
      $("#view").innerHTML = `<div class="eyebrow">Services</div><h2>What WisprOps watches, and what it may do</h2>
        <p class="lede">Each service gets a health check, a last-healthy snapshot of its config and release, and at most three allowlisted fixes. Dictate a runbook note with Wispr Flow and it is shown as evidence when that service breaks. Say a number in it (“db_pool must stay at 20 or more”) and it becomes a rule WisprOps enforces.</p>
        <div class="stack" style="margin-top:14px">${v.services.map((s) => `
          <div class="card" id="svc-${esc(s.id)}">
            <div data-svc-head="${esc(s.id)}"></div>
            <div class="grid g2" style="margin-top:12px">
              <div>
                <div class="eyebrow">Runbook notes</div>
                <ul class="notes" data-notes="${esc(s.id)}"></ul>
                <label class="sr" for="rb-${esc(s.id)}">Runbook note for ${esc(s.name)}</label>
                <textarea id="rb-${esc(s.id)}" class="dict" rows="2" data-rb="${esc(s.id)}" placeholder="Hold your Wispr Flow key: “If ${esc(s.name)} fails at night, check …”"></textarea>
                <div class="spread"><span class="muted small" data-rbm="${esc(s.id)}"></span><button class="tiny primary" data-rbsave="${esc(s.id)}">Save note</button></div>
              </div>
              <div>
                <div class="eyebrow">Maintenance window</div>
                <p class="small muted" style="margin:0 0 6px">Deploying? Snooze it: WisprOps keeps watching but won't open incidents or page. Or say “snooze ${esc(s.name)} for thirty minutes”.</p>
                <div class="row" data-snz="${esc(s.id)}"></div>
              </div>
            </div>
          </div>`).join("")}</div>
        ${inDemo() ? "" : `<div class="card" style="margin-top:14px"><div class="spread"><h3>Doctor</h3><button class="tiny" id="doc-run">Run checks</button></div><p class="small muted">Probes each health URL, reads each config file, resolves each release and finds each command, without running any of them. Same as <code>wisprops doctor</code>.</p><div id="doc-out"></div></div>`}
        <div class="card" style="margin-top:14px"><h3>Add a service by voice</h3>
          <p class="small muted">Hold your Wispr Flow key and describe it: what it's called, what runs it, where its health check is, which config file to watch. Say “slash” and “dot” if Flow doesn't write them for you.</p>
          <label class="sr" for="add-say">Describe a service</label>
          <textarea id="add-say" class="dict" rows="2" placeholder="“Watch my shop api. It runs in docker container shop-api, health check at localhost 3000 slash healthz, config in config slash api dot json, git repo here.”"></textarea>
          <div class="spread"><span class="muted small" id="add-m"></span>${backend.addService ? `<button class="tiny primary" id="add-go" disabled>Add it</button>` : `<span class="small muted">In the sandbox this only shows the command.</span>`}</div>
          <div id="add-out" class="small" style="margin-top:8px"></div></div>
        <div class="card" style="margin-top:14px"><h3>Or with the CLI</h3><pre>wisprops add --say "watch my api in docker container api, health check at localhost 3000 slash healthz"
wisprops add api --health http://localhost:3000/healthz --docker api --watch ./config/api.json --git .
wisprops add worker --health http://localhost:9100/healthz --pm2 worker --watch ./.env
wisprops add web --health https://web.internal/healthz --kubernetes web --namespace prod
wisprops doctor   # then restart wisprops start</pre></div>`;
      v.services.forEach((s) => {
        const ta = $(`[data-rb="${CSS.escape(s.id)}"]`);
        runbookTracks[s.id] = trackWispr(ta, $(`[data-rbm="${CSS.escape(s.id)}"]`), { idle: "Dictate with Wispr Flow, or type." });
      });
      $$("[data-rbsave]").forEach((b) => (b.onclick = async () => {
        const id = b.dataset.rbsave, ta = $(`[data-rb="${CSS.escape(id)}"]`);
        if (!ta.value.trim()) return toast("Say or type a note first.");
        const ok = await backend.runbook(id, ta.value.trim(), runbookTracks[id].meta());
        if (ok) { runbookTracks[id].reset(); const ns = (V().state.runbooks || {})[id] || [], n = ns[ns.length - 1]; toast(n && n.rule ? `Saved, and I'll enforce it as a rule: ${n.rule.text}.` : "Saved. It will be shown as evidence when this service breaks."); }
        this.update();
      }));
      const dr = $("#doc-run"); if (dr) dr.onclick = runDoctor;
      const as = $("#add-say"), addT = trackWispr(as, $("#add-m"), { idle: "Dictate with Wispr Flow, or type." });
      const preview = () => {
        const d = W.describeService(as.value);
        const go = $("#add-go"); if (go) go.disabled = !as.value.trim() || !d.ok;
        $("#add-out").innerHTML = !as.value.trim() ? "" : `<div>Heard: ${d.heard.length ? d.heard.map((h) => `<span class="chip">${esc(h)}</span>`).join(" ") : "nothing I can use yet"}</div>${d.warnings.map((w) => `<div class="muted">! ${esc(w)}</div>`).join("")}<pre style="margin-top:6px">${esc(d.cli)}</pre>`;
      };
      as.addEventListener("input", preview);
      const go = $("#add-go");
      if (go) go.onclick = async () => {
        const r = await backend.addService(as.value.trim());
        if (r && r.ok) { addT.reset(); preview(); toast(`Added ${r.id}. WisprOps is watching it now; it takes a healthy snapshot after a few checks.`); route(true); }
        else toast("Not added: " + ((r && (r.error || r.reply)) || "unknown error"));
      };
      this.update();
    },
    update() {
      const v = V(), S = v.state;
      v.services.forEach((s) => {
        const r = S.svc[s.id] || {};
        const can = [["config", "config drift → put the key back"], ["rollback", "bad deploy → roll back"], ["restart", "dead process → restart"]];
        const head = $(`[data-svc-head="${CSS.escape(s.id)}"]`);
        if (!head) return;
        head.innerHTML = `<div class="spread"><span class="row"><span class="dot ${r.status === "ok" ? "ok" : !r.status || r.status === "unknown" ? "unknown" : "bad"}"></span><b class="mono">${esc(s.name)}</b>${s.preset ? `<span class="chip">${esc(s.preset)}</span>` : ""}${s.snoozedUntil ? `<span class="chip warn">💤 snoozed until ${esc(hm(s.snoozedUntil))}</span>` : ""}</span><span class="chip ${r.status === "ok" ? "ok" : "bad"}">${esc(r.status || "starting")}</span></div>
          <dl class="kv"><dt>Health check</dt><dd>${esc(s.health)}</dd>
          <dt>Config watched</dt><dd>${s.configLabel ? esc(s.configLabel) + (r.golden && r.golden.config ? ` · ${Object.keys(r.golden.config).length} keys in the snapshot` : "") : "none"}</dd>
          <dt>Release</dt><dd>${r.golden && r.golden.version ? esc(r.golden.version) + (r.history && r.history.length ? ` · previous: ${esc(r.history.slice(-3).join(", "))}` : "") : "not tracked"}</dd>
          <dt>May fix</dt><dd class="row">${can.map(([k, t]) => `<span class="chip ${s.can && s.can[k] ? "ok" : ""}">${s.can && s.can[k] ? "✓" : "–"} ${esc(t)}</span>`).join("")}</dd></dl>`;
        const notes = (S.runbooks && S.runbooks[s.id]) || [];
        $(`[data-notes="${CSS.escape(s.id)}"]`).innerHTML = notes.length ? notes.map((n) => `<li><span>${esc(n.text)}</span>${n.rule ? ` <span class="chip accent" title="WisprOps checks this: the last healthy snapshot never takes a value that breaks it, and no fix may restore one">rule: ${esc(n.rule.text)}</span>` : ""}<span class="small muted"> · ${esc(n.via)} · ${esc(new Date(n.at).toLocaleDateString())}</span> <button class="tiny ghost" data-rbdel="${esc(n.id)}" data-svc="${esc(s.id)}" aria-label="Delete note">✕</button></li>`).join("") : `<li class="muted small">No notes yet.</li>`;
        $(`[data-snz="${CSS.escape(s.id)}"]`).innerHTML = s.snoozedUntil ? `<button class="tiny" data-snooze="0" data-svc="${esc(s.id)}">End snooze now</button>` : [30, 60, 240].map((m) => `<button class="tiny" data-snooze="${m}" data-svc="${esc(s.id)}">${m < 60 ? m + " min" : m / 60 + " h"}</button>`).join("");
      });
      $$("[data-rbdel]").forEach((b) => (b.onclick = async () => { await backend.removeRunbook(b.dataset.svc, b.dataset.rbdel); this.update(); }));
      $$("[data-snooze]").forEach((b) => (b.onclick = async () => { await backend.snooze(b.dataset.svc, Number(b.dataset.snooze)); refresh(); }));
    },
  };
  async function runDoctor() {
    const out = $("#doc-out"); out.innerHTML = `<p class="muted small">Checking…</p>`;
    try {
      const d = await backend.doctor();
      const icon = { ok: "✓", warn: "!", fail: "✖", info: "·" };
      out.innerHTML = `<table class="t"><tr><th>Service</th><th>Check</th><th>Result</th></tr>${d.checks.map((c) => `<tr><td class="mono">${esc(c.service === "*" ? "WisprOps" : c.service)}</td><td>${esc(c.check)}</td><td class="lvl-${c.level}">${icon[c.level]} ${esc(c.text)}</td></tr>`).join("")}</table>`;
    } catch (e) { out.innerHTML = `<p class="small">Could not run the checks: ${esc(e.message)}</p>`; }
  }

  // ---------- audit ----------
  let auditFilter = "all";
  pages.receipts = {
    mount() {
      $("#view").innerHTML = `<div class="eyebrow">Audit log · consent receipts</div><h2>The words behind every change</h2>
        <p class="lede">Every change to production lands here with the exact words that allowed it, how they arrived (one Wispr Flow burst, or keystrokes), the dry run, the checks that proved recovery and a SHA-256 digest of the record. The digest shows tampering; it is not a signature.</p>
        <div class="spread" style="margin-top:12px"><div class="row" id="af"></div><div class="row"><button class="tiny" id="ex-csv">Export CSV</button><button class="tiny" id="ex-json">Export JSON</button></div></div>
        <div class="stack" style="margin-top:12px" id="rc-list"></div>`;
      $("#ex-csv").onclick = () => download("wisprops-audit.csv", W.toCSV(W.auditRows(V().state)) || "no rows\n", "text/csv");
      $("#ex-json").onclick = () => download("wisprops-audit.json", JSON.stringify(W.auditRows(V().state), null, 2), "application/json");
      this.update();
    },
    update() {
      const S = V().state;
      setTimeout(() => $$("[data-cert]").forEach((b) => (b.onclick = () => openCertificate(b.dataset.cert))), 0);
      const F = { all: () => true, wispr: (r) => /Wispr/.test(r.channel), order: (r) => /standing/.test(r.channel), flagged: (r) => r.flagged, failed: (r) => /did not|failed/.test(r.outcome) };
      const L = { all: "All", wispr: "Approved with Wispr", order: "Standing order", flagged: "Typed (flagged)", failed: "Did not recover" };
      $("#af").innerHTML = Object.keys(F).map((k) => `<button class="tiny ${auditFilter === k ? "primary" : ""}" data-af="${k}">${L[k]} · ${S.receipts.filter(F[k]).length}</button>`).join("");
      $$("[data-af]").forEach((b) => (b.onclick = () => { auditFilter = b.dataset.af; this.update(); }));
      const list = S.receipts.filter(F[auditFilter]).slice().reverse();
      $("#rc-list").innerHTML = list.length ? list.map((r) => `<div class="card receipt">
        <div class="spread"><span class="heard">“${esc(r.heard)}”</span><span class="row"><span class="chip ${/Wispr|standing/.test(r.channel) ? "accent" : "warn"}">${esc(r.channel)}</span><span class="chip ${/^recovered/.test(r.outcome) ? "ok" : r.outcome === "verifying" ? "warn" : /^undone/.test(r.outcome) ? "" : "bad"}">${esc(r.outcome)}</span></span></div>
        <dl class="kv"><dt>When</dt><dd>${esc(new Date(r.at).toLocaleString())} · ${esc(r.service)} · ${esc(r.incidentId)}</dd>
        <dt>Change</dt><dd>${esc(r.describe)}</dd>
        ${r.meta ? `<dt>How it arrived</dt><dd>${r.meta.via === "sim" ? "simulated Wispr dictation (demo)" : `${r.meta.insert} characters in one insertion, ${r.meta.keys} keystrokes`}</dd>` : ""}
        ${r.dryRun ? `<dt>Dry run</dt><dd>${esc(r.dryRun)}</dd>` : ""}
        ${r.cause ? `<dt>Cause</dt><dd>${esc(r.cause)}</dd>` : ""}
        ${r.checks ? `<dt>Verified by</dt><dd>${r.checks.healthyInARow} healthy checks in a row · ${esc(r.checks.postCondition)}</dd>` : ""}
        ${r.digest ? `<dt>SHA-256</dt><dd class="mono">${esc(r.digest)}</dd>` : ""}</dl><div class="row" style="margin-top:8px"><button class="tiny" data-cert="${esc(r.id)}">Approval record</button></div></div>`).join("") : `<div class="card empty">${auditFilter === "all" ? "Every approval and every standing-order fix lands here with the exact words that granted it." : "Nothing matches this filter."}</div>`;
    },
  };
  function download(name, text, type) { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }

  // ---------- standing orders ----------
  pages.orders = {
    mount() {
      $("#view").innerHTML = `<div class="eyebrow">Standing orders</div><h2>Permission to fix it without waking you</h2>
        <p class="lede">After a fix you approved is verified, say “grant standing order for seven days” with Wispr Flow. It covers one service, one failure signature and one exact action, with a use count and an expiry. Anything else still pages you.</p>
        <div class="card" id="kill" style="margin-top:12px"></div>
        <div class="card" id="freeze" style="margin-top:12px"></div>
        <div class="stack" style="margin-top:12px" id="so-list"></div>`;
      this.update();
    },
    update() {
      const v = V(), S = v.state, list = S.orders.slice().reverse();
      const pol = v.settings || {};
      $("#kill").innerHTML = `<div class="spread"><div><h3>${S.ordersPaused ? "⏸ All standing orders are paused" : "Standing orders are active"}</h3><p class="small muted" style="margin:0">${S.ordersPaused ? "Every failure pages you. Say “resume standing orders” or press the button." : `Kill switch: say “pause standing orders”. Policy caps: at most ${pol.maxOrderDays || 30} days and ${pol.maxOrderUses || 10} uses per order.`}</p></div><button class="${S.ordersPaused ? "primary" : "danger"}" id="kill-btn">${S.ordersPaused ? "Resume" : "Pause all"}</button></div>`;
      $("#kill-btn").onclick = async () => { await backend.pauseOrders(!S.ordersPaused); refresh(); };
      $("#freeze").innerHTML = `<div class="spread"><div><h3>${S.frozen ? "🧊 Production is frozen" : "Freeze switch"}</h3><p class="small muted" style="margin:0">${S.frozen ? `Since ${esc(hm(S.frozen.at))}. Nothing is applied by anyone. To lift it, hold your Wispr key and say “unfreeze production”. There is no button for that on purpose.` : "Stops every change: your approvals and every standing order. Anyone can freeze (say “freeze production”, press this, or run <code>wisprops freeze</code>); only a Wispr dictation unfreezes."}</p></div>${S.frozen ? "" : `<button class="danger" id="freeze-btn">Freeze</button>`}</div>`;
      const fb = $("#freeze-btn"); if (fb) fb.onclick = async () => { await backend.freeze(); refresh(); };
      $("#so-list").innerHTML = list.length ? list.map((o) => { const active = !o.revoked && o.expires > Date.now() && o.usesLeft > 0;
        return `<div class="card"><div class="spread"><b class="mono">${esc(o.id)} · ${esc(o.service)}</b><span class="row"><span class="chip ${active ? (S.ordersPaused ? "warn" : "ok") : ""}">${active ? (S.ordersPaused ? "paused" : "active") : o.revoked ? "revoked" : "spent / expired"}</span>${active ? `<button class="tiny danger" data-rev="${esc(o.id)}">Revoke</button>` : ""}</span></div>
        <dl class="kv"><dt>Action</dt><dd>${esc(o.describe)}</dd><dt>Granted by</dt><dd>“${esc(o.said)}” via ${esc(o.channel)} · ${esc(new Date(o.granted).toLocaleString())}</dd>
        <dt>Limits</dt><dd>${o.usesLeft} of ${o.usesLeft + (o.used || 0)} uses left · expires ${esc(new Date(o.expires).toLocaleString())}${o.window ? ` · nights only (${String(o.window.from).padStart(2, "0")}:00–${String(o.window.to).padStart(2, "0")}:00)` : ""}</dd>
        ${o.renewals && o.renewals.length ? `<dt>Renewed</dt><dd>${o.renewals.map((x) => `“${esc(x.said)}” via ${esc(x.channel)} · ${esc(new Date(x.at).toLocaleString())}`).join("<br>")}</dd>` : ""}
        ${active && o.expires - Date.now() < 2 * 86400000 ? `<dt>Expiring</dt><dd>Say “renew standing order ${esc(o.id.replace("-", " "))} for seven days” with Wispr Flow to keep it.</dd>` : ""}<dt>Signature</dt><dd class="mono">${esc(o.signature)}</dd></dl></div>`; }).join("")
        : `<div class="card empty">No standing orders yet. After WisprOps verifies a fix you approved, it will offer one.</div>`;
      $$("[data-rev]").forEach((b) => (b.onclick = async () => { if (confirm("Revoke this standing order? The next failure will page you.")) { await backend.revoke(b.dataset.rev); refresh(); } }));
    },
  };

  // ---------- settings ----------
  pages.settings = {
    update() {}, // a form: don't re-render under someone typing
    mount() {
      const v = V(), ch = v.channels || {}, h = ch.hints || {}, st = v.settings || {};
      const live = !!backend.saveSettings;
      const field = (id, label, hintText, ph, help) => `<label class="field"><span>${esc(label)} ${hintText ? `<span class="chip ok">${esc(hintText)}</span>` : ""}</span>
        <span class="row"><input id="${id}" type="text" autocomplete="off" spellcheck="false" placeholder="${esc(hintText ? "leave empty to keep it" : ph)}" ${live ? "" : "disabled"} />${hintText && live ? `<button class="tiny ghost" data-clear="${id}">Remove</button>` : ""}</span>${help ? `<span class="small muted">${help}</span>` : ""}</label>`;
      const num = (id, label, val, min, max, help) => `<label class="field"><span>${esc(label)}</span><input id="${id}" type="number" min="${min}" max="${max}" value="${esc(val)}" ${live ? "" : "disabled"} /><span class="small muted">${help}</span></label>`;
      $("#view").innerHTML = `<div class="eyebrow">Settings</div><h2>Where pages reach you</h2>
        <p class="lede">Set up at least one channel, then send a test page. Changes apply at once and are saved to <code>wisprops.config.json</code>. Secrets are never shown again after you save them.</p>
        ${live ? "" : `<div class="card" style="margin-top:12px"><p class="small muted" style="margin:0">This is the sandbox. On your own WisprOps (<code>wisprops start</code>) these fields save for real.</p></div>`}
        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>Your phone</h3>
            ${field("s-ntfy", "ntfy topic", h.ntfy, "a long private topic name", `Free, no account: install the <a href="https://ntfy.sh" target="_blank" rel="noopener">ntfy app</a> and subscribe to this topic. Anyone who knows the name can read it, so make it long.`)}
            ${live ? `<button class="tiny" id="s-ntfy-gen">Pick a private topic for me</button>` : ""}
            ${field("s-ntfyServer", "ntfy server (optional)", h.ntfyServer, "https://ntfy.sh", "Only if you host your own.")}
            ${field("s-tg-token", "Telegram bot token", h.telegram ? "set" : "", "123456:ABC…", "Create a bot with @BotFather. Pages get buttons; replies dictated with Flow's keyboard can acknowledge, snooze or freeze, never approve.")}
            ${field("s-tg-chat", "Telegram chat id", h.telegram, "123456789", "Message your bot once, then open https://api.telegram.org/bot&lt;token&gt;/getUpdates and copy chat.id.")}
          </div>
          <div class="card"><h3>Your team</h3>
            ${field("s-webhook", "Slack or Discord webhook", h.webhook, "https://hooks.slack.com/services/…", "An incoming-webhook URL. Pages, all-clears and the morning handoff go there.")}
            ${field("s-pd", "PagerDuty routing key", h.pagerduty, "Events API v2 integration key", "Pages trigger an incident; a verified recovery resolves it.")}
            <h3 style="margin-top:14px">Backup person</h3>
            ${field("s-esc-ntfy", "Backup's ntfy topic", h.escalateNtfy, "their private topic", "Paged if nobody says “I'm on it” in time.")}
            ${field("s-esc-webhook", "Backup's webhook", h.escalateWebhook, "https://…", "")}
          </div>
        </div>
        <div class="card" style="margin-top:14px"><h3>Timing</h3><div class="grid g2">
          ${num("s-esc", "Escalate after (minutes)", st.escalateAfterMin != null ? st.escalateAfterMin : 15, 0, 1440, "0 never escalates.")}
          ${num("s-ho", "Morning handoff at (hour, 0–23)", st.handoffHour != null ? st.handoffHour : 7, 0, 23, "Uses this machine's clock.")}
          ${num("s-nf", "Night starts (hour)", st.nightFrom != null ? st.nightFrom : 22, 0, 23, "For standing orders granted “only at night”.")}
          ${num("s-nt", "Night ends (hour)", st.nightTo != null ? st.nightTo : 8, 0, 23, "Nights-only orders stop acting at this hour.")}</div></div>
        <div class="row" style="margin-top:14px">${live ? `<button class="primary" id="s-save">Save</button><button id="s-test">Send a test page</button>` : ""}<span class="small" id="s-out"></span></div>
        <div class="card" style="margin-top:14px"><h3>Only in the config file, on purpose</h3><p class="small muted">Anything that makes WisprOps less careful can't be changed from a browser: <code>settings.allowTyped</code> (accept typed approvals), <code>applyEnabled</code>, the standing-order caps <code>maxOrderDays</code> and <code>maxOrderUses</code>, the remote access token and hosts, and the alerts token. Edit <code>wisprops.config.json</code> and restart.</p></div>`;
      if (!live) return;
      $$("[data-clear]").forEach((b) => (b.onclick = () => { const i = $("#" + b.dataset.clear); i.value = ""; i.dataset.clear = "1"; i.placeholder = "will be removed when you save"; }));
      $("#s-ntfy-gen").onclick = () => { const a = new Uint8Array(9); crypto.getRandomValues(a); $("#s-ntfy").value = "wisprops-" + Array.from(a).map((x) => x.toString(16).padStart(2, "0")).join(""); toast("Subscribe to this topic in the ntfy app, then Save and send a test page."); };
      const val = (id) => { const i = $("#" + id); return i.value.trim() ? i.value.trim() : i.dataset.clear ? "" : undefined; };
      $("#s-save").onclick = async () => {
        const body = { notify: { ntfy: val("s-ntfy"), ntfyServer: val("s-ntfyServer"), webhook: val("s-webhook"), pagerduty: val("s-pd"), escalate: { ntfy: val("s-esc-ntfy"), webhook: val("s-esc-webhook") } },
          settings: { escalateAfterMin: Number($("#s-esc").value), handoffHour: Number($("#s-ho").value), nightFrom: Number($("#s-nf").value), nightTo: Number($("#s-nt").value) } };
        const tok = val("s-tg-token"), chat = val("s-tg-chat");
        if (tok !== undefined || chat !== undefined) body.notify.telegram = { token: tok, chat };
        const r = await backend.saveSettings(body);
        if (r && r.ok) { toast("Saved. Now send a test page."); route(true); }
        else $("#s-out").innerHTML = `<span class="lvl-fail">✖ ${esc((r && r.error) || "not saved")}</span>`;
      };
      $("#s-test").onclick = async () => {
        $("#s-out").textContent = "Sending…";
        const r = await backend.testPage();
        $("#s-out").innerHTML = r && r.results && r.results.length ? r.results.map((x) => `<span class="${x.ok ? "lvl-ok" : "lvl-fail"}">${x.ok ? "✓" : "✖"} ${esc(x.channel)}${x.detail ? ": " + esc(x.detail) : ""}</span>`).join(" · ") : "No channels set up yet.";
      };
    },
  };

  // ---------- change ledger ----------
  let chFilter = "all";
  pages.changes = {
    mount() {
      const origin = location.origin && /^http/.test(location.origin) && !staticSite ? location.origin : "http://localhost:7300";
      $("#view").innerHTML = `<div class="eyebrow">Change ledger</div><h2>What changed, and who changed it</h2>
        <p class="lede">Most 3 AM pages start with a change. WisprOps records the config changes and releases it sees, the changes your CI and deploy scripts report, and its own fixes, and quotes the ones before a failure as evidence. Say “what changed?” in the Talk box to hear them.</p>
        <div class="row" id="ch-f" style="margin-top:12px"></div>
        <ol class="ledger" id="ch-list" style="margin-top:12px"></ol>
        <div class="card" style="margin-top:14px"><h3>Report changes from CI or a deploy script</h3><pre># a deploy script on the same machine
node bin/wisprops.js change "deployed $(git rev-parse --short HEAD)" --service api --by "$USER"

# CI (GitHub Actions, GitLab, anything that can curl)
curl -X POST ${esc(origin)}/api/change -H "Authorization: Bearer $WISPROPS_ALERT_TOKEN" -H "Content-Type: application/json" \\
  -d '{"service":"api","text":"deployed v43","by":"sam","kind":"deploy"}'</pre>
          <p class="small muted">Releases from git are attributed to the commit's author automatically.</p></div>`;
      this.update();
    },
    update() {
      const S = V().state, all = (S.changes || []).slice().reverse();
      const F = { all: () => true, before: (c) => !!c.incidentId && c.source !== "wisprops", reported: (c) => !/detected|observed|wisprops/.test(c.source || ""), wisprops: (c) => c.source === "wisprops" };
      const L = { all: "All", before: "Led to an incident", reported: "Reported by CI or people", wisprops: "Made by WisprOps" };
      $("#ch-f").innerHTML = Object.keys(F).map((k) => `<button class="tiny ${chFilter === k ? "primary" : ""}" data-chf="${k}">${L[k]} · ${all.filter(F[k]).length}</button>`).join("");
      $$("[data-chf]").forEach((b) => (b.onclick = () => { chFilter = b.dataset.chf; this.update(); }));
      const src = (c) => (c.source === "detected" ? "found by WisprOps when it broke" : c.source === "observed" ? "seen while healthy" : c.source === "wisprops" ? "applied by WisprOps" : "reported by " + c.source);
      const list = all.filter(F[chFilter]).slice(0, 100);
      $("#ch-list").innerHTML = list.length ? list.map((c) => { const i = c.incidentId && S.incidents.find((x) => x.id === c.incidentId);
        return `<li class="card"><div class="spread"><span><span class="t">${esc(new Date(c.at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }))}</span> <b class="mono">${esc(c.service)}</b> · ${esc(c.text)}</span><span class="row"><span class="chip ${c.source === "wisprops" ? "accent" : /detected/.test(c.source) ? "warn" : ""}">${esc(c.kind)}</span>${i ? `<a class="chip bad" href="#/board?inc=${esc(i.id)}">→ #${i.n} ${esc(i.status)}</a>` : ""}</span></div>
          <div class="small muted">${c.by ? "by <b>" + esc(c.by) + "</b> · " : ""}${esc(src(c))}</div></li>`; }).join("")
        : `<li class="card empty">${chFilter === "all" ? (inDemo() ? "No changes yet. Break something in the sandbox and the change behind it lands here, with who made it." : "Changes appear as WisprOps sees them or your CI reports them.") : "Nothing matches this filter."}</li>`;
    },
  };

  // ---------- handoff ----------
  pages.handoff = {
    mount() {
      $("#view").innerHTML = `<div class="eyebrow">Morning handoff</div><h2>What happened while you slept</h2>
        <div class="card" style="margin-top:12px"><pre class="prose" id="ho-text"></pre>
        <div class="row" style="margin-top:12px"><button id="ho-read">🔊 Read it to me</button><button id="ho-copy">Copy</button><span class="small muted">Or say “what happened last night?”.</span></div></div>
        <p class="small muted" style="margin-top:10px">The server pushes this to your phone (ntfy) or team chat every morning at the hour you set (<code>settings.handoffHour</code>).</p>
        <h3 style="margin-top:18px">Activity</h3><ol class="activity" id="ho-act"></ol>`;
      $("#ho-read").onclick = () => { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(V().handoff)); };
      $("#ho-copy").onclick = () => navigator.clipboard.writeText(V().handoff).then(() => toast("Copied."), () => toast("Copy blocked."));
      this.update();
    },
    update() {
      const v = V();
      $("#ho-text").textContent = v.handoff;
      const act = (v.state.activity || []).slice(-30).reverse();
      $("#ho-act").innerHTML = act.length ? act.map((a) => `<li><span class="t">${esc(clock(a.at))}</span> <span class="who ${a.who}">${a.who === "agent" ? "WisprOps" : esc(a.who)}</span> ${esc(a.text)}</li>`).join("") : `<li class="muted small">Snoozes, kill-switch changes and commands said with nothing selected show up here.</li>`;
    },
  };

  // ---------- analytics ----------
  pages.analytics = {
    mount() {
      const v = V(), st = v.stats;
      const done = v.state.incidents.filter((i) => i.status === "resolved");
      const hot = Object.entries(v.state.incidents.reduce((a, i) => { if (i.cause) { const k = i.service + " · " + i.cause.kind.replace(/_/g, " "); a[k] = (a[k] || 0) + 1; } return a; }, {})).sort((a, b) => b[1] - a[1]).filter((x) => x[1] > 1);
      const max = Math.max(10000, ...done.map((i) => i.mttr));
      const Wd = 640, rowH = 30, H = Math.max(1, done.length) * rowH + 24, L = 150;
      const bars = done.slice(-20).map((i, k) => { const w = Math.max(3, ((Wd - L - 140) * i.mttr) / max); const y = 8 + k * rowH;
        return `<g><title>#${i.n} ${esc(i.service)}: ${dur(i.mttr)}</title><text class="axis-t" x="${L - 8}" y="${y + 15}" text-anchor="end">#${i.n} ${esc(i.service)}</text><path fill="${i.orderId ? "var(--accent)" : "var(--series-1)"}" d="M${L},${y + 4} H${L + w - 4} Q${L + w},${y + 4} ${L + w},${y + 8} V${y + 16} Q${L + w},${y + 20} ${L + w - 4},${y + 20} H${L} Z"/><text class="axis-t" x="${L + w + 6}" y="${y + 16}">${dur(i.mttr)}${i.orderId ? " · standing order" : ""}</text></g>`; }).join("");
      $("#view").innerHTML = `<div class="eyebrow">Analytics</div><h2>Recovery, pages and approvals</h2>
        <div class="tally"><div class="card stat"><div class="n">${dur(st.mttrMs)}</div><div class="l">mean time to verified recovery</div></div><div class="card stat"><div class="n">${dur(st.mttaMs)}</div><div class="l">mean time to acknowledge</div></div>
        <div class="card stat"><div class="n">${st.woken}</div><div class="l">pages sent${st.escalations ? ` · ${st.escalations} escalated` : ""}</div></div><div class="card stat"><div class="n">${st.approvals}</div><div class="l">changes approved through Wispr · ${st.refusals} refused</div></div></div>
        <div class="tally"><div class="card stat"><div class="n">${st.minutesSaved != null ? st.minutesSaved + " min" : "–"}</div><div class="l">saved against a 53-minute industry MTTR</div></div><div class="card stat"><div class="n">${st.repeats || 0}</div><div class="l">repeat failures (same cause within 7 days)</div></div>
        <div class="card stat"><div class="n">${st.changes || 0}</div><div class="l">changes in the ledger</div></div><div class="card stat"><div class="n" style="font-size:22px">${hot.length ? esc(hot[0][0]) : "–"}</div><div class="l">${hot.length ? `breaks most (${hot[0][1]}×): grant a standing order, or fix it at the source` : "no repeat offender yet"}</div></div></div>
        <div class="card"><h3>Time to verified recovery, per incident</h3>${done.length ? `<div class="legend small muted"><span><i style="background:var(--series-1)"></i>your Wispr approval</span><span><i style="background:var(--accent)"></i>standing order</span></div><div class="chart-wrap"><svg viewBox="0 0 ${Wd} ${Math.min(H, 20 * rowH + 24)}" role="img" aria-label="Time to verified recovery for each resolved incident">${bars}</svg></div>
          <table class="t" style="margin-top:10px"><tr><th>Incident</th><th>Cause</th><th>Fixed by</th><th class="n">Ack</th><th class="n">Recovery</th></tr>${done.slice().reverse().map((i) => `<tr><td>#${i.n} ${esc(i.service)}</td><td>${esc(i.cause.kind.replace(/_/g, " "))}</td><td>${i.orderId ? "standing order " + esc(i.orderId) : "your Wispr approval"}</td><td class="n">${i.woken ? dur(i.mtta) : "–"}</td><td class="n">${dur(i.mttr)}</td></tr>`).join("")}</table>`
          : `<p class="muted">Nothing resolved yet. ${inDemo() ? "Play the demo from the Demo section and this fills in." : ""}</p>`}</div>`;
    },
  };

  // ---------- Wispr ----------
  pages.wispr = {
    mount() {
      const v = V();
      const ch = v.channels || {};
      $("#view").innerHTML = `<div class="eyebrow">Wispr Flow</div><h2>Your voice is the approval</h2>
        <p class="lede">Wispr Flow types what you say into whatever field has focus. WisprOps is built around that: the Talk box, runbook notes, and handoff notes are all plain text fields, and the words that change production must arrive as one Wispr Flow dictation, not as keystrokes. You approve a fix by saying it back (“approve fix one, db_pool back to 20”), and every fact you say is checked against the fix. In the morning, one long dictation starting with “debrief:” becomes the postmortem.</p>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>1 · Check your setup</h3>
            <p class="small muted">Click the box, hold your Wispr Flow hotkey, and say <b>“approve fix one, db_pool back to 20”</b>. Nothing is sent anywhere.</p>
            <label class="sr" for="wt">Wispr test box</label>
            <textarea id="wt" class="dict" rows="2" placeholder="Hold your Wispr Flow key and speak…"></textarea>
            <div class="small" id="wt-m"></div>
            <div class="small" id="wt-r" style="margin-top:6px"></div>
            <button class="tiny" id="wt-clear" style="margin-top:6px">Clear</button>
          </div>
          <div class="card"><h3>2 · Teach Flow your words</h3>
            <p class="small muted">Add these to your Flow dictionary (Flow → Dictionary) so service names and config keys come out right at 3 AM, half asleep.</p>
            <div class="row" id="dict"></div>
            <button class="tiny" id="dict-copy" style="margin-top:8px">Copy all</button>
          </div>
        </div>

        <div class="card" style="margin-top:14px"><h3>3 · Phrasebook</h3>
          <p class="small muted">Say these in the Talk box. English or Hinglish; Flow handles both. Fillers and self-corrections are removed by Flow before WisprOps sees the text.</p>
          <table class="t"><tr><th>Say</th><th>Hinglish</th><th>What happens</th><th>Needs a Wispr burst</th></tr>${W.PHRASES.map((p) => `<tr><td class="mono">${esc(p.say)}</td><td class="mono muted">${esc(p.hinglish || "")}</td><td>${esc(p.does)}</td><td>${p.wispr ? `<span class="chip accent">Wispr only</span>` : `<span class="small muted">typing is fine</span>`}</td></tr>`).join("")}</table>
          <p class="small muted" style="margin-top:8px">Vague yeses (“yes, do it”, “haan kar do”) never approve anything. Speak over a read-back and the fix is withdrawn.</p>
        </div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>4 · Answer a page from your phone</h3>
            <ol class="steps small">
              <li>Install the free <a href="https://ntfy.sh" target="_blank" rel="noopener">ntfy</a> app and subscribe to a secret topic. Put it in <code>notify.ntfy</code>. ${ch.ntfy ? `<span class="chip ok">configured</span>` : ""}</li>
              <li>Put a tunnel with HTTPS in front of port 7300 (Cloudflare Tunnel, Tailscale Serve, ngrok…), then run <code>wisprops token --host &lt;its hostname&gt;</code>. ${v.remote && v.remote.hosts && v.remote.hosts.length ? `<span class="chip ok">${esc(v.remote.hosts.join(", "))}</span>` : ""}</li>
              <li>Open the link from <code>wisprops token --show</code> once on your phone. It signs that phone in for 30 days.</li>
              <li>When a page arrives, tap it, then dictate into the Talk box with Wispr Flow on your phone if you have it.</li>
            </ol>
            <p class="small muted">Without a token, WisprOps only answers on localhost.</p>
          </div>
          <div class="card"><h3>5 · Wispr Flow's MCP, next to WisprOps's</h3>
            <p class="small muted">In Claude Code (or any MCP client), connect both. Your assistant can then file Wispr Flow meeting decisions and scratchpad notes into incidents and runbooks. It can never approve, apply, acknowledge or snooze anything.</p>
            <pre>claude mcp add --transport http wispr-flow https://api.wisprflow.ai/connect/mcp
claude mcp add wisprops -- node /path/to/wisprops/bin/wisprops.js mcp</pre>
            <p class="small">Try: <i>“Find the decision about db_pool in yesterday's Wispr Flow meeting summary and add it to the checkout-api runbook in WisprOps.”</i></p>
          </div>
        </div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>🙌 No hands mode</h3>
            <p class="small muted">Turn it on in the Talk panel (or say nothing and press <kbd>/</kbd> once). Then: pages are read aloud and the cursor waits in the Talk box; a Wispr Flow dictation sends itself after a short pause; replies are spoken; “open the audit log”, “open incident two”, “what changed?” move the console. Pair it with Flow's hands-free mode and you never touch the keyboard. Typed text never auto-sends.</p>
            <button class="tiny ${noHands ? "primary" : ""}" id="nh-toggle">${noHands ? "✓ No hands is on" : "Turn on No hands"}</button></div>
          <div class="card"><h3>Hindi, Hinglish and Devanagari</h3>
            <p class="small muted">With Hindi on, Flow sometimes writes English command words in Devanagari. WisprOps maps them, so <span class="mono">अप्रूव फिक्स वन</span> approves fix one and <span class="mono">मैं देख रहा हूँ</span> acknowledges. Hindi numbers are deliberately not mapped for approvals: <span class="mono">दो</span> means both “two” and “give”.</p></div>
        </div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>Your phone: Telegram + Flow's keyboard</h3>
            <p class="small muted">Set <code>notify.telegram</code> and pages arrive in Telegram with <b>What happened?</b> and <b>I'm on it</b> buttons. Reply with Wispr Flow's phone keyboard: “I'm on it”, “note: …”, “snooze checkout for thirty minutes”, “freeze production”. A Telegram message can never approve: Telegram can't tell typing from dictation, so approvals happen in the console. ${ch.telegram ? `<span class="chip ok">connected</span>` : ""}</p></div>
          <div class="card"><h3>Set things up by voice</h3>
            <p class="small muted">On the Services page, describe a service out loud (“watch my api in docker container api, health check at localhost 3000 slash healthz”) and WisprOps adds it. Dictate a runbook note with a number in it (“db_pool must stay at 20 or more”) and it becomes a rule WisprOps enforces.</p></div>
        </div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>Command Mode for notes</h3><p class="small muted">Dictated a messy runbook note? Select it in the box and use Flow's Command Mode (“make this two short sentences”) before saving. Command Mode needs a paid Wispr plan or trial.</p></div>
          <div class="card"><h3>Desktop alerts and a test page</h3><p class="small muted">Let this tab show a system notification when a page arrives while it is in the background. Then send a harmless test page to every channel you set up (ntfy, Slack or Discord, PagerDuty, Telegram), so you know it reaches you before 3 AM does.</p>
            <div class="row"><button class="tiny" id="notif">${"Notification" in window && Notification.permission === "granted" ? "✓ Allowed" : "Allow notifications"}</button>${backend.testPage ? `<button class="tiny primary" id="test-page">Send a test page</button>` : `<span class="small muted">The test page works on your own WisprOps, not in the sandbox.</span>`}</div>
            <div id="test-out" class="small" style="margin-top:6px"></div></div>
        </div>

        <div class="card" style="margin-top:14px"><h3>What the Wispr check proves, and what it doesn't</h3>
          <p class="small muted">Wispr Flow inserts a whole dictation at once; typing arrives a key at a time. WisprOps counts both and records them on the receipt. Pasted text also arrives at once, so the check proves “not typed key by key”, not “spoken”. There is no Wispr API for a web page to start dictation or read it, so WisprOps never claims more than that. To allow typed approvals anyway (flagged on every receipt), set <code>settings.allowTyped</code>.</p></div>`;
      const wt = $("#wt");
      const tr = trackWispr(wt, $("#wt-m"), { idle: "Waiting for Wispr…" });
      wt.addEventListener("input", () => {
        const intent = W.parseIntent(wt.value), m = tr.meta();
        const c = intent.back ? W.consent(m, wt.value.trim().length, {}, wt.value) : W.consent(m, 11, {});
        const what = { approve: intent.back ? `would approve fix ${intent.n} if what you said back (“${intent.back}”) matches the fix` : `would ask you to say fix ${intent.n} back first`, debrief: "files a debrief in the postmortem: causes, action items with owners, rules", undo: `would undo fix ${intent.n}`, grant: `would grant a standing order for ${intent.days} days${intent.night ? ", nights only" : ""}`, renew: `would renew ${intent.id || "the standing order"} for ${intent.days} days`, ack: "acknowledges the page", vague: "is too vague; it never approves anything", unclear: "sounds like a question or a “no”, so it changes nothing", snooze: `snoozes ${intent.target || "the selected service"} for ${intent.minutes} min`, note: "adds a handoff note", handoff: "reads the morning handoff", explain: "explains the incident", changes: "reads the change ledger", freeze: "freezes production (no Wispr needed to make things safer)", unfreeze: "would unfreeze production", go: `opens ${intent.page}`, select: `selects incident #${intent.n}`, chat: "isn't a command" }[intent.kind] || intent.kind;
        $("#wt-r").innerHTML = wt.value.trim() ? `Heard “${esc(wt.value.trim())}”: ${esc(what)}.${W.CONSENT_KINDS.includes(intent.kind) ? (c.ok ? ` <span class="chip accent">✓ would be accepted: Wispr burst</span>` : ` <span class="chip bad">✖ would be refused: typed</span>`) : ""}` : "";
      });
      $("#wt-clear").onclick = () => { tr.reset(); $("#wt-r").innerHTML = ""; };
      $("#nh-toggle").onclick = () => { setNoHands(!noHands); route(true); };
      const tp = $("#test-page");
      if (tp) tp.onclick = async () => {
        tp.disabled = true; $("#test-out").textContent = "Sending…";
        const r = await backend.testPage();
        $("#test-out").innerHTML = r && r.results && r.results.length ? r.results.map((x) => `<div class="${x.ok ? "lvl-ok" : "lvl-fail"}">${x.ok ? "✓" : "✖"} ${esc(x.channel)}${x.detail ? ": " + esc(x.detail) : ""}</div>`).join("") : esc((r && (r.error || r.reply)) || "No channels are set up yet. Add notify.ntfy (or a webhook, PagerDuty or Telegram) to wisprops.config.json and restart.");
        tp.disabled = false;
      };
      $("#notif").onclick = async () => { if (!("Notification" in window)) return toast("This browser has no notifications."); const p = await Notification.requestPermission(); $("#notif").textContent = p === "granted" ? "✓ Allowed" : "Blocked"; };
      $("#dict-copy").onclick = () => navigator.clipboard.writeText(dictWords().join("\n")).then(() => toast("Copied. Paste them into Flow's dictionary."), () => toast("Copy blocked."));
      this.update();
    },
    update() { $("#dict").innerHTML = dictWords().map((w) => `<span class="chip">${esc(w)}</span>`).join(""); },
  };
  function dictWords() {
    const v = V(), set = new Set(["WisprOps", "standing order", "approve fix", "postmortem", "rollback", "ntfy"]);
    v.services.forEach((s) => { set.add(s.name); if (/[-_]/.test(s.name)) set.add(s.name.replace(/[-_]/g, " ")); });
    Object.values(v.state.svc || {}).forEach((r) => { if (r.golden && r.golden.config) Object.keys(r.golden.config).forEach((k) => set.add(k)); });
    return Array.from(set).slice(0, 80);
  }

  // ---------- demo ----------
  const SCENARIOS = [
    { id: "night", title: "The 3 AM config change", minutes: "2 min", body: "A deploy bot sets db_pool to 0. You try “yes, do it” and a typed approval (both refused), then say the fix back with Wispr, first with the wrong value (refused), then right: “approve fix one, DB pool back to twenty”. WisprOps verifies the recovery, you grant a standing order, and the repeat is fixed while you sleep. In the morning, one dictated debrief becomes the postmortem.", fault: "config", say: "approve fix one, db_pool back to 20" },
    { id: "deploy", title: "A bad release, rolled back and forward", minutes: "40 s", body: "Release v42 ships with a TypeError. WisprOps sees the release changed, proposes a rollback to v41, and after you approve it, “undo fix one” rolls forward again.", fault: "deploy", say: "approve fix one, roll checkout back to v41" },
    { id: "bargein", title: "Interrupt the read-back", minutes: "30 s", body: "The auth process dies. WisprOps reads the fix aloud; you speak over it, so the fix is withdrawn and its phrase stops working. It comes back as fix two.", fault: "crash", say: "read it back" },
    { id: "alert", title: "An alert nothing on the allowlist fits", minutes: "20 s", body: "Grafana says auth is slow, but its health check passes and nothing changed. WisprOps does not guess at a restart. It pages you and says why.", fault: "alert", say: "what happened?" },
    { id: "maint", title: "Maintenance window", minutes: "25 s", body: "You say “snooze checkout api for thirty minutes” before a risky change. It breaks, WisprOps keeps watching, and nobody is paged until you say “resume paging”.", fault: "config", say: "snooze checkout api for thirty minutes" },
    { id: "nohands", title: "No hands, start to finish", minutes: "35 s", body: "No hands mode is on. The page is read aloud, the cursor is already in the Talk box, and every Wispr dictation sends itself. You ask what happened, approve it, and open the audit log without a key or a click.", fault: "crash", say: "what happened?" },
    { id: "changes", title: "Who changed it?", minutes: "35 s", body: "CI reports a deploy, and release v42 breaks checkout. WisprOps names the deploy and who shipped it, keeps only the log lines that matter, and answers “what changed?” before you approve the rollback.", fault: "deploy", say: "what changed?" },
    { id: "freeze", title: "Freeze production", minutes: "30 s", body: "Mid-incident, someone types “freeze production”. Now even a Wispr approval applies nothing. Typing “unfreeze production” is refused; saying it with Wispr lifts the freeze.", fault: "config", say: "freeze production" },
    { id: "rule", title: "A rule you dictated", minutes: "35 s", body: "You dictate “db_pool must stay at 10 or more”. Someone sets it to 5: still healthy, but WisprOps keeps 5 out of its snapshot. When it drops to 0, the fix restores 20, not 5.", fault: "config", say: "db_pool must stay at 10 or more" },
  ];
  pages.demo = {
    mount() {
      $("#view").innerHTML = `<section class="hero"><div class="eyebrow">Demo sandbox</div><h1>See an incident end to end.</h1>
        <p class="lede">A simulated production (<b>checkout-api</b> and <b>auth</b>) runs in this tab with the real WisprOps agent. Watch a scenario play out, or break something and answer the page yourself with Wispr Flow in the Talk box. Scripted lines are marked “simulated Wispr” everywhere, including the audit log.</p>
        <div class="row" style="margin-top:10px"><button id="dm-reset">Reset the sandbox</button>${staticSite ? `<a class="btn" href="#/docs">Run it on your own services →</a>` : `<button id="dm-exit">Exit demo → my services</button>`}</div></section>
        <div class="card start-here" style="margin-top:14px"><div class="eyebrow">Start here · 2 minutes</div>
          <ol class="steps"><li><b>Watch</b> <i>The 3 AM config change</i> below: “yes, do it” and a typed approval are refused, a wrong say-back is refused, the right one said with Wispr Flow is applied and proven, and the repeat is fixed while nobody is woken.</li>
          <li><b>Try it</b> with your own Wispr Flow: press <i>Try it yourself</i>, click the Talk box, hold your Wispr key and say the fix back: “approve fix one, db_pool back to 20”. Say 10 instead of 20, or type it, and watch it get refused.</li>
          <li><b>Go hands-free:</b> turn on <b>🙌 No hands</b> in the Talk panel. The page is read aloud, your dictation sends itself, and “open the audit log” moves the screen.</li>
          <li><b>Check the proof</b> on <a href="#/receipts">Audit</a>: every change, the exact words that allowed it, how they arrived, and an approval record.</li></ol>
          <div class="row"><button class="primary" data-run="night" ${playing ? "disabled" : ""}>▶ Watch the 3 AM night</button><button data-run="nohands" ${playing ? "disabled" : ""}>▶ Watch it with no hands</button></div></div>
        <div class="grid g2" style="margin-top:16px">${SCENARIOS.map((s) => `<div class="card scenario"><div class="spread"><h3>${esc(s.title)}</h3><span class="chip">${esc(s.minutes)}</span></div><p class="small muted">${esc(s.body)}</p>
          <div class="row"><button class="primary tiny" data-run="${s.id}" ${playing ? "disabled" : ""}>▶ Watch it</button><button class="tiny" data-try="${s.id}" ${playing ? "disabled" : ""}>Try it yourself</button></div></div>`).join("")}</div>
        <div class="card" style="margin-top:14px"><h3>Then run it on something real</h3><p class="small muted">The same agent, watching a real HTTP service on your machine that you can break from a terminal:</p>
<pre>git clone ${REPO} && cd wisprops
node bin/wisprops.js demo            # demo service + console; open the sign-in link it prints
node bin/wisprops.js break config    # in a second terminal (also: deploy, crash)</pre></div>`;
      $("#dm-reset").onclick = () => { cancelScenario(); demo.stop(); demo = demoBackend(); backend = demo; demo.start(); selected = null; toast("Fresh sandbox."); route(true); };
      const ex = $("#dm-exit"); if (ex) ex.onclick = exitDemo;
      $$("[data-run]").forEach((b) => (b.onclick = () => runScenario(b.dataset.run)));
      $$("[data-try]").forEach((b) => (b.onclick = () => tryScenario(b.dataset.try)));
    },
    update() { $$("[data-run],[data-try]").forEach((b) => (b.disabled = playing)); },
  };
  async function tryScenario(id) {
    const s = SCENARIOS.find((x) => x.id === id);
    location.hash = "#/board";
    await waitFor(() => backend.agent.rt("checkout-api").golden && backend.agent.rt("auth").golden, 15000);
    if (id === "maint") { toast("First, hold your Wispr key and say: “snooze checkout api for thirty minutes”. Then press ⚡ “Someone sets db_pool to 0”."); focusTalk(); return; }
    if (id === "rule") { location.hash = "#/services"; toast("Dictate into checkout-api's runbook box: “db_pool must stay at 10 or more”, save it, then break it from the board."); return; }
    if (id === "nohands") setNoHands(true);
    breakIt(s.fault);
    toast(`Broken. When the page lands, hold your Wispr key and say “${s.say}”.`);
    focusTalk();
  }

  // ---------- scripted scenarios (demo only; every line is marked “simulated Wispr”) ----------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(fn, ms) { const t0 = Date.now(); while (Date.now() - t0 < (ms || 30000)) { const v = fn(); if (v) return v; await sleep(250); } return null; }
  async function typeInto(text, burst) {
    const ta = $("#say"); ta.focus({ preventScroll: true });
    if (burst) { await sleep(500); ta.value = text; talkTrack.set(text.length, 0); }
    else { ta.value = ""; for (const ch of text) { ta.value += ch; talkTrack.set(0, ta.value.length); await sleep(45); } }
    await sleep(700);
  }
  function guard() { if (!scenarioDemo || backend !== scenarioDemo) throw new Error("you left the demo"); }
  async function say(text, burst, extra) {
    guard(); await typeInto(text, burst); guard();
    const m = Object.assign(burst ? { via: "sim", insert: text.length, keys: 0 } : { via: "typed", insert: 1, keys: text.length }, extra || {});
    $("#say").value = ""; talkTrack.reset();
    await send(text, m, scenarioDemo); await sleep(1400);
  }
  const waitInc = (pred, ms) => waitFor(() => backend.agent.state.incidents.find(pred), ms || 20000);
  async function runScenario(id) {
    if (playing) return;
    if (!inDemo()) enterDemo();
    cancelScenario(); playing = true;
    demo.stop(); demo = demoBackend(); backend = demo; demo.start(); selected = null; scenarioDemo = demo;
    const run = scenarioRun, mine = demo;
    const alive = () => { if (run !== scenarioRun || backend !== mine) throw new Error("stopped"); };
    const sayR = async (t, burst, extra) => { alive(); return say(t, burst, extra); };
    location.hash = "#/board"; $("#banner").hidden = true; route(true);
    const A = backend.agent, F = backend.sim.faults;
    try {
      toast("Everything is healthy. WisprOps takes a last-healthy snapshot.");
      await waitFor(() => A.rt("checkout-api").golden && A.rt("auth").golden, 15000);
      await sleep(1200);
      if (id === "night") {
        toast("23:40. A deploy bot sets db_pool to 0 on checkout-api.");
        alive(); F.config.run();
        const inc = await waitInc((i) => i.status === "proposed");
        if (!inc) throw new Error("no incident");
        selected = inc.id; refresh(); await sleep(2500);
        await sayR("Yes, do it.", true);
        await sayR("approve fix 1", false);
        await sayR("Approve fix one.", true);
        await sayR("Approve fix one, DB pool back to ten.", true);
        await sayR("Approve fix one, DB pool back to twenty on checkout API.", true);
        await waitFor(() => inc.status === "resolved", 25000);
        await sleep(1500);
        await sayR("Grant standing order for seven days.", true);
        await sleep(2000);
        toast("03:10. The same thing happens again…");
        alive(); F.config.run();
        const inc2 = await waitInc((i) => i.id !== inc.id && i.orderId);
        if (inc2) { selected = inc2.id; refresh(); await waitFor(() => inc2.status === "resolved", 25000); }
        toast("Fixed under the standing order. Nobody was woken.");
        await sleep(2500);
        toast("Morning. You hold the Wispr key and talk it through for a minute.");
        selected = inc.id; refresh(); await sleep(1500);
        await sayR("Debrief: deploy-bot ran the staging profile against production, which set db_pool to zero. Sam will make the deploy job refuse a zero pool by Friday. We should alert on pool exhaustion before the health check fails. The standing order worked, nobody was woken the second time. db_pool must stay at 10 or more.", true);
        await sleep(5000); alive();
        $("#modal").hidden = true;
        location.hash = "#/handoff";
      } else if (id === "deploy") {
        toast("Release v42 goes out with a bug.");
        F.deploy.run();
        const inc = await waitInc((i) => i.status === "proposed");
        selected = inc.id; refresh(); await sleep(2000);
        await sayR("What happened?", true);
        await sayR("Approve fix one, roll checkout back to v41.", true);
        await waitFor(() => inc.status === "resolved", 25000);
        await sleep(1200);
        toast("Turns out v42 was needed. Roll it forward.");
        await sayR("Undo fix one.", true);
        await sleep(1500);
        location.hash = "#/receipts";
      } else if (id === "bargein") {
        toast("The auth process dies.");
        F.crash.run();
        const inc = await waitInc((i) => i.status === "proposed");
        selected = inc.id; refresh(); await sleep(1500);
        toast("WisprOps reads fix one aloud…");
        alive(); readBack(); await sleep(1800);
        await sayR("Wait, is the database okay?", true, { duringReadback: true });
        await sayR("Approve fix one.", true);
        await sleep(800);
        await sayR("Approve fix two, restart auth.", true);
        await waitFor(() => inc.status === "resolved", 25000);
      } else if (id === "alert") {
        toast("Grafana fires: auth p95 latency over 2 s.");
        breakIt("alert");
        const inc = await waitInc((i) => i.cause && i.cause.kind === "external_alert");
        selected = inc.id; refresh(); await sleep(2000);
        await sayR("Main dekh raha hoon.", true);
        await sayR("note: slow query on the sessions table, adding an index in the morning", true);
        await sleep(1200);
      } else if (id === "maint") {
        await sayR("Snooze checkout api for thirty minutes.", true);
        toast("A risky config change during the window…");
        alive(); F.config.run();
        await sleep(6000);
        toast("checkout-api is failing, but nobody was paged.");
        await sleep(1500);
        await sayR("Resume paging.", true);
        const inc = await waitInc((i) => i.serviceId === "checkout-api");
        if (inc) { selected = inc.id; refresh(); }
        toast("Paging is back on, so the failure pages you now.");
      } else if (id === "nohands") {
        const was = noHands;
        setNoHands(true);
        toast("No hands is on. Watch: no clicks from here.");
        await sleep(1500);
        alive(); F.crash.run();
        const inc = await waitInc((i) => i.status === "proposed");
        selected = inc.id; refresh(); await sleep(5000);
        await sayR("What happened?", true);
        await sleep(1500);
        await sayR("Approve fix one, restart auth.", true);
        await waitFor(() => inc.status === "resolved", 30000);
        await sleep(1200);
        await sayR("Open the audit log.", true);
        await sleep(1500);
        if (!was) { setNoHands(false); toast("Done. No hands is back off; turn it on in the Talk panel."); }
      } else if (id === "changes") {
        toast("CI reports a deploy of v42 to checkout-api.");
        alive(); await A.reportChange("checkout-api", { kind: "deploy", text: "pipeline #771 deployed v42 (pricing refactor)", by: "GitHub Actions, merge of #418 by sam", source: "CI" });
        F.deploy.run();
        const inc = await waitInc((i) => i.status === "proposed");
        selected = inc.id; refresh(); await sleep(2500);
        await sayR("What changed?", true);
        await sleep(1500);
        await sayR("Approve fix one, roll checkout back to v41.", true);
        await waitFor(() => inc.status === "resolved", 25000);
        await sleep(1000);
        location.hash = "#/changes";
      } else if (id === "freeze") {
        F.config.run();
        const inc = await waitInc((i) => i.status === "proposed");
        selected = inc.id; refresh(); await sleep(2000);
        toast("Someone else on the call panics and types it.");
        await sayR("freeze production", false);
        await sayR("Approve fix one, DB pool back to 20.", true);
        await sayR("unfreeze production", false);
        await sayR("Unfreeze production.", true);
        await sayR("Approve fix one, DB pool back to 20.", true);
        await waitFor(() => inc.status === "resolved", 25000);
      } else if (id === "rule") {
        location.hash = "#/services"; await sleep(1500);
        toast("You dictate a runbook note with a number in it.");
        alive(); await backend.runbook("checkout-api", "db_pool must stay at 10 or more since the October Postgres migration.", { via: "sim" });
        refresh(); await sleep(2500);
        toast("Someone sets db_pool to 5. checkout-api is still healthy…");
        alive(); backend.sim.svcs["checkout-api"].config.db_pool = 5;
        await sleep(5000);
        toast("…but WisprOps kept 5 out of its last-healthy snapshot. Then it drops to 0.");
        location.hash = "#/board"; await sleep(1500);
        alive(); F.config.run();
        const inc = await waitInc((i) => i.status === "proposed");
        selected = inc.id; refresh(); await sleep(2500);
        await sayR("What happened?", true);
        await sayR("Approve fix one, DB pool back to 20.", true);
        await waitFor(() => inc.status === "resolved", 25000);
      }
    } catch (e) { if (run === scenarioRun) toast("Playback stopped: " + e.message); }
    if (run === scenarioRun) { playing = false; scenarioDemo = null; }
    refresh();
  }

  // ---------- docs ----------
  pages.docs = {
    update() {},
    mount() {
      const origin = location.origin && /^http/.test(location.origin) && !staticSite ? location.origin : "http://localhost:7300";
      $("#view").innerHTML = `<div class="eyebrow">Docs</div><h2>Run WisprOps on your own services</h2>
        <p class="lede">It runs on your machine (Node 18.17+, no dependencies, no cloud account) and watches anything with an HTTP health check.</p>
        <div class="card" style="margin-top:12px"><h3>Install and start (one minute)</h3><pre>npm install -g github:${REPO.replace("https://github.com/", "")}
mkdir ~/oncall && cd ~/oncall
wisprops start          # creates the config, prints the sign-in link (like Jupyter)</pre>
          <ol class="steps small"><li>Open the link. On <b>Services</b>, hold your Wispr key and describe your service; it's watched at once. HTTP, a TCP port (“watch postgres on localhost 5432”) or a check command all work.</li>
          <li>On <b>Settings</b>, pick a private ntfy topic, subscribe in the ntfy app, Save, and <b>Send a test page</b>.</li>
          <li><code>wisprops service --write</code> keeps it running after reboot (systemd, launchd or Task Scheduler). Or Docker: <code>docker run -d --network host -v "$PWD/wisprops-data:/data" wisprops</code>.</li></ol>
          <pre>wisprops add api --health http://localhost:3000/healthz --docker api --watch ./config/api.json --git .
wisprops add db --health tcp://localhost:5432
wisprops add replica --health-cmd "pg_isready -h localhost -p 5433"
wisprops doctor         # checks every URL, port, file and command before 3 AM does
wisprops test-page      # proves pages reach you</pre></div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>Three fixes, nothing else</h3><table class="t"><tr><th>It finds</th><th>It proposes</th></tr>
            <tr><td>A config key differs from the last healthy snapshot (JSON, nested keys, or .env)</td><td>Put that one key back (then your reload command, if set)</td></tr>
            <tr><td>A config value breaks a rule you dictated</td><td>Set it to the edge of your rule</td></tr>
            <tr><td>The release changed since the last healthy check</td><td>Roll back to the last healthy release</td></tr>
            <tr><td>The process is not answering</td><td>Restart that service</td></tr>
            <tr><td>Errors, and nothing explains them</td><td>A restart, labelled “a best guess, not a root cause”; never under a standing order</td></tr></table>
            <p class="small muted" style="margin-top:8px">An alert with a passing health check and no change gets no fix: it is escalated to you with the evidence.</p></div>
          <div class="card"><h3>Presets</h3><table class="t">
            <tr><td><code>--docker api</code></td><td>restart, image as release, docker logs</td></tr>
            <tr><td><code>--compose api</code></td><td>docker compose restart and logs</td></tr>
            <tr><td><code>--pm2 api</code></td><td>pm2 restart and logs</td></tr>
            <tr><td><code>--systemd api.service</code></td><td>systemctl restart, journalctl</td></tr>
            <tr><td><code>--kubernetes web --namespace prod</code></td><td>rollout restart, image as release, <code>set image</code> rollback</td></tr></table>
            <p class="small muted" style="margin-top:8px">Anything you write explicitly on the service (<code>restart</code>, <code>rollback</code>, <code>reload</code>, <code>release</code>, <code>logs</code>) wins. Commands are argument arrays run without a shell.</p></div>
        </div>

        <div class="card" style="margin-top:14px"><h3>Send alerts from your monitoring</h3>
          <p class="small muted">Set <code>alerts.token</code>, then point any of these at <code>${esc(origin)}/api/alert</code>. Firing alerts open an incident (or join the open one); resolved ones are ignored, since WisprOps proves recovery itself. Use the service id as the <code>service</code> label, or add <code>?service=&lt;id&gt;</code>.</p>
          <pre># Prometheus Alertmanager (alertmanager.yml)
receivers:
  - name: wisprops
    webhook_configs:
      - url: ${esc(origin)}/api/alert
        http_config: { authorization: { credentials: "&lt;alerts.token&gt;" } }

# Grafana: Contact point → Webhook → URL ${esc(origin)}/api/alert, Authorization header "Bearer &lt;alerts.token&gt;"
# Uptime Kuma: Notification → Webhook → URL ${esc(origin)}/api/alert?service=api&amp;token=&lt;alerts.token&gt;
# Anything else:
curl -X POST ${esc(origin)}/api/alert -H "Authorization: Bearer &lt;alerts.token&gt;" -H "Content-Type: application/json" \\
  -d '{"service":"api","summary":"error rate over 5%"}'</pre></div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>Paging</h3><pre>"notify": {
  "ntfy": "my-secret-topic",          // phone, via the ntfy app
  "webhook": "$SLACK_WEBHOOK_URL",    // Slack or Discord ($VARS come from the environment)
  "pagerduty": "$PD_ROUTING_KEY",     // Events v2: page triggers, recovery resolves
  "escalate": { "ntfy": "backup-topic" }
},
"settings": { "escalateAfterMin": 15, "handoffHour": 7 }</pre>
            <p class="small muted">Unacknowledged pages go to <code>escalate</code> after <code>escalateAfterMin</code>. A verified recovery sends an all-clear: go back to sleep.</p></div>
          <div class="card"><h3>Safety model</h3><ul class="small steps">
            <li>You approve by saying the fix back (“approve fix one, db_pool back to 20”) in one Wispr Flow dictation. Every fact you say is checked against the fix: a wrong value, release or service, or an extra action like “and restart it”, is refused. The agent cannot approve its own change. <code>settings.sayItBack: false</code> (config file only) accepts the bare “approve fix one”.</li>
            <li>Every fix is dry-run before it is proposed and again right before it runs.</li>
            <li>Resolved means N healthy checks in a row and the post-condition (the key is back, the release is the old one).</li>
            <li>Standing orders: one service, one failure signature, one action, capped uses and days, a voice kill switch.</li>
            <li>Server binds to 127.0.0.1. Every write that can change something needs a signed-in console, even on localhost; proxied requests are never treated as local.</li>
            <li>Questions and negations (“don't approve fix one”) never approve. With two incidents waiting, you pick one first.</li>
            <li>The MCP server can read and add notes. It has no tool to approve, apply, undo, acknowledge or snooze.</li></ul></div>
        </div>

        <div class="grid g2" style="margin-top:14px">
          <div class="card"><h3>Telegram</h3><pre>"notify": {
  "telegram": { "token": "$TELEGRAM_BOT_TOKEN", "chat": "123456789" }
}</pre><p class="small muted">Create a bot with @BotFather and message it once; your chat id is in <code>https://api.telegram.org/bot&lt;token&gt;/getUpdates</code>. Pages get <b>What happened?</b> and <b>I'm on it</b> buttons; replies dictated with Wispr Flow's phone keyboard can acknowledge, note, snooze or freeze. They can never approve. Long-polling, so no public URL is needed.</p></div>
          <div class="card"><h3>Kill switches</h3><ul class="small steps">
            <li><b>Freeze</b>: say “freeze production”, press Freeze, or run <code>wisprops freeze</code>. Nothing applies until “unfreeze production” is dictated with Wispr Flow.</li>
            <li><b>Pause standing orders</b>: every failure pages you.</li>
            <li><b>Config</b>: <code>"settings": {"applyEnabled": false}</code> turns every change off; voice can't override it.</li></ul></div>
        </div>
        <div class="card" style="margin-top:14px"><h3>CLI</h3><pre>wisprops init | add [--say "…"] | doctor | token | start | open | status | mcp
wisprops change "deployed v43" --service api --by sam     # change ledger, from CI or a deploy script
wisprops freeze                                            # stop every change now
wisprops judge                                             # a whole night in the terminal, self-checking
wisprops demo | break config|deploy|crash</pre>
          <p class="small muted">Postmortem pull requests: set <code>repo</code> on a service, then say “open the pull request”. WisprOps commits the postmortem (and the source config fix) on a new branch in a throwaway worktree; with <code>"pr": {"push": true}</code> it pushes and opens a draft PR with the GitHub CLI. Nothing is merged at 3 AM.</p></div>`;
    },
  };

  // ---------- postmortem modal ----------
  async function openPostmortem(id) {
    const md = await backend.postmortem(id);
    $("#modal-title").textContent = "Postmortem";
    $("#modal-body").textContent = md;
    $("#modal-actions").innerHTML = `<button id="pm-copy">Copy Markdown</button><button id="pm-dl">Download .md</button><span class="small muted">Say “open the pull request” in the Talk box to file it on a branch.</span>`;
    $("#pm-copy").onclick = () => navigator.clipboard.writeText(md).then(() => toast("Copied."), () => toast("Copy blocked."));
    $("#pm-dl").onclick = () => download(id + "-postmortem.md", md, "text/markdown");
    $("#modal").hidden = false;
    $("#modal-x").focus();
  }

  async function openCertificate(id) {
    const md = await backend.certificate(id);
    if (!md) return toast("No approval record for that change.");
    $("#modal-title").textContent = "Approval record";
    $("#modal-body").textContent = md;
    $("#modal-actions").innerHTML = `<button id="ct-copy">Copy</button><button id="ct-dl">Download .md</button><span class="small muted">The same record goes into the pull request, where someone reads it weeks later.</span>`;
    $("#ct-copy").onclick = () => navigator.clipboard.writeText(md).then(() => toast("Copied."), () => toast("Copy blocked."));
    $("#ct-dl").onclick = () => download(id + "-approval.md", md, "text/markdown");
    $("#modal").hidden = false;
    $("#modal-x").focus();
  }

  let toastT;
  function toast(msg) {
    let el = $("#toast");
    if (!el) { el = document.createElement("div"); el.id = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = msg; el.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove("on"), 4200);
  }
  $("#modal-x").onclick = () => ($("#modal").hidden = true);
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").hidden = true; });
  wireTalk();
  boot();
})();
