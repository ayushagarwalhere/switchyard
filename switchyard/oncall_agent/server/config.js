/* Loads and checks wisprops.config.json, and expands service presets
   (docker, compose, pm2, systemd, kubernetes) into plain argument arrays.
   Anything written explicitly on a service wins over its preset. */
"use strict";
const fs = require("node:fs");

const NAME = /^(?!-)[\w.\-:/@]{1,200}$/;
const need = (p, k, type) => { if (!p[k] || !NAME.test(String(p[k]))) throw new Error(`preset ${type} needs a "${k}" (letters, digits, . _ - : / @)`); return String(p[k]); };

const PRESETS = {
  docker(p) {
    const c = need(p, "container", "docker");
    return { restart: ["docker", "restart", c], release: { command: ["docker", "inspect", "--format", "{{.Config.Image}}", c] }, logs: { command: ["docker", "logs", "--tail", "20", c] } };
  },
  compose(p) {
    const s = need(p, "service", "compose");
    const base = ["docker", "compose"];
    if (p.file) base.push("-f", need(p, "file", "compose"));
    if (p.project) base.push("-p", need(p, "project", "compose"));
    return { restart: [...base, "restart", s], logs: { command: [...base, "logs", "--no-color", "--tail", "20", s] } };
  },
  pm2(p) {
    const n = need(p, "name", "pm2");
    return { restart: ["pm2", "restart", n], logs: { command: ["pm2", "logs", n, "--lines", "20", "--nostream", "--raw"] } };
  },
  systemd(p) {
    const u = need(p, "unit", "systemd");
    return { restart: [...(p.sudo ? ["sudo", "-n"] : []), "systemctl", "restart", u], logs: { command: ["journalctl", "-u", u, "-n", "20", "--no-pager", "-o", "cat"] } };
  },
  kubernetes(p) {
    const d = "deployment/" + need(p, "deployment", "kubernetes");
    const ns = ["-n", p.namespace ? need(p, "namespace", "kubernetes") : "default"];
    const c = p.container ? need(p, "container", "kubernetes") : need(p, "deployment", "kubernetes");
    const ctx = p.context ? ["--context", need(p, "context", "kubernetes")] : [];
    return {
      restart: ["kubectl", ...ctx, "rollout", "restart", d, ...ns],
      release: { command: ["kubectl", ...ctx, "get", d, ...ns, "-o", `jsonpath={.spec.template.spec.containers[?(@.name=="${c}")].image}`] },
      rollback: ["kubectl", ...ctx, "set", "image", d, `${c}={to}`, ...ns],
      logs: { command: ["kubectl", ...ctx, "logs", d, ...ns, "--tail", "20"] },
    };
  },
};
PRESETS.k8s = PRESETS.kubernetes;

/** "$NAME" or "${NAME}" in secrets resolves from the environment, so tokens stay out of the file. */
function fromEnv(v) {
  if (typeof v !== "string") return v;
  const m = v.match(/^\$\{?([A-Z_][A-Z0-9_]*)\}?$/i);
  return m ? process.env[m[1]] || "" : v;
}

