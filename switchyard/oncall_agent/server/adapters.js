/* Real-world adapters: HTTP health checks, config files (JSON with nested keys, or .env),
   release files or commands, log tails (file or command), and the three allowlisted actions.
   Commands come only from wisprops.config.json and run without a shell. The only value ever
   substituted into a command is a release id that must match RELEASE_ID. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { execFile } = require("node:child_process");

// Image tags and git refs: letters, digits, . _ - : / @ +, never starting with "-" (no flag injection).
const RELEASE_ID = /^(?!-)[\w.\-:/@+]{1,200}$/;

function run(argv, opts) {
  return new Promise((resolve) => {
    if (!Array.isArray(argv) || !argv.length) return resolve({ ok: false, output: "no command configured" });
    execFile(argv[0], argv.slice(1), { timeout: (opts && opts.timeoutMs) || 60000, cwd: opts && opts.cwd, windowsHide: true, shell: false, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
      const out = (String(stdout || "") + String(stderr || "")).trim();
      resolve({ ok: !err, output: (err ? (out || err.message) : out).slice(-(opts && opts.keep || 600)), code: err ? err.code : 0 });
    });
  });
}

// ---------- config files ----------
const isEnvFile = (f) => /(^|[\\/])\.env([.\w-]*)?$|\.env$/i.test(f);

function flatten(obj, prefix, out) {
  out = out || {};
  for (const [k, v] of Object.entries(obj || {})) {
    const key = prefix ? prefix + "." + k : k;
    if (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length) flatten(v, key, out);
    else out[key] = v;
  }
  return out;
}
function setPath(obj, key, value) {
  if (Object.prototype.hasOwnProperty.call(obj, key) || !key.includes(".")) {
    if (value === undefined) delete obj[key]; else obj[key] = value;
    return;
  }
  const parts = key.split(".");
  let cur = obj;
  for (let k = 0; k < parts.length - 1; k++) {
    if (["__proto__", "constructor", "prototype"].includes(parts[k])) throw new Error("refusing to write " + key);
    if (!cur[parts[k]] || typeof cur[parts[k]] !== "object") cur[parts[k]] = {};
    cur = cur[parts[k]];
  }
  const last = parts[parts.length - 1];
  if (value === undefined) delete cur[last]; else cur[last] = value;
}

function parseEnv(text) {
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][\w.]*)\s*=\s*(.*?)\s*$/);
    if (!m) continue;
    let v = m[2];
    const q = v.match(/^(["'])(.*?)\1(?:\s+#.*)?$/);
    if (q) v = q[2];
    else v = v.replace(/\s+#.*$/, "");
    out[m[1]] = v;
  }
  return out;
}
function writeEnv(text, key, value) {
  const lines = text.split(/\r?\n/);
  const re = new RegExp("^\\s*(?:export\\s+)?" + key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*=");
  const k = lines.findIndex((l) => re.test(l));
  const v = value === undefined ? null : /[\s#"']/.test(String(value)) ? JSON.stringify(String(value)) : String(value);
  if (k >= 0) { if (v === null) lines.splice(k, 1); else lines[k] = lines[k].replace(/=.*$/, () => "=" + v); }
  else if (v !== null) { if (lines.length && lines[lines.length - 1] === "") lines.splice(lines.length - 1, 0, `${key}=${v}`); else lines.push(`${key}=${v}`); }
  return lines.join("\n");
}

function readConfigFile(f) {
  const text = fs.readFileSync(f, "utf8");
  return isEnvFile(f) ? parseEnv(text) : flatten(JSON.parse(text));
}
function writeConfigKey(f, key, value) {
  const text = fs.readFileSync(f, "utf8");
  let next;
  if (isEnvFile(f)) next = writeEnv(text, key, value);
  else { const j = JSON.parse(text); setPath(j, key, value); next = JSON.stringify(j, null, 2) + "\n"; }
  const tmp = f + ".wisprops-tmp";
  fs.writeFileSync(tmp, next);
  fs.renameSync(tmp, f);
}

function createAdapters(cfg, baseDir, hooks) {
  const abs = (p) => (p ? path.resolve(baseDir, p) : null);
  const subst = (argv, vars) => argv.map((a) => a.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? String(vars[k]) : m)));

  return {
    now: () => Date.now(),

    async probe(svc) {
      const h = svc.health || {};
      if (h.command) {
        // Command health check: healthy when it exits 0 (pg_isready, redis-cli ping, a script of yours).
        const t0 = Date.now();
        const r = await run(h.command, { cwd: baseDir, timeoutMs: h.timeoutMs || 10000 });
        const last = (r.output || "").split(/\r?\n/).filter(Boolean).pop() || "";
        return r.ok ? { ok: true, status: 0, ms: Date.now() - t0 } : { ok: false, ...(r.code === "ENOENT" || /ENOENT/.test(r.output) ? { error: `${h.command[0]} not found` } : { status: typeof r.code === "number" ? r.code : 1, detail: last.slice(0, 160) || "exited non-zero" }) };
      }
      if (/^tcp:/i.test(h.url || "")) {
        // TCP health check: healthy when the port accepts a connection (Postgres, Redis, MySQL, any socket).
        const m = h.url.match(/^tcp:\/\/([^/:]+):(\d+)/i);
        const t0 = Date.now();
        return await new Promise((resolve) => {
          const sock = require("node:net").connect({ host: m[1], port: Number(m[2]) });
          const done = (v) => { sock.destroy(); resolve(v); };
          sock.setTimeout(h.timeoutMs || 3000, () => done({ ok: false, error: `timeout after ${h.timeoutMs || 3000}ms` }));
          sock.once("connect", () => done({ ok: true, status: 0, ms: Date.now() - t0 }));
          sock.once("error", (e) => done({ ok: false, error: String(e.code || e.message) }));
        });
      }
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), h.timeoutMs || 3000);
      const t0 = Date.now();
      try {
        const res = await fetch(h.url, { signal: ctrl.signal, method: h.method || "GET", headers: Object.assign({ "user-agent": "wisprops-probe" }, h.headers || {}), redirect: "manual" });
        const ms = Date.now() - t0;
        const expect = h.expect || 200;
        let body = "";
        if (res.status !== expect || h.contains) { try { body = (await res.text()).slice(0, 4000); } catch (e) { /* body unreadable */ } }
        else { try { await res.body?.cancel(); } catch (e) { /* already closed */ } }
        const okStatus = res.status === expect;
        const okBody = !h.contains || body.includes(h.contains);
        const slow = h.maxMs && ms > h.maxMs;
        const ok = okStatus && okBody && !slow;
        let detail = "";
        if (!okStatus) detail = body.slice(0, 160).replace(/\s+/g, " ");
        else if (!okBody) detail = `response does not contain ${JSON.stringify(h.contains)}`;
        else if (slow) detail = `answered in ${ms}ms, over the ${h.maxMs}ms limit`;
        return { ok, status: res.status, ms, detail };
      } catch (e) {
        const code = (e.cause && (e.cause.code || (e.cause.errors && e.cause.errors[0] && e.cause.errors[0].code))) || (e.name === "AbortError" ? "timeout after " + (h.timeoutMs || 3000) + "ms" : e.message);
        return { ok: false, error: String(code) };
      } finally { clearTimeout(t); }
    },

    async readConfig(svc) {
      if (!svc.config || !svc.config.file) return null;
      try { return readConfigFile(abs(svc.config.file)); } catch (e) { return { __unreadable: e.code === "ENOENT" ? "file not found" : e.message.slice(0, 120) }; }
    },

    async version(svc) {
      const r = svc.release || {};
      if (r.file) { try { return fs.readFileSync(abs(r.file), "utf8").trim().split(/\s+/)[0] || null; } catch (e) { return null; } }
      if (r.command) { const out = await run(r.command, { cwd: baseDir, timeoutMs: 10000 }); return out.ok ? (out.output.trim().split(/\s+/)[0] || null) : null; }
      return null;
    },

    async logs(svc) {
      if (!svc.logs) return [];
      if (Array.isArray(svc.logs) || (svc.logs && svc.logs.command)) {
        const argv = Array.isArray(svc.logs) ? svc.logs : svc.logs.command;
        const r = await run(argv, { cwd: baseDir, timeoutMs: 8000, keep: 16000 });
        if (!r.ok && (r.code === "ENOENT" || /spawn \S+ ENOENT/.test(r.output))) return [];
        return r.output.split(/\r?\n/).filter(Boolean).slice(-40).map((l) => l.slice(0, 300));
      }
      try {
        const f = abs(svc.logs), size = fs.statSync(f).size, fd = fs.openSync(f, "r");
        const len = Math.min(size, 16384), buf = Buffer.alloc(len);
        fs.readSync(fd, buf, 0, len, size - len); fs.closeSync(fd);
        return buf.toString("utf8").split(/\r?\n/).filter(Boolean).slice(-40).map((l) => l.slice(0, 300));
      } catch (e) { return []; }
    },

    /** Who shipped a release, when it comes from git: the commit's author. Everything else comes from the change ledger. */
    async whoChanged(svc, what) {
      const r = svc.release || {};
      if (what.kind === "release" && what.version && RELEASE_ID.test(String(what.version)) && Array.isArray(r.command) && r.command[0] === "git") {
        const k = r.command.indexOf("-C"), dir = k >= 0 ? r.command[k + 1] : ".";
        const out = await run(["git", "-C", dir, "log", "-1", "--format=%an", String(what.version), "--"], { cwd: baseDir, timeoutMs: 5000 });
        return out.ok && out.output ? `${out.output.split(/\r?\n/)[0].slice(0, 60)} (git author)` : null;
      }
      return null;
    },

    async dryRun(svc, a) {
      if (a.kind === "restore_config") {
        const f = svc.config && abs(svc.config.file);
        if (!f) return { ok: false, text: "this service has no config file configured" };
        try { fs.accessSync(f, fs.constants.R_OK | fs.constants.W_OK); } catch (e) { return { ok: false, text: `${svc.config.file} is not writable by WisprOps` }; }
        let cur; try { cur = readConfigFile(f); } catch (e) { return { ok: false, text: `${svc.config.file} could not be parsed` }; }
        if (JSON.stringify(cur[a.key]) !== JSON.stringify(a.from)) return { ok: false, text: `${a.key} changed again since triage (now ${JSON.stringify(cur[a.key])})` };
        return { ok: true, text: `${svc.config.file}: 1 key would change (${a.key}); the file is writable and parses${svc.reload ? `; then: ${svc.reload.join(" ")}` : ""}.` };
      }
      if (a.kind === "rollback") {
        if (!RELEASE_ID.test(String(a.to))) return { ok: false, text: "release id has unexpected characters" };
        if (!svc.rollback) return { ok: false, text: "no rollback command configured for this service" };
        return { ok: true, text: `would run: ${subst(svc.rollback, { to: a.to }).join(" ")}` };
      }
      if (a.kind === "restart") {
        if (!svc.restart) return { ok: false, text: "no restart command configured for this service" };
        return { ok: true, text: `would run: ${svc.restart.join(" ")}` };
      }
      return { ok: false, text: "not an allowlisted action" };
    },

    async exec(svc, a) {
      if (a.kind === "restore_config") {
        try { writeConfigKey(abs(svc.config.file), a.key, a.value); } catch (e) { return { ok: false, output: e.message }; }
        let extra = "";
        if (svc.reload) { const r = await run(svc.reload, { cwd: baseDir }); if (!r.ok) return { ok: false, output: `Key restored, but the reload command failed: ${r.output}` }; extra = " Reloaded."; }
        return { ok: true, output: `${a.key} is now ${JSON.stringify(a.value)} in ${svc.config.file}.${extra}` };
      }
      if (a.kind === "rollback") {
        if (!RELEASE_ID.test(String(a.to))) return { ok: false, output: "release id rejected" };
        const r = await run(subst(svc.rollback, { to: a.to }), { cwd: baseDir, timeoutMs: (svc.timeoutMs || 120000) });
        return { ok: r.ok, output: r.ok ? `Rolled back to ${a.to}. ${r.output}`.trim() : r.output };
      }
      if (a.kind === "restart") {
        const r = await run(svc.restart, { cwd: baseDir, timeoutMs: (svc.timeoutMs || 120000) });
        return { ok: r.ok, output: r.ok ? `Restart command finished. ${r.output}`.trim() : r.output };
      }
      return { ok: false, output: "not an allowlisted action" };
    },

    digest: async (s) => crypto.createHash("sha256").update(s).digest("hex"),

    notify: async (n) => { if (hooks && hooks.notify) await hooks.notify(n); },

    openPR: async (inc, markdown) => (hooks && hooks.openPR ? hooks.openPR(inc, markdown) : { ok: false, text: "Pull requests are not configured." }),

    save: async (state) => { if (hooks && hooks.save) hooks.save(state); },
  };
}

module.exports = { createAdapters, run, RELEASE_ID, flatten, setPath, parseEnv, writeEnv, readConfigFile, writeConfigKey, isEnvFile };
