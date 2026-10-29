/* wisprops judge: one full incident, played in the terminal in a few seconds.
   The real agent and the real consent rules run against the simulated production; only the
   clock is fast-forwarded. Every line the engineer "says" is labelled with how it arrived.
   Each safety property is checked as it happens, and the command fails if one doesn't hold. */
"use strict";
const crypto = require("node:crypto");
const { createAgent } = require("../src/agent.js");
const { createSim } = require("../src/sim.js");

async function judge(opts) {
  opts = opts || {};
  const out = opts.out || ((s) => process.stdout.write(s + "\n"));
  const color = opts.color != null ? opts.color : process.stdout.isTTY;
  const c = (code) => (s) => (color ? `\x1b[${code}m${s}\x1b[0m` : s);
  const dim = c("2"), bold = c("1"), red = c("31"), green = c("32"), yellow = c("33"), violet = c("35"), cyan = c("36");

  let t = new Date(2026, 9, 29, 23, 40).getTime(); // 23:40 on this machine's clock
  const now = () => t;
  const sim = createSim({ now });
  const pages = [];
  const adapters = Object.assign({}, sim.adapters, { notify: async (m) => pages.push(m), digest: async (s) => crypto.createHash("sha256").update(s).digest("hex") });
  const agent = createAgent({ config: sim.config, adapters });
  const tick = async (n) => { for (let k = 0; k < (n || 1); k++) { t += 5000; await agent.tick(); } };
  const clock = () => { const d = new Date(t); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
  const checks = [];
  const check = (ok, what) => { checks.push({ ok, what }); out(`   ${ok ? green("✓") : red("✖")} ${ok ? dim(what) : red(what)}`); };
  const wrap = (s, n) => String(s).replace(new RegExp(`(.{1,${n}})(\\s|$)`, "g"), "$1\n").trim().split("\n");
  const agentSays = (s) => wrap(s, 92).forEach((l, k) => out(`   ${k ? "         " : violet("WisprOps")} ${l}`));
  const scene = (s) => out("\n" + bold(cyan("▸ " + s)) + dim("  " + clock()));
  const by = { wispr: (s) => ({ via: "wispr", insert: s.length, keys: 0 }), typed: (s) => ({ via: "typed", insert: 1, keys: s.length }) };
  async function say(id, text, how) {
    const meta = by[how](text);
    out(`   ${yellow("you")}      “${text}”  ${dim(how === "wispr" ? `(Wispr Flow: ${meta.insert} chars in one burst)` : `(typed: ${meta.keys} keystrokes)`)}`);
    const r = await agent.command(text, meta, id);
    agentSays(r.reply);
    return r;
  }
  const last = () => agent.state.incidents[agent.state.incidents.length - 1];

  out(bold("WisprOps · one incident, start to finish, in the terminal"));
  out(dim("The real agent and consent rules against a simulated production (checkout-api, auth). Only the clock is fast."));

  scene("Everything is healthy. WisprOps takes a last-healthy snapshot.");
  await tick(3);
  check(!!agent.rt("checkout-api").golden, "golden snapshot taken: db_pool=20, release v41");

  scene("A deploy bot sets db_pool to 0 on checkout-api.");
  sim.faults.config.run();
  await tick(2);
  const i = last();
  out(`   ${red("PAGE")}     ${pages.filter((p) => p.kind === "page").pop().title}: ${i.cause.summary}`);
  i.evidence.filter((e) => ["changes", "logs"].includes(e.kind)).forEach((e) => wrap(e.text, 92).forEach((l, k) => out(`   ${k ? "         " : dim("evidence")} ${dim(l)}`)));
  const p = i.proposals[0];
  agentSays(`Fix ${p.n}: ${p.describe} Dry run: ${p.dryRun}`);
  check(i.cause.kind === "config_drift" && i.cause.by === "deploy-bot", "cause found and attributed: db_pool 20 → 0, changed by deploy-bot");

  scene("Half asleep, you say yes. A yes isn't an approval.");
  await say(i.id, "Yes, do it.", "wispr");
  check(p.state === "pending" && !agent.state.receipts.length, "“yes, do it” changed nothing");
  await say(i.id, "approve fix 1", "typed");
  check(p.state === "pending" && !agent.state.receipts.length, "the exact phrase, typed on a keyboard, was refused");

  scene("You hold the Wispr Flow key. “Approve fix one” alone isn't enough: you say what it does.");
  await say(i.id, "Approve fix one.", "wispr");
  check(p.state === "pending", "the bare phrase is not enough: it asks you to say the fix back");
  await say(i.id, "Approve fix one, DB pool back to ten.", "wispr");
  check(p.state === "pending" && sim.svcs["checkout-api"].config.db_pool === 0, "a say-back with the wrong value (10, not 20) was refused");
  await say(i.id, "Approve fix one, DB pool back to twenty on checkout API.", "wispr");
  const rc = agent.state.receipts[0];
  check(!!(rc && rc.saidBack), `the right say-back applied it: ${rc && rc.saidBack ? rc.saidBack.map((f) => f.label + " ✓").join(" ") : "no receipt"}`);
  await tick(4);
  agentSays(i.timeline.filter((x) => x.who === "agent").pop().text);
  check(i.status === "resolved", `verified recovery: ${agent.settings.verifyOk} healthy checks in a row and db_pool back to 20`);

  scene("You let it handle this exact failure next time.");
  await say(i.id, "Grant standing order for seven days.", "wispr");
  const o = agent.state.orders[0];
  check(!!o && o.usesLeft === 3, "standing order so-1: one service, one signature, one action, 3 uses, 7 days");

  scene("03:10. The same thing happens again.");
  t = new Date(2026, 9, 30, 3, 9).getTime();
  await tick(3);
  sim.faults.config.run();
  await tick(6);
  const j = last();
  j.timeline.filter((x) => x.who === "agent" && /seen this before|covers this exact|^Recovered/.test(x.text)).forEach((x) => agentSays(x.text));
  check(j.status === "resolved" && j.orderId === "so-1" && !j.woken, "fixed under the standing order; nobody was woken");
  check(j.recurrence && j.recurrence.count === 2, "it remembered: the 2nd time in 7 days");

  scene("Someone panics and freezes production. Then a bad release ships.");
  await say(null, "Freeze production.", "typed");
  await tick(2);
  sim.faults.deploy.run();
  await tick(2);
  const k = last();
  await say(k.id, "Approve fix one, roll checkout back to v41.", "wispr");
  check(k.proposals[0].state === "pending" && !k.receipts.length, "frozen: even a Wispr approval applies nothing");
  await say(null, "unfreeze production", "typed");
  check(!!agent.state.frozen, "unfreezing by keyboard is refused");
  await say(null, "Unfreeze production.", "wispr");
  await say(k.id, "Approve fix one, roll checkout back to v41.", "wispr");
  await tick(4);
  check(k.status === "resolved", "rolled back to v41 and verified");

  scene("Morning. The handoff, and the receipt behind the first fix.");
  agent.handoff(t - 12 * 3600000).split("\n").forEach((l) => wrap(l, 96).forEach((x) => out("   " + x)));
  out("");
  const cert = agent.certificate(agent.state.receipts[0].id);
  cert.split("\n").filter(Boolean).slice(0, 9).forEach((l) => out("   " + dim(l.replace(/\*\*/g, ""))));
  const plainCert = cert.replace(/\*\*/g, "");
  check(/Words: “Approve fix one, DB pool back to twenty on checkout API\.”/.test(plainCert) && /Said back: db_pool ✓ · 20 ✓ · checkout-api ✓/.test(plainCert) && /56 characters in one insertion, 0 keystrokes/.test(plainCert), "approval record quotes the words, what you said back, and how they arrived");

  scene("Then you talk it through for a minute. One dictation becomes the postmortem.");
  await say(i.id, "Debrief: deploy-bot ran the staging profile against production, which set db_pool to zero. Sam will make the deploy job refuse a zero pool by Friday. We should alert on pool exhaustion before the health check fails. The standing order worked, nobody was woken the second time. db_pool must stay at 10 or more.", "wispr");
  const pm = agent.postmortem(i);
  pm.split("\n").filter((l) => /^#{2,3} (?:Debrief|Why|Action|What went|Rules)|^- \[ \]|enforces this/.test(l)).forEach((l) => out("   " + dim(l)));
  check(/- \[ \] \*\*Sam\*\*: make the deploy job refuse a zero pool \(by Friday\)/.test(pm) && /db_pool at least 10/.test(pm) && (agent.state.runbooks["checkout-api"] || []).some((n) => n.rule), "the debrief became action items with owners, a cause, and a rule WisprOps now enforces");

  const s = agent.stats();
  out("\n" + bold("The night in numbers"));
  out(`   ${s.incidents} incidents · woken ${s.woken} times · ${s.underOrder} fixed while you slept · ${s.approvals} Wispr approvals · ${s.refusals} refusals · mean time to verified recovery ${Math.round(s.mttrMs / 1000)}s`);
  const failed = checks.filter((x) => !x.ok);
  out("\n" + (failed.length ? red(bold(`${failed.length} of ${checks.length} checks failed.`)) : green(bold(`All ${checks.length} checks held.`))) + dim("  Run it on your own services: wisprops init && wisprops start"));
  return { ok: !failed.length, checks };
}

module.exports = { judge };
