/* Paging and pull requests.
   - ntfy (https://ntfy.sh, free, no account): pages, all-clears and the morning handoff on your phone.
   - webhook: Slack or Discord incoming webhook.
   - PagerDuty Events API v2: pages trigger, verified recoveries resolve (dedup key = incident id).
   - notify.escalate: a backup person's ntfy topic or webhook, used when a page is not acknowledged.
   - pull requests: written in a throwaway git worktree, so your checkout and branch are never touched.
     Pushes and opens a draft PR with the GitHub CLI only when "pr.push" is true. Nothing is ever merged. */
"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { run, readConfigFile, writeConfigKey } = require("./adapters.js");

const PRIORITY = { page: "urgent", escalate: "urgent", resolved: "default", handoff: "default" };
const TAGS = { page: "rotating_light", escalate: "sos", resolved: "white_check_mark", handoff: "sunrise" };

function createNotifier(cfg, publicUrl) {
  const n = cfg.notify || {};
  const link = (m) => (publicUrl ? publicUrl.replace(/\/?$/, "/") + (m.incidentId ? "#/board?inc=" + encodeURIComponent(m.incidentId) : "#/handoff") : "");

  const ntfy = (topic, m) => {
    const server = (n.ntfyServer || "https://ntfy.sh").replace(/\/+$/, "");
    const tail = m.kind === "page" || m.kind === "escalate" ? `\nOpen the console and answer with Wispr Flow${m.phrase ? `: “${m.phrase}”` : ""}.` : "";
    const headers = { Title: m.title, Tags: TAGS[m.kind] || "bell", Priority: PRIORITY[m.kind] || "default" };
    if (link(m)) headers.Click = link(m);
    if (n.ntfyToken) headers.Authorization = "Bearer " + n.ntfyToken;
    return fetch(`${server}/${encodeURIComponent(topic)}`, { method: "POST", headers, body: m.body + tail });
  };
  const hook = (url, m) => {
    const text = `${m.kind === "resolved" ? "✅" : m.kind === "handoff" ? "🌅" : "🚨"} *${m.title}*\n${m.body}${link(m) ? `\n${link(m)}` : ""}`;
    const body = /discord(app)?\.com\/api\/webhooks/.test(url) ? { content: text.replace(/\*/g, "**") } : { text };
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  };
  const pagerduty = (m) => {
    if (!["page", "resolved"].includes(m.kind) || !m.incidentId) return null;
    const payload = { routing_key: n.pagerduty, event_action: m.kind === "page" ? "trigger" : "resolve", dedup_key: "wisprops-" + m.incidentId };
    if (m.kind === "page") payload.payload = { summary: `${m.title}: ${m.body}`.slice(0, 1000), source: "wisprops", severity: "critical" };
    if (link(m)) payload.links = [{ href: link(m), text: "Answer with Wispr Flow in WisprOps" }];
    return fetch("https://events.pagerduty.com/v2/enqueue", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  };

  /** Returns one {channel, ok, detail} per channel tried, so the console can show a test page's result. */
  return async function notify(m) {
    const jobs = [], names = [];
    const job = (name, p) => { names.push(name); jobs.push(p); };
    if (m.kind === "escalate" && n.escalate && (n.escalate.ntfy || n.escalate.webhook)) {
      if (n.escalate.ntfy) job("ntfy (backup)", ntfy(n.escalate.ntfy, m));
      if (n.escalate.webhook) job("webhook (backup)", hook(n.escalate.webhook, m));
    } else {
      if (n.ntfy) job("ntfy", ntfy(n.ntfy, m));
      if (n.webhook) job(/discord/.test(n.webhook) ? "Discord" : "Slack webhook", hook(n.webhook, m));
    }
    if (n.pagerduty) { const j = pagerduty(m); if (j) job("PagerDuty", j); }
    const res = await Promise.allSettled(jobs);
    return res.map((r, k) => {
      if (r.status === "rejected") { console.error(`[notify] ${names[k]} failed:`, r.reason && r.reason.message); return { channel: names[k], ok: false, detail: String(r.reason && r.reason.message || "failed").slice(0, 160) }; }
      if (r.value && !r.value.ok) { console.error(`[notify] ${names[k]} HTTP`, r.value.status); return { channel: names[k], ok: false, detail: "HTTP " + r.value.status }; }
      return { channel: names[k], ok: true };
    });
  };
}

function createPR(cfg, baseDir) {
  return async function openPR(inc, markdown) {
    const svc = cfg.services.find((s) => s.id === inc.serviceId) || {};
    const repo = svc.repo ? path.resolve(baseDir, svc.repo) : null;
    if (!repo || !fs.existsSync(path.join(repo, ".git"))) {
      return { ok: false, text: "No git repo is configured for this service (set services[].repo). The postmortem is ready to copy from the console." };
    }
    const branch = `wisprops/${inc.id}-${Date.now().toString(36)}`;
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wisprops-pr-"));
    const git = (args) => run(["git", ...args], { cwd: dir, timeoutMs: 60000 });
    let added = false;
    try {
      let r = await run(["git", "-C", repo, "worktree", "add", "-b", branch, dir, "HEAD"], { timeoutMs: 60000 });
      if (!r.ok) return { ok: false, text: "Could not create a worktree: " + r.output };
      added = true;
      const pmDir = path.join(dir, "postmortems");
      fs.mkdirSync(pmDir, { recursive: true });
      const pmFile = path.join(pmDir, `${new Date(inc.opened).toISOString().slice(0, 10)}-${inc.id}.md`);
      fs.writeFileSync(pmFile, markdown);
      const changed = ["postmortems"];
      const fix = inc.proposals.find((p) => p.state === "applied");
      if (fix && fix.action.kind === "restore_config" && svc.config && svc.config.source) {
        const src = path.join(dir, svc.config.source);
        if (path.resolve(src).startsWith(path.resolve(dir) + path.sep) && fs.existsSync(src)) {
          try {
            const cur = readConfigFile(src);
            if (JSON.stringify(cur[fix.action.key]) !== JSON.stringify(fix.action.value)) { writeConfigKey(src, fix.action.key, fix.action.value); changed.push(svc.config.source); }
          } catch (e) { /* leave the source alone if it cannot be parsed */ }
        }
      }
      await git(["add", ...changed]);
      const title = `fix(${svc.name || inc.service}): ${inc.cause ? inc.cause.kind.replace("_", " ") : "incident"} (${inc.id})`;
      r = await git(["-c", "user.name=WisprOps", "-c", "user.email=wisprops@localhost", "commit", "-m", title, "-m", "Postmortem and source fix filed by WisprOps. Approved by voice with Wispr Flow; see the consent receipts in the postmortem."]);
      if (!r.ok) return { ok: false, text: "Commit failed: " + r.output };
      if (!(cfg.pr && cfg.pr.push)) {
        return { ok: true, branch, text: `Committed the postmortem${changed.length > 1 ? " and the source fix" : ""} on local branch ${branch}. Set "pr": {"push": true} to push it and open a draft PR. Nothing is merged.` };
      }
      r = await git(["push", "-u", cfg.pr.remote || "origin", branch]);
      if (!r.ok) return { ok: false, branch, text: "Push failed: " + r.output };
      const bodyDir = fs.mkdtempSync(path.join(os.tmpdir(), "wisprops-body-"));
      const body = path.join(bodyDir, "body.md");
      fs.writeFileSync(body, markdown);
      r = await run(["gh", "pr", "create", "--draft", "--title", title, "--body-file", body, "--head", branch], { cwd: dir, timeoutMs: 60000 });
      try { fs.rmSync(bodyDir, { recursive: true, force: true }); } catch (e) { /* already gone */ }
      const url = r.output.split(/\s+/).pop();
      return r.ok ? { ok: true, branch, url, text: `Draft PR opened: ${url}. Nothing is merged at 3 AM.` }
        : { ok: false, branch, text: `Branch ${branch} pushed, but the GitHub CLI could not open the PR: ${r.output}` };
    } finally {
      if (added) await run(["git", "-C", repo, "worktree", "remove", "--force", dir], { timeoutMs: 30000 });
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    }
  };
}

module.exports = { createNotifier, createPR };
