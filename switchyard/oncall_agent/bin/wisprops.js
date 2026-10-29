#!/usr/bin/env node
/* wisprops CLI */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn, execFileSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");
const argv = process.argv.slice(2);
const cmd = argv[0] || "help";
const flags = {};
const pos = [];
for (let k = 1; k < argv.length; k++) {
  const a = argv[k];
  if (a.startsWith("--")) { const [key, inline] = a.slice(2).split("="); if (inline != null) flags[key] = inline; else if (argv[k + 1] && !argv[k + 1].startsWith("--")) flags[key] = argv[++k]; else flags[key] = true; }
  else pos.push(a);
}
const cfgPath = () => path.resolve(flags.config || "wisprops.config.json");
const die = (msg, code) => { console.error(msg); process.exit(code || 1); };
const readCfgRaw = () => { try { return JSON.parse(fs.readFileSync(cfgPath(), "utf8")); } catch (e) { die(`Cannot read ${cfgPath()}. Run "wisprops init" first.`); } };
const writeCfgRaw = (c) => fs.writeFileSync(cfgPath(), JSON.stringify(c, null, 2) + "\n");

const BASE = {
  port: 7300,
  publicUrl: "",
  settings: { intervalMs: 5000, failAfter: 2, goldenAfter: 3, verifyOk: 3, verifyWindowMs: 60000, allowTyped: false, sayItBack: true, handoffHour: 7, escalateAfterMin: 15, maxOrderDays: 30, maxOrderUses: 10 },
  notify: { ntfy: "", webhook: "", pagerduty: "", escalate: { ntfy: "", webhook: "" } },
  access: { token: "", hosts: [] },
  alerts: { token: "" },
  pr: { push: false },
  services: [],
};

function help() {
  console.log(`wisprops: an on-call agent you answer with Wispr Flow

Set up
  wisprops init [service flags]           write wisprops.config.json (optionally with a first service)
  wisprops add <id> --health <url> [...]  add a service
      --docker <container> | --compose <service> | --pm2 <name> | --systemd <unit> | --kubernetes <deployment> [--namespace ns] [--container c]
      --watch <file.json|.env>   watch this config file for drift (nested JSON keys and .env are supported)
      --git <repo dir>           release = current git commit; postmortem PRs go to this repo
      --logs <file>              log file to quote as evidence
      --health tcp://host:port   databases, queues, anything with a port (no HTTP needed)
      --health-cmd "<cmd args>"  healthy when this command exits 0 (pg_isready, redis-cli ping, your script)
  wisprops add --say "<sentence>"         describe the service out loud (dictate it with Wispr Flow):
      "watch my api in docker container api, health check at localhost 3000 slash healthz, config in config slash api dot json"
  wisprops doctor                        check every service, file and command before 3 AM does
  wisprops token [--host <tunnel host>]   create an access token so you can answer pages from your phone
  wisprops token --show                   print the phone sign-in link

Run
  wisprops start                          watch your services; console on http://localhost:7300
  wisprops open                           print the sign-in link for this machine's browser
  wisprops test-page                      send a harmless page to every channel, so you know it reaches you
  wisprops service [--write]              keep it running after reboot (systemd user unit, launchd, or Windows Task Scheduler)
  wisprops status                         one-line health of every service (from the running server)
  wisprops mcp [--url u]                  MCP server for Claude Code / Cursor (read + notes, never approvals)
  wisprops change "<what>" --service id   record a change from CI or a deploy script (who changed what)
  wisprops freeze                         stop every change now; only "unfreeze production" said with Wispr undoes it

Try it
  wisprops judge                          one full incident in the terminal, with every safety check
  wisprops demo                           a breakable demo service plus the console
  wisprops break config|deploy|crash      break the demo service (in a second terminal)

Answer pages by holding your Wispr Flow key in the console and saying the fix back, e.g. "approve fix one, db_pool back to 20".`);
}

function serverUrl() {
  let port = 7300;
  try { port = JSON.parse(fs.readFileSync(cfgPath(), "utf8")).port || port; } catch (e) { /* default */ }
  return (flags.url || process.env.WISPROPS_URL || `http://127.0.0.1:${port}`).replace(/\/+$/, "");
}

/** POST to the running server on this machine, signed with the console key (or a given token). */
async function localPost(p, body, token) {
  let key = token || "";
  if (!key) { try { key = fs.readFileSync(path.join(path.dirname(cfgPath()), ".wisprops", "secret"), "utf8").trim(); } catch (e) { /* not started yet */ } }
  try {
    const r = await fetch(serverUrl() + p, { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, key ? { Authorization: "Bearer " + key } : {}), body: JSON.stringify(body) });
    return await r.json();
  } catch (e) { return { ok: false, error: `WisprOps is not running at ${serverUrl()}` }; }
}

