/* WisprOps simulation: a pretend production service with three real-world failure modes, and the
   adapters the agent needs to watch and fix it. Used by the browser demo and by the tests.
   Nothing here touches a real system. */
(function (root) {
  "use strict";

  const RELEASES = {
    v41: { ok: true, note: "stable" },
    v42: { ok: false, note: "TypeError: Cannot read properties of undefined (reading 'currency') at pricing.js:88" },
  };

  function createSim(opts) {
    opts = opts || {};
    const now = opts.now || (() => Date.now());
    const svcs = {
      "checkout-api": { up: true, bootIn: 0, version: "v41", config: { db_pool: 20, cache_ttl_s: 60, payments_timeout_ms: 3000 }, logs: [], by: {} },
      "auth": { up: true, bootIn: 0, version: "v17", config: { session_ttl_min: 60, rate_limit_rps: 50 }, logs: [], by: {} },
    };
    const config = {
      services: [
        { id: "checkout-api", name: "checkout-api", configLabel: "config/checkout.json", health: "GET /healthz" },
        { id: "auth", name: "auth", configLabel: "config/auth.json", health: "GET /healthz" },
      ],
      settings: Object.assign({ failAfter: 2, goldenAfter: 2, verifyOk: 3, verifyWindowMs: 20000, allowTyped: false }, opts.settings || {}),
    };
    const stamp = () => { const d = new Date(now()); return [d.getHours(), d.getMinutes(), d.getSeconds()].map((x) => String(x).padStart(2, "0")).join(":"); };
    const logLine = (id, t) => { const s = svcs[id]; s.logs.push(`${stamp()} ${t}`); if (s.logs.length > 60) s.logs.shift(); };
    let reqs = 0;
    // Healthy traffic, so the failure lines have something to stand out from.
    const traffic = (id) => { reqs += 1; if (reqs % 2) logLine(id, `INFO GET /api/${id === "auth" ? "session" : "cart"}/${1000 + (reqs % 97)} 200 ${18 + (reqs % 23)}ms`); };

    function health(id) {
      const s = svcs[id];
      if (!s.up) {
        if (s.bootIn > 0) { s.bootIn -= 1; if (s.bootIn === 0) { s.up = true; logLine(id, "INFO started, listening on :8080"); } }
        if (!s.up) return { ok: false, error: "connect ECONNREFUSED 10.0.3.14:8080" };
      }
      if (id === "checkout-api" && !(s.config.db_pool > 0)) { logLine(id, `WARN request ${4000 + (reqs++ % 900)} waited 3000ms for a db connection`); logLine(id, `ERROR db pool exhausted (db_pool=${s.config.db_pool}), request failed`); return { ok: false, status: 500, detail: "db pool exhausted" }; }
      if (id === "auth" && !(s.config.rate_limit_rps > 0)) { logLine(id, "ERROR rate limiter rejected every request (rate_limit_rps=0)"); return { ok: false, status: 429, detail: "rate limiter rejecting all requests" }; }
      const rel = RELEASES[s.version];
      if (rel && !rel.ok) { logLine(id, "ERROR " + rel.note); return { ok: false, status: 500, detail: rel.note.split(" at ")[0] }; }
      traffic(id);
      return { ok: true, status: 200, ms: 20 + Math.round(Math.random() * 30) };
    }

    const faults = {
      config: { label: "Someone sets db_pool to 0", svc: "checkout-api", by: "deploy-bot", run: () => { svcs["checkout-api"].config.db_pool = 0; svcs["checkout-api"].by.db_pool = "deploy-bot"; logLine("checkout-api", "INFO config reloaded by deploy-bot"); } },
      deploy: { label: "Release v42 goes out with a bug", svc: "checkout-api", by: "GitHub Actions (merge of #418 by sam)", run: () => { svcs["checkout-api"].version = "v42"; svcs["checkout-api"].by.__release = "GitHub Actions (merge of #418 by sam)"; logLine("checkout-api", "INFO deployed v42"); } },
      crash: { label: "The auth process dies", svc: "auth", run: () => { svcs.auth.up = false; svcs.auth.bootIn = 0; logLine("auth", "FATAL out of memory, process exited"); } },
      ratelimit: { label: "Someone sets auth rate_limit_rps to 0", svc: "auth", by: "ops-console (riya)", run: () => { svcs.auth.config.rate_limit_rps = 0; svcs.auth.by.rate_limit_rps = "ops-console (riya)"; logLine("auth", "INFO config reloaded by ops-console"); } },
      alert: { label: "Grafana alert: auth p95 latency over 2 s (health check still passes)", svc: "auth", alert: { source: "Grafana", summary: "HighLatency: auth p95 latency is 2.4 s (threshold 2 s) for 5 minutes" }, run: () => { logLine("auth", "WARN slow query on sessions table (1.9 s)"); } },
    };

    const adapters = {
      now,
      probe: async (svc) => health(svc.id),
      readConfig: async (svc) => JSON.parse(JSON.stringify(svcs[svc.id].config)),
      version: async (svc) => svcs[svc.id].version,
      logs: async (svc) => svcs[svc.id].logs.slice(-30),
      whoChanged: async (svc, what) => svcs[svc.id].by[what.kind === "release" ? "__release" : what.key] || null,
      dryRun: async (svc, a) => {
        const s = svcs[svc.id];
        if (a.kind === "restore_config") {
          if (!(a.key in s.config) && a.value === undefined) return { ok: false, text: `key ${a.key} does not exist` };
          return { ok: true, text: `${svc.configLabel}: 1 key would change (${a.key}). File is writable by the executor.` };
        }
        if (a.kind === "rollback") return RELEASES[a.to] ? { ok: true, text: `release ${a.to} is present in the artifact store and passed its checks when it shipped.` } : { ok: false, text: `release ${a.to} not found` };
        if (a.kind === "restart") return { ok: true, text: "the supervisor can restart this process; it has no other dependants." };
        return { ok: false, text: "unknown action" };
      },
      exec: async (svc, a) => {
        const s = svcs[svc.id];
        if (a.kind === "restore_config") { if (a.value === undefined) delete s.config[a.key]; else s.config[a.key] = a.value; logLine(svc.id, `INFO config ${a.key} set to ${JSON.stringify(a.value)} by wisprops-executor`); return { ok: true, output: `${a.key} is now ${JSON.stringify(a.value)}.` }; }
        if (a.kind === "rollback") { s.version = a.to; logLine(svc.id, `INFO rolled back to ${a.to} by wisprops-executor`); return { ok: true, output: `Now serving ${a.to}.` }; }
        if (a.kind === "restart") { s.up = false; s.bootIn = 1; logLine(svc.id, "INFO restart requested by wisprops-executor"); return { ok: true, output: "Process restarting." }; }
        return { ok: false, output: "unknown action" };
      },
      openPR: async (inc, md) => ({ ok: true, simulated: true, text: `Draft PR opened (simulated): “Postmortem ${inc.id}: ${inc.cause ? inc.cause.kind.replace("_", " ") : "incident"} on ${inc.service}”. It adds the postmortem and restores the source config. Nothing is merged.`, markdown: md }),
    };
    return { config, adapters, faults, svcs };
  }

  const api = { createSim, RELEASES };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.WisprOpsSim = api;
})(typeof window !== "undefined" ? window : globalThis);
