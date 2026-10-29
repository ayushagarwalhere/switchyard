/* wisprops doctor: checks a config the way the agent will use it, before 3 AM does.
   Probes each health URL, reads each config file, resolves each release, and confirms each
   command's program can be found. It never runs a restart or rollback. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createAdapters } = require("./adapters.js");

function findProgram(cmd, cwd) {
  if (!cmd) return { ok: false };
  if (/[\\/]/.test(cmd)) { const p = path.resolve(cwd, cmd); return { ok: fs.existsSync(p), where: p }; }
  const exts = process.platform === "win32" ? ["", ...(process.env.PATHEXT || ".EXE;.CMD;.BAT").toLowerCase().split(";")] : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    for (const ext of exts) {
      const p = path.join(dir, cmd + ext);
      try { if (fs.statSync(p).isFile()) return { ok: true, where: p, script: /\.(cmd|bat)$/i.test(p) }; } catch (e) { /* keep looking */ }
    }
  }
  return { ok: false };
}

async function doctor(cfg, baseDir) {
  const A = createAdapters(cfg, baseDir, {});
  const out = [];
  const add = (service, check, level, text) => out.push({ service, check, level, text });

  for (const svc of cfg.services) {
    const p = await A.probe(svc);
    add(svc.id, "health", p.ok ? "ok" : "warn", p.ok ? `${svc.healthLabel} → ${svc.health.command ? "exit 0" : /^tcp:/i.test(svc.health.url || "") ? "port open" : p.status} in ${p.ms}ms` : `${svc.healthLabel} → ${p.error || "HTTP " + p.status}${p.detail ? " (" + p.detail + ")" : ""}. WisprOps will page when it fails ${cfg.settings.failAfter} times in a row; is it running?`);

    if (svc.config && svc.config.file) {
      const c = await A.readConfig(svc);
      if (c && c.__unreadable) add(svc.id, "config", "fail", `${svc.config.file}: ${c.__unreadable}`);
      else {
        let writable = true; try { fs.accessSync(path.resolve(baseDir, svc.config.file), fs.constants.W_OK); } catch (e) { writable = false; }
        add(svc.id, "config", writable ? "ok" : "fail", `${svc.config.file}: ${Object.keys(c || {}).length} keys watched${writable ? ", writable" : ", NOT writable (WisprOps could not put a key back)"}`);
      }
    } else add(svc.id, "config", "info", "No config file. Config drift cannot be detected or fixed for this service.");

    if (svc.release) {
      const v = await A.version(svc);
      add(svc.id, "release", v ? "ok" : "fail", v ? `current release: ${v}` : "could not read the current release");
    } else add(svc.id, "release", "info", "No release source. Bad deploys cannot be detected or rolled back.");

    for (const k of ["restart", "rollback", "reload"]) {
      if (!svc[k]) { if (k !== "reload") add(svc.id, k, "info", k === "restart" ? "No restart command. A dead process will be escalated to you instead." : "No rollback command."); continue; }
      const prog = findProgram(svc[k][0], baseDir);
      if (!prog.ok) add(svc.id, k, "fail", `“${svc[k][0]}” was not found on PATH`);
      else if (prog.script && process.platform === "win32") add(svc.id, k, "fail", `“${svc[k][0]}” is a .cmd/.bat script; WisprOps runs commands without a shell, so call its .js with node instead (e.g. ["node", "…/pm2/bin/pm2", …])`);
      else add(svc.id, k, "ok", `${svc[k].join(" ")}`);
    }

    const logCmd = Array.isArray(svc.logs) ? svc.logs : svc.logs && svc.logs.command;
    if (logCmd && !findProgram(logCmd[0], baseDir).ok) add(svc.id, "logs", "warn", `“${logCmd[0]}” was not found on PATH, so no log lines will be quoted`);
    else if (svc.logs) {
      const lines = await A.logs(svc);
      add(svc.id, "logs", lines.length ? "ok" : "warn", lines.length ? `last line: ${lines[lines.length - 1].slice(0, 120)}` : "no log lines could be read");
    }
    const can = [svc.can.config && "config drift", svc.can.rollback && "bad deploy", svc.can.restart && "dead process"].filter(Boolean);
    add(svc.id, "covers", can.length ? "ok" : "warn", can.length ? `can fix: ${can.join(", ")}` : "nothing to fix with: every failure will be escalated to you");
  }

  const n = cfg.notify || {};
  add("*", "paging", n.ntfy || n.webhook || n.pagerduty ? "ok" : "warn", n.ntfy || n.webhook || n.pagerduty ? `pages go to ${[n.ntfy && "ntfy", n.webhook && "webhook", n.pagerduty && "PagerDuty"].filter(Boolean).join(", ")}` : "No paging channel: pages only appear in the open console. Set notify.ntfy for your phone.");
  if (cfg.settings.escalateAfterMin && !(n.escalate && (n.escalate.ntfy || n.escalate.webhook)) && !n.pagerduty) add("*", "escalation", "info", `Unacknowledged pages are re-sent on the same channel after ${cfg.settings.escalateAfterMin} min. Set notify.escalate for a backup person.`);
  if (cfg.access.hosts.length) add("*", "remote", "ok", `remote access allowed for ${cfg.access.hosts.join(", ")} with a token`);
  else add("*", "remote", "info", "Local only. To answer pages from your phone, run: wisprops token, then put a tunnel in front (see the Wispr page in the console).");
  if (cfg.alerts.token) add("*", "alerts", "ok", "POST /api/alert accepts alerts from your monitoring");
  if (cfg.settings.allowTyped) add("*", "consent", "warn", "settings.allowTyped is on: typed approvals are accepted (and flagged on the receipt)");
  if (cfg.settings.sayItBack === false) add("*", "consent", "warn", "settings.sayItBack is off: the bare \"approve fix one\" is accepted without saying the fix back");
  return out;
}

module.exports = { doctor, findProgram };
