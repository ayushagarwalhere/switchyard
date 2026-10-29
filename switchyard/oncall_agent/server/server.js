/* WisprOps local server: runs the agent against real services and serves the console.
   - Binds to 127.0.0.1 only. Requests must carry a localhost Host header (blocks DNS rebinding)
     unless the host is listed in access.hosts. Anything that arrived through a proxy is remote.
   - Reads are open on localhost (the MCP server uses them). Every write that can change something
     needs a console session: open the sign-in link that `wisprops start` prints (like Jupyter),
     or, remotely, the access-token link. Notes are the exception: they cannot change production.
   - Writes must be same-origin JSON (blocks drive-by requests from other sites).
   - /api/alert takes alerts from your monitoring, and /api/change takes change events from CI and
     deploy scripts ("who changed what"), both authenticated with alerts.token. */
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { createAgent, emptyState, auditRows, toCSV, describeService } = require("../src/agent.js");
const { createAdapters } = require("./adapters.js");
const { createNotifier, createPR } = require("./integrations.js");
const { loadConfig, normalize, serviceFromFlags, insideBase } = require("./config.js");
const { createTelegram } = require("./telegram.js");
const { doctor } = require("./doctor.js");
const { parseAlerts } = require("./alerts.js");

const ROOT = path.resolve(__dirname, "..");
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json", ".webmanifest": "application/manifest+json", ".png": "image/png" };
const STATIC = new Set(["/console.html", "/public/console.js", "/public/styles.css", "/public/manifest.webmanifest", "/public/icon.svg", "/src/agent.js", "/src/sim.js"]);
const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

const sha = (s) => crypto.createHash("sha256").update(String(s)).digest("hex");
const safeEq = (a, b) => { const x = Buffer.from(sha(a)), y = Buffer.from(sha(b)); return crypto.timingSafeEqual(x, y); };