function normalize(cfg) {
  if (!cfg || typeof cfg !== "object") throw new Error("config must be a JSON object");
  // No services yet is fine: the console opens on "add your first service".
  if (cfg.services == null) cfg.services = [];
  if (!Array.isArray(cfg.services)) throw new Error("services must be a list");
  cfg.settings = Object.assign({ intervalMs: 5000, failAfter: 2, goldenAfter: 3, verifyOk: 3, verifyWindowMs: 60000, allowTyped: false, sayItBack: true, handoffHour: 7, escalateAfterMin: 15, maxOrderDays: 30, maxOrderUses: 10 }, cfg.settings || {});
  const s = cfg.settings;
  if (!(s.intervalMs >= 200)) throw new Error("settings.intervalMs must be at least 200");
  for (const k of ["failAfter", "goldenAfter", "verifyOk"]) if (!(s[k] >= 1)) throw new Error(`settings.${k} must be 1 or more`);
  cfg.notify = cfg.notify || {};
  for (const k of ["ntfy", "ntfyServer", "ntfyToken", "webhook", "pagerduty"]) cfg.notify[k] = fromEnv(cfg.notify[k]);
  if (cfg.notify.escalate) for (const k of ["ntfy", "webhook"]) cfg.notify.escalate[k] = fromEnv(cfg.notify.escalate[k]);
  if (cfg.notify.telegram) {
    const t = cfg.notify.telegram;
    t.token = fromEnv(t.token); t.chat = fromEnv(t.chat != null ? String(t.chat) : "");
    if (t.token && !/^\d+:[\w-]{20,}$/.test(t.token)) throw new Error("notify.telegram.token does not look like a bot token (123456:ABC…)");
    if (t.token && !/^-?\d+$/.test(t.chat)) throw new Error("notify.telegram.chat must be your numeric chat id");
  }
  cfg.access = cfg.access || {};
  cfg.access.token = fromEnv(cfg.access.token);
  cfg.access.hosts = (cfg.access.hosts || []).map(String);
  if (cfg.access.hosts.length && (!cfg.access.token || cfg.access.token.length < 16)) throw new Error("access.hosts is set, so access.token must be at least 16 characters (run: wisprops token)");
  cfg.alerts = cfg.alerts || {};
  cfg.alerts.token = fromEnv(cfg.alerts.token);
  if (cfg.alerts.token && cfg.alerts.token.length < 16) throw new Error("alerts.token must be at least 16 characters");
  cfg.pr = cfg.pr || {};

  const ids = new Set();
  cfg.services = cfg.services.map((raw) => {
    const svc = Object.assign({}, raw);
    if (!svc.id || !/^[\w.-]+$/.test(svc.id)) throw new Error(`service id "${svc.id}" must be letters, digits, dot, dash or underscore`);
    if (ids.has(svc.id)) throw new Error(`duplicate service id ${svc.id}`);
    ids.add(svc.id);
    if (svc.preset) {
      const type = String(svc.preset.type || "").toLowerCase();
      if (!PRESETS[type]) throw new Error(`service ${svc.id}: unknown preset "${svc.preset.type}" (docker, compose, pm2, systemd, kubernetes)`);
      const exp = PRESETS[type](svc.preset);
      for (const [k, v] of Object.entries(exp)) if (svc[k] == null) svc[k] = v;
      svc.presetLabel = type;
    }
    svc.name = svc.name || svc.id;
    if (typeof svc.health === "string") svc.health = { url: svc.health };
    // Three kinds of health check: an HTTP URL, a TCP port (databases, queues), or a command that exits 0.
    const h = svc.health || {};
    if (h.command != null) {
      if (!Array.isArray(h.command) || !h.command.length || !h.command.every((x) => typeof x === "string")) throw new Error(`service ${svc.id}: health.command must be a non-empty array of strings (it runs without a shell)`);
    } else if (/^tcp:\/\/[^/\s:]+:\d{1,5}\/?$/i.test(h.url || "")) {
      h.url = h.url.replace(/\/$/, "");
    } else if (!/^https?:\/\//.test(h.url || "")) throw new Error(`service ${svc.id} needs a health check: an http(s):// URL, tcp://host:port, or {"command": [...]}`);
    for (const k of ["restart", "rollback", "reload"]) if (svc[k] != null && (!Array.isArray(svc[k]) || !svc[k].length || !svc[k].every((x) => typeof x === "string"))) throw new Error(`service ${svc.id}: ${k} must be a non-empty array of strings (it runs without a shell)`);
    if (svc.rollback && !svc.rollback.some((x) => x.includes("{to}"))) throw new Error(`service ${svc.id}: rollback must contain {to}, the release to go back to`);
    if (svc.release && svc.release.command && (!Array.isArray(svc.release.command) || !svc.release.command.every((x) => typeof x === "string"))) throw new Error(`service ${svc.id}: release.command must be an array of strings`);
    if (svc.logs && typeof svc.logs === "object" && !Array.isArray(svc.logs) && !Array.isArray(svc.logs.command)) throw new Error(`service ${svc.id}: logs must be a file path or {"command": [...]}`);
    if (svc.config && svc.config.file) svc.configLabel = svc.config.file;
    svc.healthLabel = h.command ? "runs " + h.command.join(" ") : /^tcp:/i.test(h.url) ? "TCP " + h.url.replace(/^tcp:\/\//i, "") : (h.method || "GET") + " " + h.url;
    svc.can = { config: !!(svc.config && svc.config.file), rollback: !!(svc.release && svc.rollback), restart: !!svc.restart };
    return svc;
  });
  return cfg;
}

function loadConfig(file) {
  let text;
  try { text = fs.readFileSync(file, "utf8"); } catch (e) { throw new Error(`cannot read ${file}. Run "wisprops init" to create one.`); }
  let cfg;
  try { cfg = JSON.parse(text); } catch (e) { throw new Error(`${file} is not valid JSON: ${e.message}`); }
  return normalize(cfg);
}

/** A service entry from CLI flags, e.g. wisprops add api --health http://localhost:3000/healthz --docker api */
function serviceFromFlags(id, f) {
  const svc = { id, name: f.name || id, health: f["health-cmd"] ? { command: String(f["health-cmd"]).trim().split(/\s+/), timeoutMs: 10000 } : /^tcp:/i.test(f.health || "") ? { url: f.health, timeoutMs: 3000 } : { url: f.health || "", timeoutMs: 3000, expect: 200 } };
  const presets = ["docker", "compose", "pm2", "systemd", "kubernetes", "k8s"].filter((k) => f[k]);
  if (presets.length > 1) throw new Error("pick one of --docker, --compose, --pm2, --systemd, --kubernetes");
  if (presets.length) {
    const t = presets[0] === "k8s" ? "kubernetes" : presets[0];
    const key = { docker: "container", compose: "service", pm2: "name", systemd: "unit", kubernetes: "deployment" }[t];
    svc.preset = { type: t, [key]: f[presets[0]] };
    if (f.namespace) svc.preset.namespace = f.namespace;
    if (f.container && t === "kubernetes") svc.preset.container = f.container;
  }
  if (f.watch) svc.config = { file: f.watch, source: f.source || f.watch };
  if (f["release-file"]) svc.release = { file: f["release-file"] };
  if (f.git) { svc.release = svc.release || { command: ["git", "-C", f.git, "rev-parse", "--short", "HEAD"] }; svc.repo = f.git; }
  if (f.logs) svc.logs = f.logs;
  if (f.repo) svc.repo = f.repo;
  return svc;
}

/** Paths a service added from the console may name (config, logs, git): inside the folder that holds the config. */
function insideBase(p, cfgFile) {
  if (typeof p !== "string" || !p || p.length >= 300 || /^(?:[a-z]:|[\\/]|~)/i.test(p)) return false;
  const parts = p.split(/[\\/]/).filter((x) => x && x !== ".");
  if (parts.includes("..")) return false;
  // Never WisprOps's own secret, state or config, and no dotfiles except .env files and "." itself.
  if (parts.some((x) => /^\.wisprops$/i.test(x) || (x.startsWith(".") && !/^\.env([.\w-]*)?$/i.test(x)))) return false;
  if (cfgFile && parts.length && parts[parts.length - 1].toLowerCase() === require("node:path").basename(cfgFile).toLowerCase()) return false;
  return true;
}

module.exports = { loadConfig, normalize, PRESETS, serviceFromFlags, fromEnv, insideBase };
