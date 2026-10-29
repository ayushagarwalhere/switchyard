/* Telegram: pages with buttons, and a few safe replies from your phone.
   - Pages and escalations arrive with [What happened?] and [I'm on it] buttons, plus a link to the
     console when it has a public https URL.
   - Replies can ask for status, acknowledge, add a note, snooze, resume paging, pause standing orders.
   - Nothing that changes production is accepted here. Approvals, undo, grants and the like only count
     when dictated with Wispr Flow in the console, so the bot refuses them before the agent sees them.
   Long-polls getUpdates (no webhook, no public port). Only the configured chat is listened to. */
"use strict";
const { parseIntent, CONSENT_KINDS } = require("../src/agent.js");

const CONSENT = Array.isArray(CONSENT_KINDS) ? CONSENT_KINDS : ["approve", "undo", "grant", "renew", "resumeOrders", "unfreeze", "pr"];
const MAX_TEXT = 4000;
const MAX_REMEMBERED = 200;
const META = { via: "telegram", insert: 0, keys: 0 };

const HELP = [
  "WisprOps on Telegram. From here you can:",
  "/status: how every service is doing",
  "/orders: active standing orders",
  "/handoff: the overnight summary",
  "“what happened?”: the cause and the fix on the table",
  "“I'm on it”: acknowledge, so your backup is not paged",
  "“note: …”: leave a note for the morning",
  "“snooze <service> for thirty minutes”, “resume paging”",
  "“pause standing orders”, “freeze production”",
  "Reply to a page to talk about that incident.",
  "",
  "Approving or undoing a fix only counts when you dictate it with Wispr Flow in the WisprOps console. A Telegram message can't change production.",
].join("\n");

