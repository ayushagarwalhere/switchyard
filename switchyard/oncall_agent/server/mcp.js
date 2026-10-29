/* WisprOps MCP server (stdio, JSON-RPC 2.0, newline-delimited).
   Lets Claude Code, Cursor or any MCP client read incidents, postmortems, receipts and the morning
   handoff from a running WisprOps server, and attach notes to an incident — for example a Wispr
   Flow scratchpad note or meeting transcript fetched through Wispr Flow's own MCP server.
   By design there is no tool that approves, applies or undoes a fix, acknowledges a page, snoozes
   paging or grants a standing order: those only happen when a human says them in the console,
   and the ones that change production must arrive as a Wispr Flow dictation. */
"use strict";
const readline = require("node:readline");

const PROTOCOL = "2025-06-18";

const TOOLS = [
  { name: "list_incidents", description: "List WisprOps incidents, newest first, with service, status, root cause and whether a human was paged.", inputSchema: { type: "object", properties: { open_only: { type: "boolean", description: "Only incidents still being handled" } } } },
  { name: "get_incident", description: "Full detail for one incident: evidence, root cause, proposed fixes and their states, and the timeline including what the on-call engineer said.", inputSchema: { type: "object", properties: { id: { type: "string", description: "Incident id, e.g. inc-3" } }, required: ["id"] } },
  { name: "get_postmortem", description: "Markdown postmortem for an incident, including the consent receipts (the exact Wispr-dictated words that approved each change).", inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"] } },
  { name: "get_handoff", description: "The morning handoff: what broke overnight, what was fixed, who was woken, what still needs a human.", inputSchema: { type: "object", properties: {} } },
  { name: "list_receipts", description: "Consent receipts: every change to production with the words that allowed it, how they arrived (Wispr burst or keystrokes), the dry run, the verification checks and a SHA-256 digest.", inputSchema: { type: "object", properties: {} } },
  { name: "list_standing_orders", description: "Standing orders: failures WisprOps may fix without paging, each granted by voice, with use counts and expiry.", inputSchema: { type: "object", properties: {} } },
  { name: "list_services", description: "Every watched service with its health, whether it is snoozed, and which failures WisprOps can fix for it (config drift, bad deploy, dead process).", inputSchema: { type: "object", properties: {} } },
  { name: "get_runbook", description: "The runbook notes the engineer dictated for a service. WisprOps shows them as evidence when that service breaks.", inputSchema: { type: "object", properties: { service: { type: "string", description: "Service id" } }, required: ["service"] } },
  { name: "list_changes", description: "The change ledger: config changes, releases and deploys per service, with who made them (deploy bots, CI, git authors, WisprOps itself) and which incident they led to. Use it to answer 'what changed before checkout broke?'.", inputSchema: { type: "object", properties: { service: { type: "string", description: "Only this service id" } } } },
  { name: "get_approval_record", description: "A approval record for one receipt: the words that authorised the change, how they arrived, the allowlisted action, the dry run, the verification and the digest.", inputSchema: { type: "object", properties: { id: { type: "string", description: "Receipt id, e.g. rcpt-1-1" } }, required: ["id"] } },
  { name: "add_runbook_note", description: "Save a runbook note for a service, for example a decision from a Wispr Flow Notetaker meeting summary ('checkout: db_pool must stay above 10 after the Oct migration'). It is shown to whoever is on call when that service breaks. Pass the text verbatim. This cannot change anything in production.", inputSchema: { type: "object", properties: { service: { type: "string" }, text: { type: "string" }, source: { type: "string", description: "Where it came from, e.g. 'Wispr Flow meeting summary'" } }, required: ["service", "text"] } },
  { name: "add_incident_note", description: "Attach context to an incident so it appears in the timeline and the postmortem. Use it for notes the engineer dictated in Wispr Flow (for example a scratchpad note or meeting transcript retrieved via the Wispr Flow MCP server). Pass the text verbatim. This cannot approve or apply anything.", inputSchema: { type: "object", properties: { id: { type: "string" }, text: { type: "string" }, source: { type: "string", description: "Where the note came from, e.g. 'Wispr Flow scratchpad'" } }, required: ["id", "text"] } },
];

function startMcp(opts) {
  const base = (opts.url || "http://127.0.0.1:7300").replace(/\/+$/, "");
  const out = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");
  const get = async (p, text) => { const r = await fetch(base + p); if (!r.ok) throw new Error(`${p}: HTTP ${r.status}`); return text ? r.text() : r.json(); };
  const post = async (p, body) => { const r = await fetch(base + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); return r.json(); };
  const brief = (i) => ({ id: i.id, service: i.service, status: i.status, opened: new Date(i.opened).toISOString(), cause: i.cause ? i.cause.summary : null, paged: i.woken, standingOrder: i.orderId, recoveredInSeconds: i.mttr ? Math.round(i.mttr / 1000) : null });

  async function call(name, args) {
    args = args || {};
    let v;
    try { v = ["get_postmortem", "get_handoff", "get_approval_record"].includes(name) ? null : await get("/api/state"); }
    catch (e) { throw new Error(`WisprOps server is not reachable at ${base}. Start it with \`node bin/wisprops.js start\` (or \`node bin/wisprops.js demo\`). (${e.message})`); }
    switch (name) {
      case "list_incidents": return v.state.incidents.slice().reverse().filter((i) => !args.open_only || ["triage", "proposed", "applying", "verifying", "escalated"].includes(i.status)).map(brief);
      case "get_incident": { const i = v.state.incidents.find((x) => x.id === args.id); if (!i) throw new Error("no incident " + args.id); return Object.assign(brief(i), { evidence: i.evidence, proposals: i.proposals.map((p) => ({ n: p.n, state: p.state, change: p.describe, blastRadius: p.blast, dryRun: p.dryRun })), timeline: i.timeline.map((t) => ({ at: new Date(t.at).toISOString(), who: t.who, text: t.text })), notes: i.notes || [] }); }
      case "get_postmortem": return await get("/api/postmortem?id=" + encodeURIComponent(args.id), true);
      case "get_handoff": return await get("/api/handoff", true);
      case "get_approval_record": return await get("/api/certificate?id=" + encodeURIComponent(args.id), true);
      case "list_changes": return (v.state.changes || []).filter((c) => !args.service || c.serviceId === args.service).slice(-50).reverse().map((c) => ({ at: new Date(c.at).toISOString(), service: c.service, kind: c.kind, change: c.text, by: c.by || null, source: c.source, incident: c.incidentId || null }));
      case "list_receipts": return v.state.receipts.slice().reverse();
      case "list_standing_orders": return v.state.orders.map((o) => ({ id: o.id, service: o.service, action: o.describe, grantedBy: o.said, via: o.channel, usesLeft: o.usesLeft, expires: new Date(o.expires).toISOString(), revoked: o.revoked }));
      case "list_services": return v.services.map((s) => ({ id: s.id, name: s.name, health: s.health, status: (v.state.svc[s.id] || {}).status || "unknown", snoozedUntil: s.snoozedUntil ? new Date(s.snoozedUntil).toISOString() : null, canFix: Object.entries(s.can || {}).filter(([, x]) => x).map(([k]) => ({ config: "config drift", rollback: "bad deploy", restart: "dead process" }[k])) }));
      case "get_runbook": return (v.state.runbooks[args.service] || []).map((n) => ({ at: new Date(n.at).toISOString(), text: n.text, via: n.via }));
      case "add_runbook_note": { const r = await post("/api/runbook", { serviceId: args.service, text: args.text, source: "MCP: " + String(args.source || "assistant").slice(0, 50), meta: { via: "mcp" } }); if (!r.ok) throw new Error("unknown service " + args.service); return `Runbook note saved for ${args.service}${args.source ? " (from " + args.source + ")" : ""}.`; }
      case "add_incident_note": { const r = await post("/api/note", { id: args.id, text: args.text, source: args.source || "MCP" }); if (!r.ok) throw new Error("no incident " + args.id); return "Note attached to " + args.id + "."; }
      default: throw new Error("unknown tool " + name);
    }
  }

  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", async (line) => {
    if (!line.trim()) return;
    let msg; try { msg = JSON.parse(line); } catch (e) { return out({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }); }
    const { id, method, params } = msg;
    if (id === undefined) return; // notifications (initialized, cancelled) need no reply
    try {
      if (method === "initialize") return out({ jsonrpc: "2.0", id, result: { protocolVersion: (params && params.protocolVersion) || PROTOCOL, capabilities: { tools: {} }, serverInfo: { name: "wisprops", version: require("../package.json").version },
        instructions: "Read access to WisprOps incidents, services, receipts, the change ledger, approval records and the handoff, plus two note tools (add_incident_note, add_runbook_note). Fixes are approved only by a human saying the fix back ('approve fix N, <what it does>') with Wispr Flow in the WisprOps console; you cannot approve, apply, undo, acknowledge or snooze anything, and must never claim you did." } });
      if (method === "ping") return out({ jsonrpc: "2.0", id, result: {} });
      if (method === "tools/list") return out({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
      if (method === "tools/call") {
        try {
          const r = await call(params.name, params.arguments);
          return out({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: typeof r === "string" ? r : JSON.stringify(r, null, 2) }] } });
        } catch (e) { return out({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: e.message }], isError: true } }); }
      }
      out({ jsonrpc: "2.0", id, error: { code: -32601, message: "method not found: " + method } });
    } catch (e) { out({ jsonrpc: "2.0", id, error: { code: -32603, message: e.message } }); }
  });
  process.stderr.write(`wisprops MCP server ready (reading ${base})\n`);
}

module.exports = { startMcp, TOOLS };