function start(opts) {
  const cfgFile = path.resolve(opts.config);
  const baseDir = path.dirname(cfgFile);
  const cfg = loadConfig(cfgFile);
  const port = Number(opts.port || cfg.port || 7300);
  // 127.0.0.1 unless told otherwise. Bound wider (WISPROPS_BIND), only real loopback connections count as local.
  const bind = opts.bind || process.env.WISPROPS_BIND || cfg.bind || "127.0.0.1";
  const loopbackBind = /^(127\.|::1$|localhost$)/.test(bind);
  const dataDir = path.join(baseDir, ".wisprops");
  fs.mkdirSync(dataDir, { recursive: true });
  const stateFile = path.join(dataDir, "state.json");
  const secretFile = path.join(dataDir, "secret");
  let secret = "";
  try { secret = fs.readFileSync(secretFile, "utf8").trim(); } catch (e) { /* first run */ }
  if (!/^[a-f0-9]{64}$/.test(secret)) { secret = crypto.randomBytes(32).toString("hex"); fs.writeFileSync(secretFile, secret + "\n", { mode: 0o600 }); }
  const signInUrl = `http://localhost:${port}/login?key=${secret}`;
  let state = emptyState();
  try { state = Object.assign(emptyState(), JSON.parse(fs.readFileSync(stateFile, "utf8"))); } catch (e) { /* first run */ }

  const clients = new Set();
  const broadcast = (event, data) => { for (const res of clients) res.write(`event: ${event}\ndata: ${JSON.stringify(data || {})}\n\n`); };
  const publicUrl = cfg.publicUrl || (cfg.access.hosts[0] ? `https://${cfg.access.hosts[0]}/` : `http://localhost:${port}/`);
  const notifyOut = createNotifier(cfg, publicUrl);
  let saveT = null, tg = null;
  const writeState = (s) => { const tmp = stateFile + ".tmp"; fs.writeFileSync(tmp, JSON.stringify(s)); fs.renameSync(tmp, stateFile); };
  const hooks = {
    save: (s) => { clearTimeout(saveT); saveT = setTimeout(() => { try { writeState(s); } catch (e) { console.error("[state]", e.message); } }, 200); },
    notify: async (n) => { broadcast("page", n); if (!opts.quiet) console.log(`[${n.kind || "page"}] ${n.title}: ${n.body}`); await Promise.all([notifyOut(n), tg ? tg.notify(n) : null]); },
    openPR: createPR(cfg, baseDir),
  };
  const agent = createAgent({ config: { services: cfg.services, settings: cfg.settings }, adapters: createAdapters(cfg, baseDir, hooks), state });
  agent.on((ev) => { if (ev.type === "state" || ev.type === "timeline") broadcast("state"); });
  function startTelegram() {
    if (tg) { tg.stop(); tg = null; }
    const tgc = cfg.notify.telegram;
    if (tgc && tgc.token && tgc.chat && !opts.noTelegram) { tg = createTelegram({ token: tgc.token, chat: tgc.chat, agent, publicUrl, skipBacklog: true }); tg.start(); }
  }
  startTelegram();

  /** Settings from the console. Paging channels and quiet hours only: anything that loosens safety
      (typed approvals, policy caps, applyEnabled) stays in the config file. Secrets are write-only. */
  const SETTABLE = { handoffHour: [0, 23], escalateAfterMin: [0, 1440], nightFrom: [0, 23], nightTo: [0, 23] };
  const CHECK = {
    ntfy: (v) => /^[\w-]{1,64}$/.test(v) || "ntfy topic: letters, digits, - and _ only",
    ntfyServer: (v) => /^https?:\/\/[^\s]+$/.test(v) || "ntfy server must be a URL",
    webhook: (v) => /^https:\/\/[^\s]+$/.test(v) || "webhook must be an https:// URL",
    pagerduty: (v) => /^[\w-]{16,64}$/.test(v) || "PagerDuty routing key looks wrong",
  };
  function applySettings(body) {
    const raw = JSON.parse(fs.readFileSync(cfgFile, "utf8"));
    raw.notify = raw.notify || {}; raw.settings = raw.settings || {};
    const live = {};
    const n = body.notify && typeof body.notify === "object" ? body.notify : {};
    for (const k of ["ntfy", "ntfyServer", "webhook", "pagerduty"]) {
      if (n[k] === undefined) continue;
      const v = String(n[k] || "").trim();
      if (v && CHECK[k](v) !== true) return { ok: false, error: CHECK[k](v) };
      raw.notify[k] = v; live[k] = v;
    }
    if (n.escalate && typeof n.escalate === "object") {
      raw.notify.escalate = raw.notify.escalate || {}; live.escalate = Object.assign({}, cfg.notify.escalate || {});
      for (const k of ["ntfy", "webhook"]) if (n.escalate[k] !== undefined) {
        const v = String(n.escalate[k] || "").trim();
        if (v && CHECK[k](v) !== true) return { ok: false, error: "backup " + CHECK[k](v) };
        raw.notify.escalate[k] = v; live.escalate[k] = v;
      }
    }
    let tgChanged = false;
    if (n.telegram && typeof n.telegram === "object") {
      const cur = cfg.notify.telegram || {};
      const token = n.telegram.token === undefined ? cur.token || "" : String(n.telegram.token || "").trim();
      const chat = n.telegram.chat === undefined ? String(cur.chat || "") : String(n.telegram.chat || "").trim();
      if (token && !/^\d+:[\w-]{20,}$/.test(token)) return { ok: false, error: "Telegram token looks wrong (123456:ABC…)" };
      if (token && !/^-?\d+$/.test(chat)) return { ok: false, error: "Telegram chat must be your numeric chat id" };
      raw.notify.telegram = Object.assign({}, raw.notify.telegram || {});
      if (n.telegram.token !== undefined) raw.notify.telegram.token = token;
      if (n.telegram.chat !== undefined) raw.notify.telegram.chat = chat;
      live.telegram = { token, chat }; tgChanged = true;
    }
    const s = body.settings && typeof body.settings === "object" ? body.settings : {};
    const liveSet = {};
    for (const [k, [lo, hi]] of Object.entries(SETTABLE)) {
      if (s[k] === undefined) continue;
      const v = Number(s[k]);
      if (!Number.isInteger(v) || v < lo || v > hi) return { ok: false, error: `${k} must be a whole number from ${lo} to ${hi}` };
      raw.settings[k] = v; liveSet[k] = v;
    }
    const tmp = cfgFile + ".wisprops-tmp"; fs.writeFileSync(tmp, JSON.stringify(raw, null, 2) + "\n"); fs.renameSync(tmp, cfgFile);
    Object.assign(cfg.notify, live); Object.assign(cfg.settings, liveSet); Object.assign(agent.settings, liveSet);
    if (tgChanged) startTelegram();
    broadcast("state");
    return { ok: true };
  }

  const services = () => cfg.services.map((s) => ({ id: s.id, name: s.name, health: s.healthLabel, configLabel: s.configLabel || null, preset: s.presetLabel || null, can: s.can, snoozedUntil: agent.snoozedUntil(s.id) || null }));
  // What the console may know about channels: whether each is set, and enough to recognise it. Never the secret.
  const hint = (v) => (!v ? "" : String(v).length <= 6 ? "set" : String(v).slice(0, 3) + "…" + String(v).slice(-2));
  const channels = () => { const n = cfg.notify, e = n.escalate || {}, t = n.telegram || {}; return { ntfy: !!n.ntfy, webhook: !!n.webhook, pagerduty: !!n.pagerduty, telegram: !!tg, escalate: !!(e.ntfy || e.webhook),
    hints: { ntfy: hint(n.ntfy), ntfyServer: n.ntfyServer || "", webhook: n.webhook ? String(n.webhook).replace(/^(https:\/\/[^/]+\/).*$/, "$1…") : "", pagerduty: hint(n.pagerduty), escalateNtfy: hint(e.ntfy), escalateWebhook: e.webhook ? String(e.webhook).replace(/^(https:\/\/[^/]+\/).*$/, "$1…") : "", telegram: t.token ? "chat " + t.chat : "" } }; };
  // Reads are open on localhost, so what they show is trimmed: no raw log lines, and config values
  // whose key looks secret are masked (WisprOps keeps the real value internally to restore it).
  const SECRETISH = /secret|token|passw|pwd|api[_-]?key|private|credential|auth[_-]?key|dsn|connection[_-]?string/i;
  const mask = (c) => (c && typeof c === "object" ? Object.fromEntries(Object.entries(c).map(([k, v]) => [k, SECRETISH.test(k) ? "••••" : v])) : c);
  const publicState = () => {
    const S = agent.state;
    const svc = Object.fromEntries(Object.entries(S.svc).map(([k, r]) => [k, Object.assign({}, r, { golden: r.golden ? Object.assign({}, r.golden, { logBase: undefined, config: mask(r.golden.config) }) : r.golden })]));
    return Object.assign({}, S, { svc });
  };
  const view = () => ({ wisprops: true, mode: "live", state: publicState(), services: services(), stats: agent.stats(), handoff: agent.handoff(), settings: agent.settings,
    channels: channels(), remote: { hosts: cfg.access.hosts }, alerts: { enabled: true, token: !!cfg.alerts.token }, pr: { push: !!cfg.pr.push } });

  let busy = false;
  const timer = setInterval(async () => { if (busy) return; busy = true; try { await agent.tick(); } catch (e) { console.error("[tick]", e); } busy = false; }, cfg.settings.intervalMs);
  agent.tick().catch((e) => console.error("[tick]", e));

  // morning handoff, once a day
  let lastHandoff = null;
  const hoTimer = setInterval(() => {
    const d = new Date(), key = d.toDateString();
    if (d.getHours() === Number(cfg.settings.handoffHour) && lastHandoff !== key) { lastHandoff = key; const m = { kind: "handoff", title: "WisprOps morning handoff", body: agent.handoff() }; Promise.all([notifyOut(m), tg ? tg.notify(m) : null]).catch(() => {}); }
  }, 60000);

  const hostOf = (h) => String(h || "").toLowerCase().replace(/:\d+$/, "");
  const isLocalHost = (h) => /^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i.test(String(h || ""));
  const proxied = (req) => ["x-forwarded-for", "forwarded", "x-forwarded-host", "x-real-ip", "cf-connecting-ip", "true-client-ip", "x-forwarded-proto"].some((k) => req.headers[k] != null);
  const isLoopback = (a) => /^(?:127\.|::1$|::ffff:127\.)/.test(String(a || ""));
  const isLocal = (req) => isLocalHost(req.headers.host) && !proxied(req) && (loopbackBind || isLoopback(req.socket.remoteAddress));
  const okHost = (h) => isLocalHost(h) || cfg.access.hosts.includes(hostOf(h));
  const cookieName = "wisprops_session";
  const cookieOf = (req) => { const m = String(req.headers.cookie || "").match(new RegExp("(?:^|;\\s*)" + cookieName + "=([a-f0-9]{64})")); return m ? m[1] : ""; };
  const keys = () => [secret].concat(cfg.access.token ? [cfg.access.token] : []);
  /** A signed-in console (cookie) or a client holding the key or access token (bearer). */
  const session = (req) => {
    const bearer = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "");
    if (bearer && keys().some((k) => safeEq(bearer, k))) return true;
    const c = cookieOf(req);
    return !!c && keys().some((k) => safeEq(c, sha("session:" + k)));
  };
  const authed = (req) => isLocal(req) || session(req);
  const sameOrigin = (req) => { const o = req.headers.origin; if (!o) return true; try { return new URL(o).host.toLowerCase() === String(req.headers.host || "").toLowerCase(); } catch (e) { return false; } };
  const send = (res, code, body, type, extra) => {
    res.writeHead(code, Object.assign({ "Content-Type": type || "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" }, extra || {}));
    res.end(typeof body === "string" ? body : JSON.stringify(body));
  };
  const readBody = (req, raw) => new Promise((resolve, reject) => { let b = ""; req.on("data", (c) => { b += c; if (b.length > 262144) { reject(new Error("too large")); req.destroy(); } }); req.on("end", () => { if (raw) return resolve(b); try { resolve(JSON.parse(b || "{}")); } catch (e) { reject(new Error("invalid JSON")); } }); });

  let doctorCache = null;
  // Failed sign-ins and alert tokens. Global, not per client: behind a tunnel every client looks the same.
  const fails = { login: [], alert: [], change: [] };
  const limited = (k) => { const t = Date.now(); fails[k] = fails[k].filter((x) => t - x < 15 * 60000); return fails[k].length >= 20; };
  const failed = (k) => { fails[k].push(Date.now()); if (fails[k].length > 100) fails[k].shift(); };

  async function handleAlert(req, res, url) {
    const t = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "") || url.searchParams.get("token") || "";
    if (cfg.alerts.token) {
      // Check the token first: a flood of bad tokens must never lock out your real monitoring.
      if (!t || !safeEq(t, cfg.alerts.token)) {
        if (limited("alert")) return send(res, 429, { error: "too many bad alert tokens; wait 15 minutes" });
        failed("alert"); return send(res, 401, { error: "bad alert token" });
      }
    } else if (!isLocal(req) || req.headers.origin) return send(res, 401, { error: "set alerts.token to accept alerts from elsewhere" });
    let body;
    try { body = JSON.parse((await readBody(req, true)) || "{}"); } catch (e) { return send(res, 400, { error: "invalid JSON" }); }
    const alerts = parseAlerts(body, url.searchParams.get("service"));
    const results = [];
    for (const a of alerts) results.push(Object.assign({ service: a.service }, await agent.alert(a.service, { source: a.source, summary: a.summary })));
    return send(res, 202, { received: alerts.length, results });
  }

  /** Change events from CI or a deploy script: {"service":"api","text":"deployed v43","by":"sam","kind":"deploy"}. */
  async function handleChange(req, res) {
    const t = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "");
    // Same rules as every other write: same-origin JSON, so another page on localhost can't forge "who changed it".
    if (!sameOrigin(req) || !/^application\/json/.test(req.headers["content-type"] || "")) return send(res, 403, { error: "same-origin JSON only" });
    const ok = (cfg.alerts.token && t && safeEq(t, cfg.alerts.token)) || session(req) || (isLocal(req) && !req.headers.origin && !cfg.alerts.token);
    if (!ok) { if (limited("change")) return send(res, 429, { error: "too many bad tokens; wait 15 minutes" }); failed("change"); return send(res, 401, { error: "send alerts.token as a Bearer token" }); }
    let b;
    try { b = JSON.parse((await readBody(req, true)) || "{}"); } catch (e) { return send(res, 400, { error: "invalid JSON" }); }
    if (!b || typeof b !== "object" || !b.service || !b.text) return send(res, 400, { error: "need service and text" });
    const r = await agent.reportChange(String(b.service).slice(0, 100), { text: String(b.text), by: b.by ? String(b.by) : null, kind: String(b.kind || "deploy"), source: String(b.source || "reported"), key: b.key, version: b.version });
    return send(res, r.ok ? 202 : 404, r);
  }

  /** Add a service from a sentence (or flags) without editing JSON. Paths stay inside the config folder. */
  async function addService(body) {
    let id, flags;
    if (body.text) { const d = describeService(String(body.text).slice(0, 1000)); if (!d.ok) return { ok: false, error: d.warnings.join(" ") }; id = d.id; flags = d.flags; }
    else { id = String(body.id || ""); flags = body.flags && typeof body.flags === "object" ? body.flags : {}; }
    const allowed = ["health", "docker", "compose", "pm2", "systemd", "kubernetes", "namespace", "watch", "logs", "git", "name"];
    flags = Object.fromEntries(Object.entries(flags).filter(([k, v]) => allowed.includes(k) && typeof v === "string" && v.length < 300));
    for (const k of ["watch", "logs", "git"]) if (flags[k] && !insideBase(flags[k], cfgFile)) return { ok: false, error: `${k} must be a path inside ${baseDir}, and not WisprOps's own files` };
    if (!/^[\w.-]{1,60}$/.test(id)) return { ok: false, error: "service id must be letters, digits, dot, dash or underscore" };
    if (cfg.services.some((s) => s.id === id)) return { ok: false, error: `a service called ${id} already exists` };
    const raw = JSON.parse(fs.readFileSync(cfgFile, "utf8"));
    raw.services = raw.services || [];
    let n;
    try { raw.services.push(serviceFromFlags(id, flags)); n = normalize(JSON.parse(JSON.stringify(raw))); } catch (e) { return { ok: false, error: e.message }; }
    const tmp = cfgFile + ".wisprops-tmp"; fs.writeFileSync(tmp, JSON.stringify(raw, null, 2) + "\n"); fs.renameSync(tmp, cfgFile);
    const svc = n.services.find((s) => s.id === id);
    cfg.services.push(svc);
    broadcast("state");
    return { ok: true, id, can: svc.can };
  }

  const server = http.createServer(async (req, res) => {
    try {
      if (!okHost(req.headers.host)) return send(res, 403, { error: "bad host" });
      const url = new URL(req.url, "http://localhost");
      if (url.pathname === "/api/alert" && req.method === "POST") return await handleAlert(req, res, url);
      if (url.pathname === "/api/change" && req.method === "POST") return await handleChange(req, res);
      if (url.pathname === "/login") {
        if (limited("login")) return send(res, 429, "Too many attempts. Wait 15 minutes.", "text/plain; charset=utf-8");
        const t = url.searchParams.get("key") || url.searchParams.get("token") || "";
        // The console key signs in on this machine only; the access token works through your tunnel.
        const k = isLocal(req) && t && safeEq(t, secret) ? secret : cfg.access.token && t && safeEq(t, cfg.access.token) ? cfg.access.token : null;
        if (!k) { failed("login"); return send(res, 401, "Wrong or missing key. On this machine, open the link printed by `wisprops start` (or run `wisprops open`). From your phone, use `wisprops token --show`.", "text/plain; charset=utf-8"); }
        const secure = /https/.test(String(req.headers["x-forwarded-proto"] || "")) || !isLocal(req);
        return send(res, 302, "", "text/plain", { Location: "/", "Set-Cookie": `${cookieName}=${sha("session:" + k)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000${secure ? "; Secure" : ""}` });
      }
      if (!authed(req)) {
        if (url.pathname.startsWith("/api/")) return send(res, 401, { error: "sign in with your WisprOps token" });
        return send(res, 401, `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WisprOps</title><body style="font:16px system-ui;padding:24px;max-width:520px;margin:auto;background:#0f1013;color:#eceaf2"><h1 style="font-weight:500">WisprOps</h1><p>This console is protected. Open the sign-in link from <code>wisprops token --show</code> once on this device.</p></body>`, "text/html; charset=utf-8");
      }
      if (req.method === "POST") {
        if (!sameOrigin(req) || !/^application\/json/.test(req.headers["content-type"] || "")) return send(res, 403, { error: "same-origin JSON only" });
        const notesOnly = url.pathname === "/api/note" || url.pathname === "/api/runbook";
        if (!notesOnly && !session(req)) return send(res, 401, { error: "sign in: open the link printed by `wisprops start` (or run `wisprops open`)", signIn: true });
        const body = await readBody(req);
        const meta = body.meta && typeof body.meta === "object" ? { via: String(body.meta.via || "").slice(0, 10), insert: Math.max(0, Number(body.meta.insert) || 0), keys: Math.max(0, Number(body.meta.keys) || 0), duringReadback: !!body.meta.duringReadback } : {};
        // Only the console's own input tracking may claim a Wispr burst. The demo script, MCP and buttons never can.
        if (!["wispr", "typed"].includes(meta.via)) { meta.via = meta.via === "button" ? "button" : "typed"; meta.insert = 0; }
        const text = String(body.text || "").slice(0, 2000);
        switch (url.pathname) {
          case "/api/command": return send(res, 200, await agent.command(text, meta, body.id ? String(body.id) : null));
          case "/api/talk": return send(res, 200, await agent.command(text, meta, String(body.id || "")));
          case "/api/note": return send(res, 200, { ok: agent.addNote(String(body.id), text, String(body.source || "note").slice(0, 40)) });
          case "/api/ack": return send(res, 200, { ok: await agent.acknowledge(String(body.id), "console button") });
          case "/api/runbook":
            // Anyone on this machine (MCP included) may add a note. Removing one, or a note becoming an
            // enforced rule, needs a signed-in console: a rule changes what WisprOps will do.
            if (body.remove) return session(req) ? send(res, 200, { ok: agent.removeRunbook(String(body.serviceId), String(body.remove)) }) : send(res, 401, { error: "sign in to remove notes", signIn: true });
            return send(res, 200, { ok: !!agent.addRunbook(String(body.serviceId), text, meta.via === "wispr" ? "dictated with Wispr Flow" : body.source ? String(body.source).slice(0, 60) : "typed", { rules: session(req) && !body.source }) });
          case "/api/snooze": return send(res, 200, { ok: await agent.snooze(String(body.serviceId), Math.min(Math.max(Number(body.minutes) || 0, 0), 1440), "console button") });
          case "/api/orders/pause": return send(res, 200, { ok: await agent.setOrdersPaused(!!body.paused, "console button") });
          case "/api/revoke": return send(res, 200, { ok: await agent.revokeOrder(String(body.id), "console button") });
          // Freezing is always allowed; unfreezing has to be said with Wispr Flow in the Talk box.
          case "/api/freeze": return send(res, 200, { ok: await agent.freeze("console button") });
          case "/api/test-page": {
            // A harmless page to every channel, so you know it reaches you before 3 AM does.
            const m = { kind: "test", title: "WisprOps test page", body: "If you can read this, pages reach you here. Nothing is broken. To answer a real one, open the console and hold your Wispr Flow key." };
            const [out, tgr] = await Promise.all([notifyOut(m), tg ? tg.notify(m) : null]);
            const results = (out || []).concat(tg ? [{ channel: "Telegram", ok: !!tgr }] : []);
            broadcast("page", Object.assign({}, m, { test: true }));
            return send(res, 200, { ok: results.some((r) => r.ok), results });
          }
          case "/api/settings": { const r = applySettings(body); return send(res, r.ok ? 200 : 400, r); }
          case "/api/services": { const r = await addService(body); return send(res, r.ok ? 200 : 400, r); }
          case "/api/doctor":
            if (!doctorCache || Date.now() - doctorCache.at > 10000) doctorCache = { at: Date.now(), checks: await doctor(cfg, baseDir) };
            return send(res, 200, doctorCache);
          default: return send(res, 404, { error: "not found" });
        }
      }
      switch (url.pathname) {
        case "/api/state": return send(res, 200, Object.assign(view(), { session: session(req) }));
        case "/api/postmortem": { const i = agent.state.incidents.find((x) => x.id === url.searchParams.get("id")); return i ? send(res, 200, agent.postmortem(i), "text/markdown; charset=utf-8") : send(res, 404, "no such incident", "text/plain"); }
        case "/api/handoff": return send(res, 200, agent.handoff(), "text/plain; charset=utf-8");
        case "/api/certificate": { const c = agent.certificate(url.searchParams.get("id")); return c ? send(res, 200, c, "text/markdown; charset=utf-8") : send(res, 404, "no such receipt", "text/plain"); }
        case "/api/export": {
          const rows = auditRows(agent.state);
          return url.searchParams.get("format") === "csv" ? send(res, 200, toCSV(rows), "text/csv; charset=utf-8", { "Content-Disposition": 'attachment; filename="wisprops-audit.csv"' })
            : send(res, 200, JSON.stringify(rows, null, 2), "application/json", { "Content-Disposition": 'attachment; filename="wisprops-audit.json"' });
        }

        case "/api/events": {
          res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store", Connection: "keep-alive" });
          res.write("event: state\ndata: {}\n\n");
          clients.add(res); req.on("close", () => clients.delete(res));
          return;
        }
        default: break;
      }
      // "/" is the console on your own WisprOps. The project website lives in site/ and is hosted separately.
      const p = url.pathname === "/" ? "/console.html" : url.pathname;
      if (!STATIC.has(p)) return send(res, 404, "not found", "text/plain");
      const file = path.join(ROOT, p);
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer" });
      fs.createReadStream(file).pipe(res);
    } catch (e) {
      send(res, /JSON|too large/.test(e.message) ? 400 : 500, { error: e.message });
    }
  });
  const ping = setInterval(() => { for (const res of clients) res.write(": ping\n\n"); }, 20000);
  server.listen(port, bind, () => {
    if (opts.quiet) return;
    console.log(cfg.services.length ? `WisprOps is watching ${cfg.services.length} service${cfg.services.length === 1 ? "" : "s"}: ${cfg.services.map((s) => s.name).join(", ")}` : "WisprOps is running, with nothing to watch yet. Open the console and add your first service (you can describe it out loud).");
    console.log(`Console: ${signInUrl}`);
    console.log("         (open this link once to sign this browser in; then answer pages there with Wispr Flow)");
    if (tg) console.log("Pages also go to your Telegram chat. Replies there can acknowledge, snooze or freeze, never approve.");
    if (cfg.notify.ntfy) console.log(`Pages go to ntfy topic "${cfg.notify.ntfy}". Subscribe in the ntfy app on your phone.`);
    if (cfg.access.hosts.length) console.log(`Remote access: ${cfg.access.hosts.join(", ")} (token required). Sign-in link: wisprops token --show`);
  });
  return {
    server, agent, cfg, signInUrl, secret,
    stop: () => { if (tg) tg.stop(); clearInterval(timer); clearInterval(hoTimer); clearInterval(ping); clearTimeout(saveT); try { writeState(agent.state); } catch (e) { /* best effort */ } for (const r of clients) r.end(); server.close(); },
  };
}

module.exports = { start, loadConfig };