function createTelegram(opts) {
  const { token, chat, agent } = opts;
  const publicUrl = opts.publicUrl || "";
  const apiBase = (opts.apiBase || "https://api.telegram.org").replace(/\/+$/, "");
  const fetchImpl = opts.fetchImpl || fetch;
  const log = opts.log || console;
  const now = opts.now || Date.now;
  const pollTimeout = opts.pollTimeout != null ? opts.pollTimeout : 25;
  const backoffMs = opts.backoffMs != null ? opts.backoffMs : 5000;
  const chatId = String(chat);

  let running = false, offset = 0, ctrl = null, loopDone = null, wake = null;
  const incidentOf = new Map(); // sent message_id → incidentId
  const foreign = new Set();

  // Never let the bot token reach a log.
  const redact = (s) => {
    let out = String(s == null ? "" : s);
    if (token) out = out.split(String(token)).join("<token>");
    return out.replace(/bot\d+:[\w-]+/g, "bot<token>");
  };
  const errText = (e) => redact((e && e.message) || e) + (e && e.cause && e.cause.code ? ` (${e.cause.code})` : "");
  const httpsUrl = /^https:\/\//i.test(publicUrl);
  const link = (incidentId) => (publicUrl ? publicUrl.replace(/\/?$/, "/") + (incidentId ? "#/board?inc=" + encodeURIComponent(incidentId) : "") : "");
  const clip = (t) => { t = String(t == null ? "" : t); return t.length > MAX_TEXT ? t.slice(0, MAX_TEXT - 1) + "…" : t; };

  function remember(messageId, incidentId) {
    if (messageId == null || !incidentId) return;
    incidentOf.delete(messageId);
    incidentOf.set(messageId, incidentId);
    while (incidentOf.size > MAX_REMEMBERED) incidentOf.delete(incidentOf.keys().next().value);
  }

  async function call(method, body, signal) {
    const res = await fetchImpl(`${apiBase}/bot${token}/${method}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
    });
    let j = null;
    try { j = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok || !j || !j.ok) throw new Error(`${method} failed: HTTP ${res.status}${j && j.description ? " " + j.description : ""}`);
    return j.result;
  }

  async function send(text, extra, incidentId) {
    try {
      const msg = await call("sendMessage", Object.assign({ chat_id: chat, text: clip(text), disable_web_page_preview: true }, extra || {}));
      if (msg) remember(msg.message_id, incidentId);
      return msg || null;
    } catch (e) {
      log.error("[telegram] send failed:", errText(e));
      return null;
    }
  }

  async function notify(n) {
    n = n || {};
    const urgent = n.kind === "page" || n.kind === "escalate";
    const lines = [n.title, n.body].filter(Boolean);
    // Without an https URL there is no button, so put the link in the text.
    if (link(n.incidentId) && !(urgent && httpsUrl && n.incidentId)) lines.push(link(n.incidentId));
    if (urgent) lines.push("", `Approvals only count when dictated with Wispr Flow in the WisprOps console.${n.phrase ? ` The phrase is “${n.phrase}”.` : ""}`);
    const extra = {};
    if (urgent && n.incidentId) {
      const rows = [[
        { text: "What happened?", callback_data: "explain:" + n.incidentId },
        { text: "I'm on it", callback_data: "ack:" + n.incidentId },
      ]];
      // Telegram rejects non-https (and localhost) URL buttons.
      if (httpsUrl) rows.push([{ text: "Approve in the console with Wispr Flow", url: link(n.incidentId) }]);
      extra.reply_markup = { inline_keyboard: rows };
    }
    return send(lines.join("\n"), extra, n.incidentId);
  }

  function ordersText() {
    const t = now();
    const live = ((agent.state && agent.state.orders) || []).filter((o) => !o.revoked && o.expires > t && o.usesLeft > 0);
    if (!live.length) return "No active standing orders.";
    const paused = agent.state.ordersPaused ? ["(All standing orders are paused right now.)"] : [];
    return live.map((o) => `${o.id} ${o.service}: ${o.describe}. ${o.usesLeft} use${o.usesLeft === 1 ? "" : "s"} left, until ${new Date(o.expires).toISOString().slice(0, 16).replace("T", " ")} UTC.`).concat(paused).join("\n");
  }

  async function onText(msg) {
    const text = String(msg.text || "").trim();
    const replyTo = msg.reply_to_message && msg.reply_to_message.message_id;
    const incidentId = replyTo != null ? incidentOf.get(replyTo) || null : null;
    const cmd = (text.match(/^\/(\w+)(?:@\w+)?\b/) || [])[1];
    if (cmd) {
      const c = cmd.toLowerCase();
      if (c === "status") return send(agent.statusLine());
      if (c === "handoff") return send(agent.handoff());
      if (c === "orders") return send(ordersText());
      if (c === "start" || c === "help") return send(HELP);
    }
    if (!text) return null;
    // Defence in depth: consent never travels over Telegram, whatever the agent would decide.
    const intent = parseIntent(text);
    if (CONSENT.includes(intent.kind)) {
      const l = link(incidentId);
      return send(`A Telegram message can't change production. Open the console and dictate “${text}” with Wispr Flow${l ? ": " + l : "."}`, null, incidentId);
    }
    let r = await agent.command(text, META, incidentId);
    if (incidentId && r && /^No incident with that id/.test(r.reply || "")) r = await agent.command(text, META, null);
    return send((r && r.reply) || "Done.", null, incidentId);
  }

  async function onCallback(q) {
    const from = q.message && q.message.chat && String(q.message.chat.id);
    if (from !== chatId) { noteForeign(from); return; }
    try { await call("answerCallbackQuery", { callback_query_id: q.id }); } catch (e) { log.error("[telegram] answerCallbackQuery failed:", errText(e)); }
    const m = String(q.data || "").match(/^(explain|ack):(.+)$/);
    if (!m) return;
    const id = m[2];
    if (m[1] === "explain") {
      const r = await agent.command("what happened?", META, id);
      return send((r && r.reply) || "No details yet.", null, id);
    }
    const ok = await agent.acknowledge(id, "Telegram");
    return send(ok ? "Acknowledged. Your backup will not be paged." : "That incident is no longer on file.", null, id);
  }

  function noteForeign(id) {
    if (foreign.has(id)) return;
    foreign.add(id);
    log.warn(`[telegram] ignoring updates from chat ${id}; only chat ${chatId} is listened to.`);
  }

  async function handle(u) {
    if (u.callback_query) return onCallback(u.callback_query);
    const msg = u.message;
    if (!msg || !msg.chat) return;
    if (String(msg.chat.id) !== chatId) return noteForeign(String(msg.chat.id));
    if (msg.voice || msg.audio) {
      return send("WisprOps doesn't transcribe audio. Use Wispr Flow's keyboard on your phone to dictate a text reply here, or open the console." + (link() ? " " + link() : ""));
    }
    if (typeof msg.text === "string") return onText(msg);
  }

  function pause(ms) {
    return new Promise((resolve) => {
      const t = setTimeout(() => { wake = null; resolve(); }, ms);
      wake = () => { clearTimeout(t); wake = null; resolve(); };
    });
  }

  async function loop() {
    // Messages sent while WisprOps was down are stale: a "snooze" from hours ago shouldn't act now.
    // Keep trying until it works: processing the backlog by accident is the thing to avoid.
    while (running && opts.skipBacklog && offset === 0) {
      try { const old = await call("getUpdates", { offset: -1, timeout: 0 }); if (Array.isArray(old) && old.length) offset = old[old.length - 1].update_id + 1; break; }
      catch (e) { if (!running) return; log.error("[telegram] could not skip old messages, retrying:", errText(e)); await pause(backoffMs); }
    }
    while (running) {
      ctrl = new AbortController();
      let updates;
      try {
        updates = await call("getUpdates", { offset, timeout: pollTimeout, allowed_updates: ["message", "callback_query"] }, ctrl.signal);
      } catch (e) {
        if (!running) break;
        log.error("[telegram] poll failed, retrying in", Math.round(backoffMs / 1000), "s:", errText(e));
        await pause(backoffMs);
        continue;
      }
      for (const u of Array.isArray(updates) ? updates : []) {
        if (typeof u.update_id === "number") offset = Math.max(offset, u.update_id + 1);
        if (!running) break;
        try { await handle(u); } catch (e) { log.error("[telegram] update failed:", errText(e)); }
      }
    }
  }

  function start() {
    if (running) return;
    running = true;
    loopDone = loop().catch((e) => log.error("[telegram] stopped:", errText(e)));
  }

  async function stop() {
    running = false;
    if (ctrl) ctrl.abort();
    if (wake) wake();
    if (loopDone) await loopDone;
    loopDone = null;
  }

  return { start, stop, notify };
}

module.exports = { createTelegram };