async function main() {
  switch (cmd) {
    case "init": {
      const f = cfgPath();
      if (fs.existsSync(f)) die(`${path.basename(f)} already exists. Use "wisprops add" to add a service.`);
      const c = JSON.parse(JSON.stringify(BASE));
      if (pos[0] || flags.health) {
        const { serviceFromFlags } = require("../server/config.js");
        c.services.push(serviceFromFlags(pos[0] || "api", Object.assign({ health: "http://localhost:3000/healthz" }, flags)));
      } else {
        c.services.push({ id: "api", name: "api", health: { url: "http://localhost:3000/healthz", timeoutMs: 3000, expect: 200 }, config: { file: "./config/api.json", source: "config/api.json" }, preset: { type: "docker", container: "api" }, logs: "./logs/api.log", repo: "." });
      }
      writeCfgRaw(c);
      console.log(`Wrote ${path.basename(f)}. Edit the service (or "wisprops add" more), then run:\n  wisprops doctor\n  wisprops start`);
      break;
    }
    case "add": {
      if (flags.say && flags.say !== true) {
        // Describe the service in a sentence (dictate it with Wispr Flow): wisprops add --say "watch my api in docker container api, health check at localhost 3000 slash healthz"
        const d = require("../src/agent.js").describeService(String(flags.say));
        console.log(`Heard: ${d.heard.join(" · ") || "nothing I can use"}`);
        d.warnings.forEach((w) => console.log("  ! " + w));
        if (!d.ok) die("Not added.");
        Object.assign(flags, d.flags);
        pos[0] = pos[0] || d.id;
        console.log(`Same as: ${d.cli}`);
      }
      const id = pos[0];
      if (!id || (!flags.health && !flags["health-cmd"])) die("usage: wisprops add <id> --health http://localhost:3000/healthz [--docker api] [--watch ./config.json] [--git .]", 2);
      const { serviceFromFlags, normalize } = require("../server/config.js");
      const c = fs.existsSync(cfgPath()) ? readCfgRaw() : JSON.parse(JSON.stringify(BASE));
      if (c.services.some((s) => s.id === id)) die(`service ${id} already exists in ${path.basename(cfgPath())}`);
      const svc = serviceFromFlags(id, flags);
      c.services.push(svc);
      try { normalize(JSON.parse(JSON.stringify(c))); } catch (e) { die("Not added: " + e.message); }
      writeCfgRaw(c);
      const norm = normalize(JSON.parse(JSON.stringify(c))).services.find((s) => s.id === id);
      const can = [norm.can.config && "config drift", norm.can.rollback && "bad deploy (rollback)", norm.can.restart && "dead process (restart)"].filter(Boolean);
      console.log(`Added ${id}. WisprOps can fix: ${can.length ? can.join(", ") : "nothing yet (it will escalate every failure to you)"}.`);
      if (!norm.can.rollback && norm.release) console.log(`  To enable rollbacks, add "rollback": ["your-deploy-command", "{to}"] to ${id}.`);
      console.log("Run \"wisprops doctor\" to check it, then restart \"wisprops start\".");
      break;
    }
    case "doctor": {
      const { loadConfig } = require("../server/config.js");
      const { doctor } = require("../server/doctor.js");
      let cfg;
      try { cfg = loadConfig(cfgPath()); } catch (e) { die("✖ " + e.message); }
      const checks = await doctor(cfg, path.dirname(cfgPath()));
      const icon = { ok: "✓", warn: "!", fail: "✖", info: "·" };
      let last = null;
      for (const c of checks) {
        if (c.service !== last) { console.log(c.service === "*" ? "\nWisprOps" : `\n${c.service}`); last = c.service; }
        console.log(`  ${icon[c.level]} ${c.check.padEnd(10)} ${c.text}`);
      }
      const fails = checks.filter((c) => c.level === "fail").length;
      console.log(fails ? `\n${fails} problem${fails === 1 ? "" : "s"} to fix.` : "\nReady. Start it with: wisprops start");
      process.exit(fails ? 1 : 0);
    }
    case "token": {
      const c = readCfgRaw();
      c.access = c.access || { token: "", hosts: [] };
      if (flags.show) {
        if (!c.access.token) die("No token yet. Run: wisprops token --host <your tunnel hostname>");
        const host = c.access.hosts && c.access.hosts[0];
        const tok = /^\$/.test(c.access.token) ? "<value of " + c.access.token + ">" : c.access.token;
        console.log(host ? `Open this once on your phone:\n  https://${host}/login?token=${tok}` : `Token: ${tok}\nAdd your tunnel hostname with: wisprops token --host <hostname>`);
        break;
      }
      if (!c.access.token || flags.rotate) c.access.token = crypto.randomBytes(24).toString("base64url");
      if (flags.host && flags.host !== true) {
        const h = String(flags.host).replace(/^https?:\/\//, "").replace(/\/.*$/, "").toLowerCase();
        c.access.hosts = Array.from(new Set((c.access.hosts || []).concat([h])));
        if (!c.publicUrl) c.publicUrl = `https://${h}/`;
      }
      writeCfgRaw(c);
      console.log(`Access token saved in ${path.basename(cfgPath())}${flags.rotate ? " (rotated; old sessions are signed out)" : ""}.`);
      console.log(c.access.hosts && c.access.hosts.length ? `Remote hosts: ${c.access.hosts.join(", ")}. Restart WisprOps, then run "wisprops token --show" for the phone link.` : "Next: put a tunnel in front of port " + (c.port || 7300) + " and run: wisprops token --host <its hostname>");
      break;
    }
    case "status": {
      try {
        const v = await (await fetch(serverUrl() + "/api/state")).json();
        for (const s of v.services) { const r = v.state.svc[s.id] || {}; console.log(`${r.status === "ok" ? "✓" : "✖"} ${s.name.padEnd(20)} ${(r.status || "unknown").padEnd(8)} ${s.snoozedUntil ? "snoozed until " + new Date(s.snoozedUntil).toLocaleTimeString() : ""}`); }
        const open = v.state.incidents.filter((i) => !["resolved", "recovered on its own", "handed back", "stood down"].includes(i.status));
        console.log(open.length ? `\n${open.length} open: ${open.map((i) => `${i.id} ${i.service} (${i.status})`).join(", ")}` : "\nNo open incidents.");
      } catch (e) { die(`WisprOps is not running at ${serverUrl()}. Start it with: wisprops start`); }
      break;
    }
    case "open": {
      const f = path.join(path.dirname(cfgPath()), ".wisprops", "secret");
      let key = "";
      try { key = fs.readFileSync(f, "utf8").trim(); } catch (e) { die("No key yet. Start WisprOps once with: wisprops start"); }
      let port = 7300; try { port = readCfgRaw().port || port; } catch (e) { /* default */ }
      console.log(`Open this once in your browser to sign it in:\n  http://localhost:${port}/login?key=${key}`);
      break;
    }
    case "start":
      // First run with no config: create an empty one and let the console walk you through adding a service.
      if (!fs.existsSync(cfgPath())) { writeCfgRaw(JSON.parse(JSON.stringify(BASE))); console.log(`Created ${path.basename(cfgPath())} in ${path.dirname(cfgPath())}.`); }
      try { require("../server/server.js").start({ config: cfgPath(), port: flags.port, bind: flags.bind }); } catch (e) { die("✖ " + e.message); }
      break;
    case "test-page": {
      const r = await localPost("/api/test-page", {});
      if (r.error) die(`Could not send: ${r.error}`);
      if (!r.results || !r.results.length) die('No channels configured. Set "notify" in wisprops.config.json (ntfy, webhook, pagerduty, telegram), restart, and try again.');
      for (const x of r.results) console.log(`${x.ok ? "✓" : "✖"} ${x.channel}${x.detail ? ": " + x.detail : ""}`);
      process.exit(r.ok ? 0 : 1);
    }
    case "service": {
      // Keep WisprOps running after logout and reboot. Prints (or with --write, writes) the file for this OS; never needs root.
      const node = process.execPath, cli = path.join(ROOT, "bin", "wisprops.js"), cfg = cfgPath(), dir = path.dirname(cfg);
      const q = (s) => JSON.stringify(s);
      if (process.platform === "linux") {
        const unit = `[Unit]\nDescription=WisprOps on-call agent\nAfter=network-online.target\n\n[Service]\nWorkingDirectory=${dir}\nExecStart=${q(node)} ${q(cli)} start --config ${q(cfg)}\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n`;
        const f = path.join(require("node:os").homedir(), ".config", "systemd", "user", "wisprops.service");
        if (flags.write) { fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, unit); console.log(`Wrote ${f}`); } else console.log(`# ${f}\n${unit}`);
        console.log("Then:\n  systemctl --user daemon-reload && systemctl --user enable --now wisprops\n  loginctl enable-linger \"$USER\"   # keep it running when you're logged out\n  journalctl --user -u wisprops -f  # its log (the sign-in link is printed there)");
      } else if (process.platform === "darwin") {
        const f = path.join(require("node:os").homedir(), "Library", "LaunchAgents", "dev.wisprops.plist");
        const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
        const plist = `<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n  <key>Label</key><string>dev.wisprops</string>\n  <key>ProgramArguments</key><array><string>${esc(node)}</string><string>${esc(cli)}</string><string>start</string><string>--config</string><string>${esc(cfg)}</string></array>\n  <key>WorkingDirectory</key><string>${esc(dir)}</string>\n  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>StandardOutPath</key><string>${esc(path.join(dir, ".wisprops", "wisprops.log"))}</string>\n  <key>StandardErrorPath</key><string>${esc(path.join(dir, ".wisprops", "wisprops.log"))}</string>\n</dict></plist>\n`;
        if (flags.write) { fs.mkdirSync(path.dirname(f), { recursive: true }); fs.mkdirSync(path.join(dir, ".wisprops"), { recursive: true }); fs.writeFileSync(f, plist); console.log(`Wrote ${f}`); } else console.log(`<!-- ${f} -->\n${plist}`);
        console.log(`Then:\n  launchctl load -w ${q(f)}\n  tail -f ${q(path.join(dir, ".wisprops", "wisprops.log"))}`);
      } else if (process.platform === "win32") {
        const tr = `"${node}" "${cli}" start --config "${cfg}"`;
        console.log(`Run this once in a terminal (it starts WisprOps whenever you sign in to Windows):\n  schtasks /Create /TN WisprOps /SC ONLOGON /RL LIMITED /F /TR "${tr.replace(/"/g, '\\"')}"\n  schtasks /Run /TN WisprOps\nRemove it with: schtasks /Delete /TN WisprOps /F`);
      } else die("Run \"wisprops start\" under your platform's service manager.");
      console.log('\nThe console sign-in link: "wisprops open".');
      break;
    }
    case "demo": {
      const demo = path.join(ROOT, "demo");
      execFileSync(process.execPath, [path.join(demo, "ctl.js"), "reset"], { stdio: "inherit" });
      const patient = spawn(process.execPath, [path.join(demo, "patient.js")], { stdio: "inherit" });
      const stop = () => { patient.kill(); process.exit(0); };
      process.on("SIGINT", stop); process.on("SIGTERM", stop);
      setTimeout(() => {
        require("../server/server.js").start({ config: path.join(demo, "wisprops.config.json"), port: flags.port });
        console.log("\nBreak it from another terminal:  node bin/wisprops.js break config   (or deploy, crash)");
      }, 400);
      break;
    }
    case "break": {
      const kind = pos[0];
      if (!["config", "deploy", "crash"].includes(kind)) die("usage: wisprops break config|deploy|crash", 2);
      execFileSync(process.execPath, [path.join(ROOT, "demo", "ctl.js"), "break", kind], { stdio: "inherit" });
      break;
    }
    case "mcp":
      require("../server/mcp.js").startMcp({ url: flags.url || process.env.WISPROPS_URL });
      break;
    case "judge": {
      const r = await require("./judge.js").judge({ color: flags["no-color"] ? false : undefined });
      process.exit(r.ok ? 0 : 1);
    }
    case "change": {
      // From CI or a deploy script: wisprops change "deployed v43" --service api --by sam
      const text = pos.join(" ");
      if (!text || !flags.service) die('usage: wisprops change "<what changed>" --service <id> [--by <who>] [--kind deploy|config|release|infra]', 2);
      const r = await localPost("/api/change", { service: flags.service, text, by: flags.by || process.env.GITHUB_ACTOR || process.env.USER || process.env.USERNAME || null, kind: flags.kind || "deploy", source: flags.source || (process.env.CI ? "CI" : "CLI") }, flags.token || process.env.WISPROPS_ALERT_TOKEN);
      console.log(r.ok ? `Recorded in the change ledger (${r.id}).` : `Not recorded: ${r.reason || r.error}`);
      process.exit(r.ok ? 0 : 1);
    }
    case "freeze": {
      const r = await localPost("/api/freeze", {});
      console.log(r.ok ? "Production is frozen. Nothing will be applied until someone says “unfreeze production” with Wispr Flow in the console." : `Could not freeze: ${r.error}`);
      process.exit(r.ok ? 0 : 1);
    }
    case "version": case "--version": case "-v":
      console.log(require("../package.json").version);
      break;
    default: help();
  }
}
process.on("unhandledRejection", (e) => console.error("[wisprops] unexpected error:", e && e.stack || e));
main().catch((e) => die(e.stack || e.message));
