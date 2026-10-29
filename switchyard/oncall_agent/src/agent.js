/* WisprOps agent core.
   Watches services, finds the cause, proposes one allowlisted fix, applies it only on a phrase
   dictated with Wispr Flow, proves the recovery, and can handle the same failure again under a
   standing order. Runs unchanged in the browser (demo sandbox) and in Node (real services).
   All side effects go through adapters. */
(function (root) {
  "use strict";

  // ---------- speech parsing ----------
  const NUM = {
    one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
    eleven: 11, twelve: 12, fourteen: 14, fifteen: 15, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, ninety: 90,
    // Hinglish numbers, as Wispr Flow writes them
    ek: 1, teen: 3, char: 4, chaar: 4, paanch: 5, panch: 5, chhe: 6, chhah: 6, saat: 7, aath: 8, nau: 9, das: 10, pandrah: 15, bees: 20, tees: 30,
  };
  // Words dictation sometimes produces for small numbers. Fine for durations, never for which fix to approve.
  const LOOSE = { a: 1, an: 1, won: 1, to: 2, too: 2, for: 4, do: 2 };
  // Wispr Flow sometimes writes English command words in Devanagari when Hindi is on ("अप्रूव फिक्स वन").
  // Map those words before matching, so the same words unlock the same change in either script.
  // Hindi cardinals are deliberately not mapped: "दो" is both "two" and "give".
  const DEVA = {
    "अप्रूव": "approve", "फिक्स": "fix", "फ़िक्स": "fix", "अनडू": "undo", "ग्रांट": "grant", "ग्रान्ट": "grant", "स्टैंडिंग": "standing", "स्टेंडिंग": "standing",
    "ऑर्डर": "order", "आर्डर": "order", "डेज़": "days", "डेज": "days", "दिन": "din", "वन": "one", "टू": "two", "थ्री": "three", "फोर": "four", "फ़ोर": "four",
    "फाइव": "five", "फ़ाइव": "five", "सेवन": "seven", "नंबर": "number", "रीड": "read", "इट": "it", "बैक": "back", "स्टेटस": "status", "रिन्यू": "renew",
    "फ्रीज़": "freeze", "फ्रीज": "freeze", "अनफ्रीज़": "unfreeze", "अनफ्रीज": "unfreeze", "प्रोडक्शन": "production", "प्लीज़": "please", "प्लीज": "please",
    "मत": "mat", "नहीं": "nahi", "नही": "nahi", "ना": "na", "न": "na", "मैं": "main", "देख": "dekh", "रहा": "raha", "रही": "rahi", "हूँ": "hoon", "हूं": "hoon", "हू": "hoon", "क्या": "kya", "हुआ": "hua", "संभाल": "sambhal", "लूंगा": "lunga", "लूँगा": "lunga",
  };
  const deva = (t) => String(t || "").replace(/[।॥]/g, ".").replace(/[ऀ-ॿ]+/g, (w) => DEVA[w] || w);
  const norm = (t) => deva(t).toLowerCase().replace(/[’']/g, "").replace(/[^\p{L}\p{M}\p{N}\s]/gu, " ").replace(/\s+/g, " ").trim();
  const numOf = (w) => (/^\d+$/.test(w) ? Number(w) : NUM[w] || LOOSE[w]);
  const fixNum = (w) => (/^\d+$/.test(w) ? Number(w) : NUM[w]);
  // "forty five" → 45
  const numPair = (a, b) => { const x = numOf(a), y = numOf(b); return x >= 20 && y && y < 10 ? x + y : x; };

  function minutesIn(n) {
    let m;
    if (/\bfor (?:half an hour|aadha ghanta)\b/.test(n)) return 30;
    if (/\bfor (?:an|one|a|ek) (?:hour|ghanta)\b/.test(n)) return 60;
    if ((m = n.match(/\bfor (\w+)(?: (\w+))? (minutes?|mins?|minat|hours?|hrs?|ghante?|ghanta)\b/))) {
      const k = m[2] ? numPair(m[1], m[2]) : numOf(m[1]);
      if (k) return /^(h|g)/.test(m[3]) ? k * 60 : k;
    }
    return null;
  }

  // A question or a negation is never a command that changes something.
  const HEDGE = /\b(?:dont|do not|never|not|no|na|if|whether|should|could|would|can|cant|shall|maybe|mat|nahi|nahin|wait|unless|except|until|after|before|later|tomorrow|but)\b/;
  const LEAD = "(?:(?:ok|okay|yes|yeah|yep|haan|han|alright|right|so|then|please|now)\\s+)*";
  const TAIL = "(?:\\s+(?:please|now|thanks|thank you|karo|kar do))*";
  const RX = {
    // "approve fix one" may carry a say-back: "approve fix one, db_pool back to 20 on checkout api".
    approve: new RegExp("^" + LEAD + "approve fix (?:number )?(\\w+)\\b(.*)$"),
    undo: new RegExp("^" + LEAD + "undo fix (?:number )?(\\w+)" + TAIL + "$"),
    grant: new RegExp("^" + LEAD + "(?:grant|give|issue)(?: a| the)? standing order\\b(.*)$"),
    renew: new RegExp("^" + LEAD + "(?:renew|extend)(?: the| my)? standing orders?(?: (?:so |s o |so|number )?(\\d+|one|two|three|four|five|six|seven|eight|nine|ten|ek|do|teen))?\\b(.*)$"),
    unfreeze: new RegExp("^" + LEAD + "(?:unfreeze|unlock|thaw)(?: all)?(?: production| prod| changes| all changes| everything)?" + TAIL + "$"),
  };
  const CHANGE_WORDS = /\b(?:approve|undo) fix\b|\b(?:grant|give|issue|renew|extend)\b.*\bstanding order\b|\b(?:unfreeze|unlock)\b/;
  /** Intents that change production or loosen a safety switch. They only count as a Wispr Flow dictation. */
  const CONSENT_KINDS = ["approve", "undo", "grant", "renew", "resumeOrders", "unfreeze", "pr"];
  const PAGES = { board: "board", incidents: "board", services: "services", service: "services", audit: "receipts", "audit log": "receipts", receipts: "receipts", "standing orders": "orders", orders: "orders",
    handoff: "handoff", "morning handoff": "handoff", analytics: "analytics", stats: "analytics", changes: "changes", "change ledger": "changes", ledger: "changes", wispr: "wispr", "wispr page": "wispr", demo: "demo", docs: "docs", settings: "settings", "notifications": "settings" };
  function grantTerms(n, m0) {
    let m, days = 7, uses = 3;
    if ((m = n.match(/\bfor (\w+)(?: (\w+))? (?:days?|din)\b/))) days = (m[2] ? numPair(m[1], m[2]) : numOf(m[1])) || days;
    else if (/\bfor (?:a|one|ek) (?:week|hafta)\b/.test(n)) days = 7;
    else if (/\bfor (?:a|one|ek) month\b/.test(n)) days = 30;
    else if (/\bfor (?:tonight|one night|a night|aaj raat)\b/.test(n)) days = 1;
    if ((m = n.match(/\b(\w+) (?:times|uses|baar)\b/))) uses = numOf(m[1]) || uses;
    const night = /\b(?:only )?(?:at night|overnight|nights only|raat ko|raat mein)\b/.test(m0 || n);
    return night ? { days, uses, night } : { days, uses };
  }

  // After "grant/renew standing order", only its terms may follow. "…unless it's a rollback" or
  // "…after I check the logs" is not consent to anything, so it is unclear.
  const TERMS = new RegExp("\\b(?:for|a|an|the|one|ek|days?|din|weeks?|hafta|months?|tonight|nights?|aaj|raat|ko|mein|only|at|overnight|times|uses|baar|please|now|and|with|thanks|thank you|karo|kar do|" + Object.keys(NUM).join("|") + "|\\d+)\\b", "g");
  const onlyTerms = (tail) => !String(tail || "").replace(TERMS, " ").trim();

  /** What did the person ask for? Pure; the safety checks live in the agent, not here. */
  function parseIntent(text) {
    const raw = String(text || "").trim();
    const n = norm(text);
    const asked = /\?\s*$/.test(raw) || /^(?:what|why|how|is|was|are|did|does|kya|kyun)\b/.test(n);
    const hedged = HEDGE.test(n) || asked;
    let m;
    // A note is a note, whatever words are inside it.
    if ((m = raw.match(/^\s*(?:note|add (?:a )?note|handoff note|note for the morning|for the morning)\s*[:,.-]?\s+([\s\S]+)$/i))) return { kind: "note", text: m[1].trim() };
    // A debrief is a long dictation about the incident. Like a note, the words inside it are never commands.
    if ((m = raw.match(/^\s*(?:debrief|de-brief|post[- ]?mortem|retro(?:spective)?)\s*[:,.-]?\s+([\s\S]+)$/i))) return { kind: "debrief", text: m[1].trim() };
    if (!hedged && (m = n.match(RX.approve))) {
      const k = fixNum(m[1]);
      if (k) {
        const back = m[2].replace(/^(?:\s*(?:please|now|thanks|thank you|karo|kar do))+\s*$/, "").trim();
        if (!back) return { kind: "approve", n: k };
        // Keep digits and decimals as written ("0.5", "1,000") for the number check.
        const low = deva(raw).toLowerCase(), at = low.search(/\bfix\s+(?:number\s+)?\S+/);
        const rawBack = at < 0 ? back : low.slice(at).replace(/^fix\s+(?:number\s+)?\S+/, "").replace(/(\d),(?=\d{3}\b)/g, "$1");
        return { kind: "approve", n: k, back, rawBack };
      }
    }
    if (!hedged && (m = n.match(RX.undo))) { const k = fixNum(m[1]); if (k) return { kind: "undo", n: k }; }
    if (!hedged && (m = n.match(RX.grant)) && onlyTerms(m[1])) return Object.assign({ kind: "grant" }, grantTerms(n));
    if (!hedged && (m = n.match(RX.renew)) && onlyTerms(m[2])) {
      const id = m[1] ? "so-" + (fixNum(m[1]) || NUM[m[1]] || m[1]) : null;
      return Object.assign({ kind: "renew", id }, grantTerms(n));
    }
    if (!hedged && RX.unfreeze.test(n)) return { kind: "unfreeze" };
    // Mentions a change but is a question, a negation, or not the exact phrase: say so, change nothing.
    if (CHANGE_WORDS.test(n)) return { kind: "unclear" };
    if (!asked) {
      // Making things safer never needs Wispr: anyone can freeze, from anywhere.
      if (/^(?:please )?(?:freeze|lock)(?: all)?(?: production| prod| changes| all changes| everything| deploys| fixes)(?: now)?(?: please)?$|^(?:please )?stop all changes(?: now)?$/.test(n)) return { kind: "freeze" };
      if ((m = n.match(/^(?:please )?(?:open|show|go to|take me to|switch to|bring up)(?: me)?(?: the| my)? (board|incidents|services?|audit(?: log)?|receipts|standing orders|orders|(?:morning )?handoff|analytics|stats|changes|change ledger|ledger|wispr(?: page)?|demo|docs|settings|notifications)(?: page| tab)?(?: please)?$/))) return { kind: "go", page: PAGES[m[1]] };
      if ((m = n.match(/^(?:please )?(?:open|select|pick|show|go to|switch to)(?: me)?(?: the)? incident (?:number |no )?(\w+)$/))) { const k = fixNum(m[1]); if (k) return { kind: "select", n: k }; }
    }
    if (!asked) {
      if (/^(?:please )?(?:pause|suspend|freeze|stop)(?: all| the| my)? standing orders?$/.test(n)) return { kind: "pauseOrders" };
      if (/^(?:please )?(?:resume|unpause|restart|unfreeze)(?: all| the| my)? standing orders?$/.test(n)) return { kind: "resumeOrders" };
      if (/^(?:please )?(?:revoke|cancel|end)(?: the| my| this)? standing orders?$/.test(n)) return { kind: "revoke" };
      if ((m = n.match(/^(?:please )?(?:unsnooze|unmute|end maintenance|stop maintenance|resume (?:paging|pages|alerts|watching))(?: (?:on|for))?(?: (.+))?$/))) return { kind: "unsnooze", target: (m[1] || "").trim() };
      if ((m = n.match(/^(?:please )?(?:snooze|mute|silence|maintenance(?: mode)?(?: on)?|pause (?:paging|pages|alerts)(?: on)?)\s*(.*?)\s*(?:\bfor\b.*)?$/))) {
        const target = (m[1] || "").replace(/^(?:on|for)\s+/, "").replace(/\b(?:alerts?|paging|pages)\b/g, "").replace(/\s+/g, " ").trim();
        const minutes = minutesIn(n) || 30;
        return { kind: "snooze", target, minutes: Math.min(Math.max(minutes, 1), 24 * 60) };
      }
    }
    if (/^(?:ok(?:ay)? |haan |ha )?(?:i m on it|im on it|i am on it|on it|ack|acknowledged?|acknowledge it|got it|looking into it|looking at it|main dekh raha (?:hoon|hu|hun)|main dekh rahi (?:hoon|hu|hun)|dekh raha (?:hoon|hu|hun)|dekh rahi (?:hoon|hu|hun))\b/.test(n)) return { kind: "ack" };
    if (!hedged && /^(?:please )?(?:ok(?:ay)? )?open(?: the| a)? (?:pull request|pr)(?: please)?$/.test(n)) return { kind: "pr" };
    if (/\b(?:read it back|read that back|repeat that|say that again|read the fix|phir se bolo)\b/.test(n)) return { kind: "readback" };
    if (/\b(?:what changed|who changed|what was changed|any changes|recent changes|kya badla|kisne badla)\b/.test(n)) return { kind: "changes" };
    if (/\b(?:handoff|hand off|last night|overnight|morning report|kal raat)\b/.test(n)) return { kind: "handoff" };
    if (/\b(?:what happened|whats wrong|what is wrong|status|explain|why|kya hua)\b/.test(n)) return { kind: "explain" };
    if (/^(?:ok(?:ay)? |no )?(?:i ll handle it|ill handle it|i will handle it|stand down|leave it to me|dont touch it|do not touch it|main sambhal lunga|main sambhal lungi|rehne do)\b/.test(n)) return { kind: "standdown" };
    if (/^(?:yes|yeah|yep|yup|ok|okay|sure|fine|haan|ha|han|theek hai|thik hai)?\s*(?:do it|go ahead|go for it|ship it|kar do|karo|apply it|fix it|approve it|approved)?\s*(?:please)?$/.test(n) && n) return { kind: "vague" };
    return { kind: "chat" };
  }

  /** Every phrase WisprOps understands. Shown in the console's phrasebook. */
  const PHRASES = [
    { say: "approve fix one, db_pool back to 20", hinglish: "approve fix ek, db_pool wapas 20", does: "Say the fix back in one Wispr Flow dictation. Every fact you say (the key, the value, the release, the service) is checked against the fix; one wrong fact and nothing is applied.", wispr: true, scope: "incident" },
    { say: "debrief: deploy-bot ran the staging profile, which set db_pool to zero. Sam will make the deploy job refuse a zero pool by Friday.", does: "Talk it through for a minute. WisprOps files it in the postmortem: why it happened, action items with owners and dates, what went well, and any “must stay at” rule.", scope: "incident" },
    { say: "read it back", hinglish: "phir se bolo", does: "Reads the pending fix aloud. Speak over it and the fix is withdrawn.", scope: "incident" },
    { say: "I'm on it", hinglish: "main dekh raha hoon", does: "Acknowledges the page and stops escalation to your backup.", scope: "incident" },
    { say: "what happened?", hinglish: "kya hua?", does: "Explains the root cause and what is waiting.", scope: "incident" },
    { say: "undo fix one", hinglish: "undo fix ek", does: "Runs the inverse of an applied fix and hands the incident back to you. Needs Wispr.", wispr: true, scope: "incident" },
    { say: "I'll handle it", hinglish: "main sambhal lunga", does: "Withdraws the fix. Nothing will be applied.", scope: "incident" },
    { say: "grant standing order for seven days", hinglish: "grant standing order for saat din", does: "After a verified fix: handle this exact failure next time without paging. Needs Wispr.", wispr: true, scope: "incident" },
    { say: "revoke standing order", does: "Ends the standing orders for this service.", scope: "incident" },
    { say: "open the pull request", does: "Commits the postmortem (and the source fix) on a new branch; opens a draft PR if allowed.", scope: "incident" },
    { say: "note: the deploy bot ran the staging profile", does: "Adds a handoff note to the incident, its postmortem and the morning handoff.", scope: "incident" },
    { say: "snooze checkout api for thirty minutes", does: "Maintenance window: keep watching, but don't open incidents or page.", scope: "global" },
    { say: "resume paging", does: "Ends every snooze now.", scope: "global" },
    { say: "pause standing orders", does: "Kill switch: every failure pages you until you say “resume standing orders”.", scope: "global" },
    { say: "what happened last night?", hinglish: "kal raat kya hua?", does: "Reads the morning handoff.", scope: "global" },
    { say: "what changed?", hinglish: "kya badla?", does: "Reads the change ledger: what changed before it broke, and who changed it.", scope: "global" },
    { say: "freeze production", does: "Global kill switch: nothing is applied, by anyone or any standing order, until you unfreeze.", scope: "global" },
    { say: "unfreeze production", does: "Lets approved fixes run again. Needs Wispr.", wispr: true, scope: "global" },
    { say: "renew standing order for seven days", does: "Extends a standing order before it runs out. Add “only at night” to limit it to 22:00–08:00. Needs Wispr.", wispr: true, scope: "global" },
    { say: "open the audit log", hinglish: "", does: "Moves the console: board, services, audit log, standing orders, handoff, analytics, changes, Wispr, docs.", scope: "global" },
    { say: "open incident two", does: "Selects incident #2 so the next thing you say is about it.", scope: "global" },
  ];

  // ---------- text helpers (pure) ----------
  /** Dictation at 3 AM can carry an email, a phone number or a pasted key. Stored text is redacted;
      the consent check reads the words in memory before this runs. */
  function redact(text) {
    let n = 0;
    const out = String(text == null ? "" : text)
      .replace(/\b(?:sk|pk|rk)_(?:live|test)_\w{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_\w{20,}\b|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[\w-]{10,}\b|\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}\b|\b(?:password|passwd|secret|token|api[_ ]?key)\s*(?:is|=|:)\s*\S+/gi, () => (n++, "[secret]"))
      .replace(/\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b/g, () => (n++, "[email]"))
      .replace(/(?:\+?\d[\d -]{8,}\d)/g, (m) => { const d = m.replace(/\D/g, "").length; return d >= 10 && d <= 19 ? (n++, "[number]") : m; });
    return { text: out, redacted: n };
  }

  /** Log reduction: group lines by shape, keep the few that are errors and new since the last healthy snapshot. */
  const logShape = (l) => String(l).replace(/^\S*\d{1,2}:\d{2}(?::\d{2})?\S*\s*/, "").replace(/"[^"]*"|'[^']*'/g, "<s>").replace(/\b[0-9a-f]{8,}\b/gi, "<id>").replace(/\d+(?:\.\d+)*/g, "<n>").trim().slice(0, 200);
  const severity = (l) => (/\b(?:fatal|panic|critical|emerg)/i.test(l) ? 3 : /\b(?:error|err|exception|traceback|refused|timed? ?out|failed|failure|denied|exhausted|typeerror|oom|killed)\b/i.test(l) ? 2 : /\bwarn(?:ing)?\b/i.test(l) ? 1 : 0);
  function reduceLogs(lines, baseline, max) {
    lines = (lines || []).filter((l) => String(l).trim());
    const base = new Set((baseline || []).map(logShape));
    const groups = new Map();
    lines.forEach((l, k) => { const s = logShape(l); const g = groups.get(s) || { line: l, shape: s, count: 0 }; g.count += 1; g.line = l; g.lastAt = k; groups.set(s, g); });
    const scored = Array.from(groups.values()).map((g) => Object.assign(g, { novel: !base.has(g.shape), severity: severity(g.line) }))
      .map((g) => Object.assign(g, { score: g.severity * 3 + (g.novel ? 2 : 0) + g.lastAt / Math.max(1, lines.length) }))
      .filter((g) => g.severity >= 2 || (g.novel && g.severity >= 1));
    // Errors that are new since the service was healthy are the story; old ones only fill in when there are none.
    const fresh = scored.filter((g) => g.novel);
    const kept = (fresh.length ? fresh : scored).sort((x, y) => y.score - x.score).slice(0, max || 3);
    return { total: lines.length, shapes: groups.size, kept };
  }

  /** "db_pool must stay at 20 or more" → a rule WisprOps checks. Runbook notes you dictate become guardrails. */
  const RULE_OPS = { ">=": (a, b) => a >= b, "<=": (a, b) => a <= b, ">": (a, b) => a > b, "<": (a, b) => a < b, "==": (a, b) => a === b, "!=": (a, b) => a !== b };
  const OP_TEXT = { ">=": "at least", "<=": "at most", ">": "above", "<": "below", "==": "exactly", "!=": "never" };
  const SMALL = { zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, fifteen: 15, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90 };
  /** "1,000", "1.5k", "five hundred", "twenty five", "two thousand" → a number (null if it isn't one). */
  function spokenNumber(str) {
    const s = String(str).trim().replace(/(\d),(?=\d{3}\b)/g, "$1");
    let m;
    if ((m = s.match(/^(-?\d+(?:\.\d+)?)\s*(k|thousand|m|million)?$/))) return Number(m[1]) * ({ k: 1e3, thousand: 1e3, m: 1e6, million: 1e6 }[m[2]] || 1);
    let total = 0, cur = 0, any = false;
    for (const w of s.split(/[\s-]+/)) {
      if (w in SMALL) { cur += SMALL[w]; any = true; }
      else if (w === "hundred") { cur = (cur || 1) * 100; any = true; }
      else if (w === "thousand") { total += (cur || 1) * 1000; cur = 0; any = true; }
      else if (w !== "and") return null;
    }
    return any ? total + cur : null;
  }
  const NUMW = "(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)";
  function parseRule(text) {
    const t = deva(text).toLowerCase().replace(/[“”"]/g, "");
    const num = "(-?\\d[\\d,]*(?:\\.\\d+)?(?:\\s*(?:k|thousand))?|" + NUMW + "(?:[\\s-]+(?:and[\\s-]+)?" + NUMW + ")*)";
    const m = t.match(new RegExp("([a-z][\\w.]*(?: [a-z][\\w.]*){0,3}) (?:must|should|has to|needs to|ko)(?: always)?(?: (never|not))? (?:stay |be |remain |go |rehna chahiye )?(?:(at least|no less than|minimum|at most|no more than|maximum|above|over|more than|greater than|below|under|less than|exactly|equal to|set to) )?(?:at )?" + num + "(?: (or more|or higher|or above|or less|or lower|or below|ya zyada|ya kam))?\\b"));
    if (!m) return null;
    const words = m[1].split(" ").filter((w) => !/^(?:the|my|our|a|an|value|setting|key|of|and|so|it|that|this|if|then|after|before|since|now|always)$/.test(w));
    if (!words.length) return null;
    const v = spokenNumber(m[4]);
    if (v == null || Number.isNaN(v)) return null;
    const neg = !!m[2], q = m[3] || "", tail = m[5] || "";
    let op;
    if (/least|no less|minimum/.test(q) || /more|higher|above|zyada/.test(tail)) op = neg ? "<" : ">=";
    else if (/most|no more|maximum/.test(q) || /less|lower|below|kam/.test(tail)) op = neg ? ">" : "<=";
    else if (/above|over|more|greater/.test(q)) op = neg ? "<=" : ">";
    else if (/below|under|less/.test(q)) op = neg ? ">=" : "<";
    else if (neg) op = "!=";
    else if (/exactly|equal|set to/.test(q)) op = "==";
    else return null; // "db_pool should be 20" is too loose to enforce
    // Prefer the words from the one that looks like a config key (db_pool, cache.ttl) onwards.
    const k = words.findIndex((w) => /[_.]/.test(w));
    const key = (k >= 0 ? words.slice(k) : words.slice(-3)).join(" ");
    return { key, words: words.slice(-4), op, value: v, text: `${key} ${OP_TEXT[op]} ${v}` };
  }
  /** Which config key does a spoken rule mean? "db pool" matches db_pool, "pool" matches database.pool.
      Tries the last 4, 3, 2, then 1 spoken words, so "after the migration db pool" still finds db_pool. */
  function ruleKey(rule, config) {
    const keys = Object.keys(config || {});
    const words = rule.words || String(rule.key).split(" ");
    for (let n = Math.min(4, words.length); n >= 1; n--) {
      const want = slugK(words.slice(-n).join(" "));
      const hit = keys.find((k) => slugK(k) === want) || keys.find((k) => slugK(k).endsWith(want) && want.length >= 4);
      if (hit) return hit;
    }
    return null;
  }
  const slugK = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  function breaks(rule, value) { const n = Number(value); return value == null || value === "" || Number.isNaN(n) ? rule.op !== "!=" : !RULE_OPS[rule.op](n, rule.value); }

  /** "Watch my api, it runs in docker container shop-api, health check at localhost 3000 slash healthz,
      config in config slash api dot json, git repo here" → wisprops add flags. Set a service up by voice. */
  function describeService(text) {
    let t = " " + deva(text).replace(/[“”"]/g, " ") + " ";
    t = t.replace(/\s+(slash|forward slash)\s+/gi, "/").replace(/\s+(?:dot)\s+/gi, ".").replace(/\bdot\s+(?=\w)/gi, ".").replace(/\s+colon\s+/gi, ":").replace(/\s+(?:underscore)\s+/gi, "_").replace(/\s+(?:dash|hyphen)\s+/gi, "-");
    // Match without caring about case, but keep what was said: Config/App.json is not config/app.json on Linux.
    const low = { match: (re) => t.match(new RegExp(re.source, re.flags.includes("i") ? re.flags : re.flags + "i")) };
    const STOP = /^(?:called|named|is|on|at|in|the|my|our|a|an|and|it|with|for|port|container|service|process|app|unit|deployment|health|check)$/i;
    const word = (re) => { const m = low.match(re); return m && m[1] && !STOP.test(m[1]) ? m[1].replace(/[.,;]+$/, "") : null; };
    const flags = {}, heard = [];
    let m;
    if ((m = low.match(/\b(https?:\/\/[^\s,;]+)/))) flags.health = m[1].replace(/[.,;]+$/, "");
    else if ((m = low.match(/\b(localhost|127\.0\.0\.1|0\.0\.0\.0)\s*(?::|port|on port|,)?\s*(\d{2,5})\s*(\/[\w/.-]*)?/))) flags.health = `http://${m[1] === "0.0.0.0" ? "localhost" : m[1]}:${m[2]}${(m[3] || "/").replace(/[.,;]+$/, "") || "/"}`;
    else if ((m = low.match(/\bport (\d{2,5})(?:\s*(\/[\w/.-]*))?/))) flags.health = `http://localhost:${m[1]}${(m[2] || "/").replace(/[.,;]+$/, "")}`;
    if (flags.health && /^http:\/\/[^/]+\/?$/.test(flags.health) && /\b(?:tcp|postgres(?:ql)?|redis|mysql|mariadb|mongo(?:db)?|rabbitmq|kafka|memcached|database|socket)\b/i.test(t)) flags.health = flags.health.replace(/^http:/, "tcp:").replace(/\/$/, "");
    if (flags.health) heard.push(`health check ${flags.health}`);
    const presets = [
      ["kubernetes", /\b(?:kubernetes|k8s|kube)(?: deployment)?(?: called| named)? ([\w.-]+)/],
      ["compose", /\bcompose(?: service)?(?: called| named)? ([\w.-]+)/],
      ["pm2", /\bpm ?2(?: process| app)?(?: called| named)? ([\w.-]+)/],
      ["systemd", /\bsystemd(?: unit| service)?(?: called| named)? ([\w.@-]+)/],
      ["docker", /\bdocker(?: container)?(?: called| named)? ([\w.-]+)/],
      ["docker", /\bcontainer(?: called| named)? ([\w.-]+)/],
    ];
    for (const [type, re] of presets) { const v = word(re); if (v) { flags[type] = v; heard.push(`${type} ${v}`); break; } }
    if (flags.kubernetes && (m = low.match(/\bnamespace ([\w.-]+)/))) { flags.namespace = m[1].replace(/[.,;]+$/, ""); heard.push(`namespace ${flags.namespace}`); }
    if ((m = low.match(/\b(?:config(?:uration)?|settings|env)(?: file)?(?: is| lives| sits)?(?: in| at)? ((?:\.{1,2}\/)?[\w./-]*?(?:\.json|\.env[\w.-]*))\b/)) || (m = low.match(/((?:\.{1,2}\/)?[\w/-]*\.env(?:\.[\w-]+)?|(?:\.{1,2}\/)?[\w/-]+\.json)\b/))) { flags.watch = m[1].startsWith(".") || m[1].startsWith("/") ? m[1] : "./" + m[1]; heard.push(`watch ${flags.watch}`); }
    if ((m = low.match(/\blogs?(?: are| file| go)?(?: in| at| to) ((?:\.{1,2}\/)?[\w./-]+\.log)\b/))) { flags.logs = m[1]; heard.push(`logs ${flags.logs}`); }
    if (/\b(?:git|repo|repository)\b/i.test(t)) { m = low.match(/\b(?:git|repo|repository)(?: repo)?(?: is)?(?: in| at) ((?:\.{1,2}\/|\/|~)[\w./-]*)/); flags.git = m ? m[1] : "."; heard.push(`git ${flags.git}`); }
    let id = word(/\b(?:watch|monitor|add|track)(?: my| the| our)? ([\w.-]+(?: (?:api|app|web|site|service|server|worker|frontend|backend|db|database|queue|bot|cron|gateway))?)\b/) || word(/\b(?:called|named) ([\w.-]+)/);
    if (id) id = id.replace(/ /g, "-");
    const runner = flags.docker || flags.compose || flags.pm2 || (flags.systemd || "").replace(/\.service$/, "") || flags.kubernetes;
    if (!id || /^(?:service|app|api service)$/.test(id) || (runner && slugK(runner).startsWith(slugK(id)))) id = runner || id || "api";
    id = id.replace(/[^\w.-]/g, "").slice(0, 60) || "api";
    const warnings = [];
    if (!flags.health) warnings.push("I didn't hear a health check URL. Say “health check at localhost 3000 slash healthz”.");
    if (!["docker", "compose", "pm2", "systemd", "kubernetes"].some((k) => flags[k])) warnings.push("No runner heard (docker, compose, pm2, systemd or kubernetes), so WisprOps can't restart it. It will still watch and page.");
    const q = (v) => (/^[\w./:@-]+$/.test(v) ? v : JSON.stringify(v));
    const cli = `wisprops add ${q(id)}` + Object.entries(flags).map(([k, v]) => ` --${k} ${q(v)}`).join("");
    return { ok: !!flags.health, id, flags, heard, warnings, cli };
  }

  // ---------- say-it-back: approve a fix by saying what it does ----------
  const FIXW = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
  const plain = (v) => (typeof v === "string" ? v : JSON.stringify(v));
  /** The shortest sentence that proves you heard the fix: "approve fix one, db_pool back to 20". */
  function sayBackExample(n, action, svc) {
    const head = `approve fix ${FIXW[n] || n}, `;
    if (action.kind === "restore_config") return head + `${action.key} ${action.rule ? "to" : "back to"} ${plain(action.value)}`;
    if (action.kind === "rollback") return head + `roll ${svc.name} back to ${action.to}`;
    return head + `restart ${svc.name}`;
  }
  const esc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const AFTER_ONE = /^(?:the|previous|last|old|older|healthy|good|that|this|stable|working)$/;
  /** Every number said: "20", "0.5", "twenty five", "v41" (41). "the previous one" is not a 1. */
  function numbersIn(back, rawBack) {
    const out = (String(rawBack || "").match(/\d+(?:\.\d+)?/g) || []).map(Number);
    let run = [];
    const flush = () => { if (run.length) { const v = spokenNumber(run.join(" ")); if (v != null) out.push(v); } run = []; };
    back.split(" ").forEach((w, k, all) => {
      if (w === "one" && !run.length && AFTER_ONE.test(all[k - 1] || "")) { flush(); return; }
      if (w in SMALL || w === "hundred" || w === "thousand") run.push(w); else flush();
    });
    flush();
    return out;
  }
  const digitsOf = (...xs) => xs.flatMap((x) => (x == null ? [] : (String(typeof x === "string" ? x : JSON.stringify(x)).match(/\d+(?:\.\d+)?/g) || []).map(Number)));
  /** Does the sentence name this service? "checkout api", "checkout-api", or its first word ("checkout"). */
  function names(back, svc) {
    const words = norm(svc.name).split(" "), flat = back.replace(/ /g, "");
    return new RegExp("(?:^| )" + esc(words.join(" ")) + "(?: |$)").test(back) || (slug(svc.name).length >= 5 && flat.includes(slug(svc.name))) || (words[0].length >= 4 && new RegExp("(?:^| )" + esc(words[0]) + "(?: |$)").test(back));
  }
  const NOT_FOR = {
    restore_config: /\b(?:restart|restarting|reboot|bounce|redeploy|release|deploy)\b/,
    rollback: /\b(?:restart|restarting|reboot|bounce)\b/,
    restart: /\b(?:roll|rolling|rollback|revert|release|deploy|redeploy|set|config)\b|\bback to\b/,
  };
  /**
   * Check a say-back against the fix on the table. Every fact you name must be right, and the ones that
   * define the fix must be there: the key and its new value, the release to roll back to, or "restart" and
   * the service. Pure: returns { ok, facts: [{ label, ok }], problems: [] }.
   */
  function checkSayBack(p, svc, services, back, rawBack) {
    const a = p.action, facts = [], problems = [];
    back = String(back || ""); const flat = back.replace(/ /g, "");
    const fact = (label, ok, required) => { if (ok || required) facts.push({ label, ok: !!ok }); return ok; };
    let rest = back; // what's left once the key is taken out, so "auth_timeout" doesn't sound like the auth service
    if (a.kind === "restore_config") {
      const key = slugK(a.key), last = slugK(String(a.key).split(/[._-]/).pop());
      const hasKey = flat.includes(key) || (last.length >= 4 && flat.includes(last));
      fact(a.key, hasKey, true);
      rest = back.replace(new RegExp(esc(norm(a.key)), "g"), " ");
      const v = a.value;
      let hasVal;
      if (typeof v === "number") hasVal = numbersIn(back, rawBack).includes(v);
      else if (typeof v === "boolean") hasVal = (v ? /\b(?:true|on|enabled?)\b/ : /\b(?:false|off|disabled?)\b/).test(back);
      else if (v == null) hasVal = /\b(?:remove|delete|unset|drop)\b/.test(back);
      else hasVal = !!slug(v) && flat.includes(slug(v));
      fact(plain(v), hasVal, true);
    } else if (a.kind === "rollback") {
      const to = String(a.to), m = to.match(/^v?(\d+)$/i);
      const hasTo = (slug(to).length >= 2 && flat.includes(slug(to))) || (m && numbersIn(back, rawBack).includes(Number(m[1]))) || (/^[0-9a-f]{7,}$/i.test(to) && flat.includes(to.slice(0, 7).toLowerCase()));
      fact(to, hasTo, true);
    } else {
      fact("restart", /\b(?:restart|restarting|reboot|bounce)\b/.test(back), true);
    }
    fact(svc.name, names(back, svc), a.kind === "restart");
    // Anything else you name has to be right too.
    const other = (services || []).find((s) => s.id !== svc.id && names(rest, s) && !names(rest, svc));
    if (other) problems.push(`you said ${other.name}, but fix ${p.n} is for ${svc.name}`);
    const allowed = digitsOf(a.value, a.from, a.to, a.key, svc.name, svc.id, svc.configLabel);
    if (a.kind === "rollback") { const m = String(a.to).match(/^v?(\d+)$/i); if (m) allowed.push(Number(m[1])); }
    const wrong = numbersIn(rest, rawBack.replace(new RegExp(esc(String(a.key || "\u0000")), "gi"), " ")).filter((x) => !allowed.includes(x));
    if (wrong.length) problems.push(`you said ${wrong.join(", ")}, but fix ${p.n} ${a.kind === "restore_config" ? `sets ${a.key} to ${plain(a.value)}` : a.kind === "rollback" ? `rolls back to ${a.to}` : `restarts ${svc.name}`}`);
    const verb = rest.match(NOT_FOR[a.kind]);
    if (verb) problems.push(`you said “${verb[0]}”, but fix ${p.n} ${a.kind === "restore_config" ? `only changes ${a.key}` : a.kind === "rollback" ? "is a rollback, not a restart" : "only restarts it"}`);
    // A wrong number already explains a missing value; don't say it twice.
    const missing = facts.filter((f) => !f.ok && !(wrong.length && (f.label === plain(a.value) || f.label === String(a.to)))).map((f) => f.label);
    if (missing.length) problems.push(`I didn't hear ${missing.join(" or ")}`);
    return { ok: !problems.length, facts, problems };
  }

  // ---------- debrief: a long dictation becomes the postmortem ----------
  const NOT_NAME = /^(?:We|I|You|They|Team|It|This|That|The|There|He|She|Someone|Somebody|Everyone|Nobody|Then|Also|So|And|But|Next|Going|Our)$/;
  const DUE = /\b(?:by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|eod|end of (?:the )?(?:day|week|sprint)|next week)|this week|today|tomorrow|before (?:the )?next (?:deploy|release))\b/i;
  const MODAL = "(?:should|will|ll|need to|needs to|must|has to|have to|is going to|are going to|is gonna|are gonna|gonna|is adding|is fixing|is writing|owns|takes)";
  /** "Sam will add a CI check by Friday" → { owner: "Sam", text: "add a CI check", due: "by Friday" }. Pure. */
  function parseDebrief(text) {
    const src = deva(String(text || "")).replace(/\s+/g, " ").trim();
    const parts = src.split(/(?<=[.!?])\s+|;\s*|\s+(?:and also|also,)\s+/i).map((s) => s.trim().replace(/^(?:and|so|also|then|plus)\s*,?\s+/i, "")).filter((s) => s.replace(/\W/g, "").length > 2);
    const out = { actions: [], causes: [], good: [], context: [], rules: [], words: src ? src.split(" ").length : 0 };
    const tidy = (s) => s.replace(/\s+/g, " ").replace(/^[\s,:-]+|[\s,.;:!-]+$/g, "");
    for (const s of parts) {
      const rule = parseRule(s);
      if (rule) { out.rules.push({ text: s, rule }); continue; }
      const lead = s.replace(/^(?:(?:ok(?:ay)?|so|and|then|also)[:,]?\s+)+/i, "");
      const due = (s.match(DUE) || [""])[0];
      const act = (owner, what) => out.actions.push({ owner, text: tidy(what.replace(DUE, "")), due: due ? due.replace(/^\w/, (c) => c.toLowerCase()) : null });
      let m;
      // A person (a capitalised name, I, we) and what they will do. Names are case-sensitive: "deploy-bot" is not a person.
      if ((m = lead.match(new RegExp("^([A-Z][a-z][\\w-]*|I|[Ww]e|[Yy]ou|[Tt]hey|[Tt]he team)\\s*(?:'|’)?\\s*" + MODAL + "\\s+(.+)$")))) {
        const who = m[1];
        act(who === "I" ? "me" : /^(?:we|you|they|the team)$/i.test(who) || NOT_NAME.test(who) ? "team" : who, m[2]);
      } else if ((m = lead.match(/^(?:let(?:'|’)?s|action item:?|todo:?|to-do:?|follow[- ]up:?)\s+(.+)$/i))) act("team", m[1]);
      else if (/^.{3,60}?\s(?:should|needs to|need to|must|has to|have to)\s+\S/i.test(lead)) act("team", lead);
      else if (/\b(?:because|caused|due to|root cause|the reason|turned out|that'?s why|which (?:set|made|broke|meant|took)|led to|triggered|ran the|happened when)\b/i.test(s)) out.causes.push(tidy(s));
      else if (/\b(?:went well|worked|helped|saved us|good thing|glad|luckily|nobody was woken|caught it)\b/i.test(s)) out.good.push(tidy(s));
      else out.context.push(tidy(s));
    }
    return out;
  }

  /** A approval record: one receipt as a document someone can read six weeks later. */
  function certificate(r, S) {
    if (!r) return "";
    const ch = (S && S.changes || []).filter((c) => c.incidentId === r.incidentId && c.source !== "wisprops");
    return [
      `# Approval record ${r.id}`, "",
      `**Words:** “${r.heard}”`,
      `**Heard through:** ${r.channel}${r.flagged ? " (flagged: typed, allowed by config)" : ""}`,
      r.meta ? `**How it arrived:** ${r.meta.via === "sim" ? "simulated Wispr dictation (demo)" : `${r.meta.insert || 0} characters in one insertion, ${r.meta.keys || 0} keystrokes`}` : "",
      r.saidBack ? `**Said back:** ${r.saidBack.map((f) => f.label + " ✓").join(" · ")} (checked against the fix before it ran)` : "",
      `**When:** ${new Date(r.at).toISOString()} · ${r.service} · ${r.incidentId}`,
      `**Allowlisted action:** \`${r.action ? r.action.kind : "?"}\` ${r.action ? "`" + JSON.stringify(r.action) + "`" : ""}`,
      `**Change:** ${r.describe}`,
      r.dryRun ? `**Dry run:** ${r.dryRun}` : "",
      r.cause ? `**Cause:** ${r.cause}` : "",
      ch.length ? `**Change behind it:** ${ch.map((c) => `${c.text}${c.by ? ` (by ${c.by})` : ""}`).join("; ")}` : "",
      r.checks ? `**Verified by:** ${r.checks.healthyInARow} healthy checks in a row · ${r.checks.postCondition}` : "**Verified by:** not verified",
      `**Outcome:** ${r.outcome}`,
      r.digest ? `**SHA-256 of the record:** \`${r.digest}\`` : "",
      "", "_The digest shows whether this record was edited. It is not a signature. The Wispr check proves the words arrived as one dictation, not key by key; pasted text looks the same._",
    ].filter((x) => x !== "").join("\n") + "\n";
  }

  /**
   * Was this message dictated with Wispr Flow?
   * meta.insert: the largest single insertion into the Talk box (Wispr drops text in as one burst)
   * meta.keys: characters that arrived one keystroke at a time
   * meta.via: "wispr" | "typed" | "sim" (scripted demo, labelled as such everywhere)
   */
  function consent(meta, phraseLen, settings, said) {
    meta = meta || {};
    if (meta.via === "sim") return { ok: true, channel: "simulated Wispr" };
    const insert = Number(meta.insert) || 0, keys = Number(meta.keys) || 0;
    // The whole utterance has to arrive in one go. A short form ("unfreeze") counts if all of it did.
    const need = said ? Math.max(4, Math.min(phraseLen, String(said).trim().length - 2)) : phraseLen;
    if (insert >= need && keys <= 1) return { ok: true, channel: "Wispr Flow" };
    if (settings && settings.allowTyped) return { ok: true, channel: "typed (allowed by config)", flagged: true };
    return { ok: false, reason: keys > 1 ? `That arrived as ${keys} keystrokes, not a Wispr Flow dictation. Hold your Wispr key and say it.` : "Only part of that arrived as one Wispr Flow dictation. Hold your Wispr key and say the whole phrase in one go." };
  }

  // ---------- helpers ----------
  const J = (v) => JSON.stringify(v);
  const same = (a, b) => J(a) === J(b);
  function diffConfig(golden, current) {
    if (!golden || !current) return [];
    const keys = new Set(Object.keys(golden).concat(Object.keys(current)));
    const out = [];
    keys.forEach((k) => { if (k !== "__unreadable" && !same(golden[k], current[k])) out.push({ key: k, golden: golden[k], current: current[k] }); });
    return out;
  }
  function ago(ms) { const s = Math.round(ms / 1000); return s < 90 ? s + "s" : s < 5400 ? Math.round(s / 60) + " min" : Math.round(s / 3600) + " h"; }
  function fmtClock(t) { const d = new Date(t); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

  const ACTIONS = {
    restore_config: {
      describe: (a, svc) => a.rule ? `Set ${a.key} to ${J(a.value)} in ${svc.configLabel || "the config"}, the edge of your rule “${a.rule}” (it is ${J(a.from)} now).` : `Set ${a.key} back to ${J(a.value)} in ${svc.configLabel || "the config"} (it is ${J(a.from)} now).`,
      blast: (a, svc) => `One key in one file for ${svc.name}. No restart, no other service touched.`,
      inverse: (a) => ({ kind: "restore_config", key: a.key, value: a.from, from: a.value }),
    },
    rollback: {
      describe: (a, svc) => `Roll ${svc.name} back from release ${a.from} to ${a.to}.`,
      blast: (a, svc) => `${svc.name} only. It serves the previous release until a new deploy.`,
      inverse: (a) => ({ kind: "rollback", to: a.from, from: a.to }),
    },
    restart: {
      describe: (a, svc) => a.remedy ? `Restart ${svc.name}. This is a best guess, not a root cause: nothing I can see explains the failure.` : `Restart ${svc.name}.`,
      blast: (a, svc) => `${svc.name} only. A few seconds of dropped requests while it comes back.`,
      inverse: () => null,
    },
  };
  const ALLOWED = Object.keys(ACTIONS);
  const CLOSED = ["resolved", "recovered on its own", "handed back", "stood down"];
  const OPEN = ["triage", "proposed", "applying", "verifying", "escalated"];

  function emptyState() {
    return { seq: 0, svc: {}, incidents: [], receipts: [], orders: [], notes: [], runbooks: {}, snoozes: {}, ordersPaused: false, frozen: null, changes: [], activity: [], settings: {} };
  }
  const ORD = (k) => k + (k % 100 >= 11 && k % 100 <= 13 ? "th" : ["th", "st", "nd", "rd"][k % 10] || "th");

  // ---------- agent ----------
  function createAgent(opts) {
    const cfg = opts.config;
    const A = opts.adapters;
    const S = Object.assign(emptyState(), opts.state || {});
    const set = Object.assign({ failAfter: 2, goldenAfter: 3, verifyOk: 3, verifyWindowMs: 60000, allowTyped: false, sayItBack: true, escalateAfterMin: 15, maxOrderDays: 30, maxOrderUses: 10, keepIncidents: 500, applyEnabled: true, nightFrom: 22, nightTo: 8 }, cfg.settings || {});
    S.changes = S.changes || []; S.frozen = S.frozen || null;
    const now = () => (A.now ? A.now() : Date.now());
    const listeners = new Set();
    const emit = (ev) => listeners.forEach((fn) => { try { fn(ev, S); } catch (e) { /* listener errors stay local */ } });
    const svcOf = (id) => cfg.services.find((s) => s.id === id);
    const rt = (id) => (S.svc[id] = S.svc[id] || { status: "unknown", fails: 0, okStreak: 0, last: null, golden: null, history: [] });
    const openFor = (id) => S.incidents.find((i) => i.serviceId === id && !CLOSED.includes(i.status));
    const inc = (id) => S.incidents.find((i) => i.id === id);
    const pending = (i) => i.proposals.find((p) => p.state === "pending");
    const snoozedUntil = (id) => (S.snoozes[id] && S.snoozes[id].until > now() ? S.snoozes[id].until : 0);

    // What you said is stored redacted (emails, numbers, keys). Consent was already checked on the words in memory.
    const clean = (who, text) => (who === "you" ? redact(text).text : text);
    function log(i, who, text, extra) { i.timeline.push(Object.assign({ at: now(), who, text: clean(who, text) }, extra || {})); emit({ type: "timeline", incidentId: i.id }); }
    function activity(who, text, extra) { S.activity.push(Object.assign({ at: now(), who, text: clean(who, text) }, extra || {})); if (S.activity.length > 300) S.activity.splice(0, S.activity.length - 300); }
    async function save() { if (A.save) await A.save(S); emit({ type: "state" }); }

    // ---------- change ledger: what changed, when, and who changed it ----------
    function recordChange(serviceId, c) {
      const svc = svcOf(serviceId);
      const e = Object.assign({ id: "chg-" + now().toString(36) + Math.random().toString(36).slice(2, 5), at: now(), serviceId, service: svc ? svc.name : serviceId }, c);
      e.text = String(e.text || "").slice(0, 300); if (e.by) e.by = String(e.by).slice(0, 80);
      S.changes.push(e);
      if (S.changes.length > 500) S.changes.splice(0, S.changes.length - 500);
      return e;
    }
    /** A change reported by CI, a deploy script, a teammate, or the demo. */
    async function reportChange(serviceRef, info) {
      const svc = svcOf(serviceRef) || findService(serviceRef);
      if (!svc) return { ok: false, reason: "unknown service" };
      info = info || {};
      const e = recordChange(svc.id, { kind: ["config", "release", "deploy", "infra", "other"].includes(info.kind) ? info.kind : "deploy", text: redact(info.text || "change").text, by: info.by ? redact(info.by).text : null, source: String(info.source || "reported").slice(0, 40), key: info.key ? String(info.key).slice(0, 120) : undefined, version: info.version ? String(info.version).slice(0, 120) : undefined });
      await save();
      return { ok: true, id: e.id };
    }
    const changesFor = (id, since) => S.changes.filter((c) => c.serviceId === id && c.at >= (since || 0));

    // ---------- freeze: one switch that stops every write ----------
    function frozenWhy() {
      if (set.applyEnabled === false) return "Changes are switched off in the config (settings.applyEnabled is false).";
      if (S.frozen) return `Production is frozen (since ${fmtClock(S.frozen.at)}, “${S.frozen.by}”).`;
      return null;
    }

    // ---------- rules you dictated in runbook notes ----------
    function rulesFor(id) { return (S.runbooks[id] || []).filter((n) => n.rule).map((n) => Object.assign({ note: n }, n.rule)); }
    function ruleBreaks(id, config) {
      if (!config || config.__unreadable) return [];
      return rulesFor(id).map((r) => ({ r, key: ruleKey(r, config) })).filter((x) => x.key && breaks(x.r, config[x.key]))
        .map((x) => ({ key: x.key, value: config[x.key], limit: x.r.value, op: x.r.op, text: x.r.text, note: x.r.note }));
    }

    function findService(target) {
      const t = slug(target);
      if (!t) return null;
      const exact = cfg.services.find((s) => slug(s.id) === t || slug(s.name) === t);
      if (exact) return exact;
      const loose = cfg.services.filter((s) => t.length >= 3 && (slug(s.name).startsWith(t) || slug(s.id).startsWith(t) || t.startsWith(slug(s.id))));
      return loose.length === 1 ? loose[0] : null;
    }

    async function snapshot(svc, refreshLogs) {
      const r = rt(svc.id);
      let config = A.readConfig ? await A.readConfig(svc) : null;
      if (config && config.__unreadable) return;
      const version = A.version ? await A.version(svc) : null;
      const g = r.golden;
      // The last-healthy snapshot never takes in a value that breaks one of your dictated rules:
      // that one key keeps its last good value; everything else (other keys, the release) moves on.
      const bad = ruleBreaks(svc.id, config);
      if (bad.length) {
        const sig = bad.map((b) => b.key + "=" + J(b.value)).join(",");
        if (r.ruleWarned !== sig) { r.ruleWarned = sig; activity("agent", `${svc.name} is healthy, but ${bad.map((b) => `${b.key} is ${J(b.value)} (your rule: ${b.text})`).join("; ")}. I won't take that into the last healthy snapshot.`); }
        if (g && g.config && config) {
          const kept = Object.assign({}, config);
          bad.forEach((b) => { if (Object.prototype.hasOwnProperty.call(g.config, b.key)) kept[b.key] = g.config[b.key]; else delete kept[b.key]; });
          config = kept;
        }
      } else r.ruleWarned = null;
      if (!g || !same(g.config, config) || g.version !== version) {
        if (g && g.version && g.version !== version) {
          r.history.push(g.version); if (r.history.length > 20) r.history.shift();
          const by = A.whoChanged ? await A.whoChanged(svc, { kind: "release", version }) : null;
          if (!changesFor(svc.id, g.at).some((c) => c.version === version)) recordChange(svc.id, { kind: "release", version, text: `Release ${g.version} → ${version} (healthy)`, by, source: "observed" });
        }
        if (g && g.config && config && !same(g.config, config)) {
          const d = diffConfig(g.config, config).filter((x) => !changesFor(svc.id, g.at).some((c) => c.key === x.key));
          if (d.length) recordChange(svc.id, { kind: "config", key: d[0].key, text: `${d.map((x) => `${x.key} ${J(x.golden)} → ${J(x.current)}`).join(", ")} (healthy)`, by: A.whoChanged ? await A.whoChanged(svc, { kind: "config", key: d[0].key }) : null, source: "observed" });
        }
        let logBase = r.golden && r.golden.logBase;
        if (A.logs && (!logBase || !g || g.version !== version)) { try { logBase = (await A.logs(svc)) || []; } catch (e) { logBase = []; } }
        r.golden = { config, version, at: now(), logBase: (logBase || []).slice(-40) };
      }
      r.golden.seen = now(); // last time this snapshot was confirmed healthy
      if (refreshLogs && A.logs) { try { r.golden.logBase = ((await A.logs(svc)) || []).slice(-40); } catch (e) { /* keep the old baseline */ } }
    }

    async function diagnose(svc, probe) {
      const r = rt(svc.id), g = r.golden || {};
      const config = A.readConfig ? await A.readConfig(svc) : null;
      const version = A.version ? await A.version(svc) : null;
      const logs = A.logs ? await A.logs(svc) : [];
      const evidence = [];
      if (probe.alert) evidence.push({ kind: "alert", text: `${probe.alert.source} alert: ${probe.alert.summary}` });
      evidence.push({ kind: "probe", text: probe.alertOnly ? `Health check passes (HTTP ${probe.status}).` : probe.error ? `Health check failed: ${probe.error}.` : `Health check returned HTTP ${probe.status}${probe.detail ? ": " + probe.detail : ""}.` });
      if (config && config.__unreadable) evidence.push({ kind: "config", text: `The config file could not be read: ${config.__unreadable}.` });
      const drift = config && config.__unreadable ? [] : diffConfig(g.config, config);
      if (g.config) evidence.push({ kind: "config", text: drift.length ? `Config differs from the last healthy snapshot (${ago(now() - g.at)} ago): ${drift.map((d) => `${d.key} ${J(d.golden)} → ${J(d.current)}`).join(", ")}.` : "Config matches the last healthy snapshot." });
      if (g.version) evidence.push({ kind: "release", text: version !== g.version ? `Release changed since the last healthy check: ${g.version} → ${version}.` : `Release unchanged (${version}).` });

      // Change ledger: record what we just found, and attribute it to whoever reported or made it.
      const since = g.seen || g.at || now() - 2 * 3600000;
      const ledger = () => changesFor(svc.id, since).filter((c) => c.source !== "wisprops");
      const who = async (what) => {
        // Only an exact match names someone: a deploy that happened nearby is listed as evidence, never blamed.
        const rep = ledger().filter((c) => c.by && ((what.key && c.key === what.key) || (what.version && c.version === what.version))).pop();
        if (rep) return rep.by;
        return A.whoChanged ? await A.whoChanged(svc, what) : null;
      };
      let by = null;
      if (drift.length) {
        const d = drift[0];
        by = await who({ kind: "config", key: d.key });
        if (!ledger().some((c) => c.key === d.key && c.source === "detected")) recordChange(svc.id, { kind: "config", key: d.key, text: drift.map((x) => `${x.key} ${J(x.golden)} → ${J(x.current)}`).join(", "), by, source: "detected" });
      } else if (g.version && version && version !== g.version) {
        by = await who({ kind: "release", version });
        if (!ledger().some((c) => c.version === version && c.source === "detected")) recordChange(svc.id, { kind: "release", version, text: `Release ${g.version} → ${version}`, by, source: "detected" });
      }
      const recent = ledger().slice(-4);
      if (recent.length) evidence.push({ kind: "changes", text: "Change ledger since the last healthy snapshot: " + recent.map((c) => `${fmtClock(c.at)} ${c.text}${c.by ? ` by ${c.by}` : ""}${c.source && !/detected|observed/.test(c.source) ? ` (${c.source})` : ""}`).join("; ") + "." });

      // Log reduction: the few lines that are errors and new since the service was last healthy.
      let reduced = null;
      if (logs && logs.length) {
        reduced = reduceLogs(logs, g.logBase, 3);
        if (reduced.kept.length) evidence.push({ kind: "logs", text: `${reduced.kept.length === 1 ? "The log line that matters" : "The " + reduced.kept.length + " log lines that matter"} (of ${reduced.total}): ` + reduced.kept.map((k) => `${k.line}${k.count > 1 ? ` ×${k.count}` : ""}${k.novel ? " (new since healthy)" : ""}`).join(" | ") });
        else evidence.push({ kind: "logs", text: `No error lines in the last ${reduced.total} log lines.` });
      }
      const bad = ruleBreaks(svc.id, config);
      bad.forEach((b) => evidence.push({ kind: "rule", text: `Your rule for ${svc.name} (${b.note.via}, ${new Date(b.note.at).toLocaleDateString()}): “${b.note.text}”. ${b.key} is ${J(b.value)} now, which breaks it.` }));
      const notes = (S.runbooks[svc.id] || []).filter((n) => !bad.some((b) => b.note === n)).slice(-2);
      notes.forEach((n) => evidence.push({ kind: "runbook", text: `Your runbook note for ${svc.name}: “${n.text}”` }));
      const byTxt = by ? ` Changed by ${by}.` : "";
      const out = (cause, action) => ({ evidence, cause, action, logs: reduced });

      if (drift.length) {
        const d = drift[0];
        return out({ kind: "config_drift", key: d.key, by, summary: `${svc.name} broke after ${d.key} changed from ${J(d.golden)} to ${J(d.current)}.${byTxt}${drift.length > 1 ? ` ${drift.length - 1} other key${drift.length > 2 ? "s" : ""} also changed; one fix at a time.` : " Every other key matches the last healthy snapshot."}` },
          { kind: "restore_config", key: d.key, value: d.golden, from: d.current });
      }
      if (g.version && version && version !== g.version) {
        return out({ kind: "bad_deploy", key: version, by, summary: `${svc.name} started failing after release ${version} replaced ${g.version}.${byTxt} Config is unchanged.` },
          { kind: "rollback", to: g.version, from: version });
      }
      // Nothing drifted, but a value breaks a rule you dictated: put it back to the edge of your rule.
      const fixable = bad.find((b) => [">=", "<=", "=="].includes(b.op));
      if (fixable && !probe.alertOnly && !probe.error) {
        return out({ kind: "rule_broken", key: fixable.key, summary: `${svc.name} is failing and ${fixable.key} is ${J(fixable.value)}, which breaks your rule “${fixable.text}”. The last healthy snapshot had the same value, so it was wrong before it broke.` },
          { kind: "restore_config", key: fixable.key, value: typeof fixable.value === "string" ? String(fixable.limit) : fixable.limit, from: fixable.value, rule: fixable.text });
      }
      if (probe.alertOnly) {
        return out({ kind: "external_alert", key: "", summary: `${probe.alert.source} says ${svc.name} is unhealthy, but its health check passes and nothing changed since the last healthy snapshot. None of the allowlisted fixes fits, so this needs a human.` }, null);
      }
      if (probe.error) {
        return out({ kind: "process_down", key: "", summary: `${svc.name} is not answering (${probe.error}). Config and release match the last healthy state, so the process itself is down.` },
          { kind: "restart" });
      }
      return out({ kind: "errors", key: String(probe.status || ""), summary: `${svc.name} is returning HTTP ${probe.status} with no config or release change. Nothing I can see explains it, so the only move I have is a restart: a best guess, not a root cause.` },
        { kind: "restart", remedy: true });
    }

    async function propose(i, action) {
      const svc = svcOf(i.serviceId);
      if (!ALLOWED.includes(action.kind)) { await escalate(i, `Refusing ${action.kind}: it is not on the allowlist.`); return null; }
      if (action.kind === "restore_config") {
        const v = rulesFor(svc.id).find((r) => slugK(r.key) && (ruleKey(r, { [action.key]: 1 }) === action.key) && breaks(r, action.value));
        if (v) { await escalate(i, `Putting ${action.key} back to ${J(action.value)} would break your rule “${v.text}”, so I won't propose it.`); return null; }
      }
      const dry =A.dryRun ? await A.dryRun(svc, action) : { ok: true, text: "No dry run available." };
      if (!dry.ok) {
        // Say what's wrong first, then why WisprOps can't fix it, so the page is useful on its own.
        const why = /not configured|no \w+ command configured|no config file configured/.test(dry.text) ? `I can't fix it from here: ${dry.text}.` : `The fix would be: ${ACTIONS[action.kind].describe(action, svc)} But its dry run failed (${dry.text}), so nothing will be applied.`;
        await escalate(i, `${i.cause ? i.cause.summary + " " : ""}${why}`); return null;
      }
      const n = i.proposals.length + 1;
      if (!/[.!?]$/.test(dry.text)) dry.text += ".";
      const sayBack = sayBackExample(n, action, svc);
      const p = { n, action, dryRun: dry.text, describe: ACTIONS[action.kind].describe(action, svc), blast: ACTIONS[action.kind].blast(action, svc), phrase: set.sayItBack === false ? `approve fix ${n}` : sayBack, sayBack, state: "pending", at: now() };
      i.proposals.push(p);
      i.status = "proposed";
      // When a standing order will apply it on its own, don't ask for a yes that isn't needed.
      const covered = matchOrder(i, p) && !frozenWhy() && !S.ordersPaused;
      log(i, "agent", `Fix ${n}: ${p.describe} Dry run: ${dry.text} Blast radius: ${p.blast} ${covered ? "A standing order covers this, so I'll apply it myself." : (set.sayItBack === false ? `Say “approve fix ${n}” with Wispr Flow to apply it.` : `To apply it, say it back in one Wispr Flow dictation: “${p.phrase}”.`)}`, { proposal: n });
      return p;
    }
    const readback = (p) => `Fix ${p.n}. ${p.describe} Blast radius: ${p.blast} To apply it, say: ${p.phrase}.`;

    function signature(i) { return [i.serviceId, i.cause.kind, i.cause.kind === "config_drift" || i.cause.kind === "rule_broken" ? i.cause.key : ""].join("|"); }
    const atNight = (o) => { const h = new Date(now()).getHours(), f = o.window.from, t = o.window.to; return f > t ? h >= f || h < t : h >= f && h < t; };
    function matchOrder(i, p, ignoreWindow) {
      const t = now();
      return S.orders.find((o) => !o.revoked && o.signature === signature(i) && o.expires > t && o.usesLeft > 0 && o.action.kind === p.action.kind && !p.action.remedy &&
        (p.action.kind !== "restore_config" || (o.action.key === p.action.key && same(o.action.value, p.action.value))) && (ignoreWindow || !o.window || atNight(o)));
    }
    /** Have we seen this exact failure before? Memory across nights. */
    function recurrence(i) {
      const sig = signature(i);
      const past = S.incidents.filter((x) => x !== i && x.cause && x.opened > now() - 7 * 86400000 && signature(x) === sig);
      if (!past.length) return null;
      const last = past[past.length - 1];
      const fix = last.proposals.find((p) => p.state === "applied" || p.state === "undone");
      const rc = fix && S.receipts.find((r) => r.id === "rcpt-" + last.n + "-" + fix.n);
      const day = new Date(last.opened).toLocaleDateString([], { weekday: "short" });
      return { count: past.length + 1, lastId: last.id,
        text: `I've seen this before: the ${ORD(past.length + 1)} time in 7 days that ${i.service} broke this way. Last time (#${last.n}, ${day} ${fmtClock(last.opened)}) ${fix ? `fix ${fix.n} ${fix.state === "undone" ? "was applied and then undone" : "worked"}: ${fix.describe}${rc ? ` Approved by “${rc.heard}” via ${rc.channel}.` : ""}` : `it ended “${last.status}”.`}` };
    }

    async function page(i, why) {
      i.woken = true; i.pagedAt = i.pagedAt || now();
      const svc = svcOf(i.serviceId);
      const p = pending(i);
      const fz = frozenWhy();
      if (A.notify) await A.notify({ kind: "page", title: `${svc.name} is down`, body: (why || i.cause.summary) + (p ? ` Fix ${p.n}: ${p.describe}` : "") + (fz ? ` ${fz}` : ""), incidentId: i.id, phrase: p ? p.phrase : null });
      log(i, "system", "Paged you: " + (why || i.cause.summary));
    }
    async function escalate(i, why) {
      i.status = "escalated";
      log(i, "agent", why + " Handing this to you.");
      if (!i.woken) await page(i, why);
    }

    async function openIncident(svc, probe) {
      const r = rt(svc.id);
      const i = { id: "inc-" + (++S.seq), n: S.seq, serviceId: svc.id, service: svc.name, opened: now(), status: "triage", alarm: probe, timeline: [], proposals: [], receipts: [], woken: false, orderId: null, source: probe.alert ? probe.alert.source : "health check" };
      S.incidents.push(i);
      trim();
      log(i, "system", probe.alert ? `${probe.alert.source} alert for ${svc.name}: ${probe.alert.summary}` : `${svc.name} failed ${r.fails} health checks in a row (${probe.error || "HTTP " + probe.status}).`);
      const d = await diagnose(svc, probe);
      i.evidence = d.evidence; i.cause = d.cause; i.logs = d.logs ? d.logs.kept.map((k) => ({ line: k.line, count: k.count, novel: k.novel })) : null;
      i.changes = changesFor(svc.id, (rt(svc.id).golden || {}).seen || (rt(svc.id).golden || {}).at || now() - 7200000).filter((c) => c.source !== "wisprops").map((c) => c.id);
      changesFor(svc.id, 0).filter((c) => i.changes.includes(c.id) && !c.incidentId).forEach((c) => (c.incidentId = i.id));
      log(i, "agent", "Root cause: " + d.cause.summary, { evidence: d.evidence });
      const rec = recurrence(i);
      if (rec) { i.recurrence = { count: rec.count, lastId: rec.lastId }; i.evidence.push({ kind: "memory", text: rec.text }); log(i, "agent", rec.text); }
      if (!d.action) { await escalate(i, "No allowlisted fix fits this."); return i; }
      const p = await propose(i, d.action);
      if (!p) return i;
      const o = matchOrder(i, p);
      const outside = !o && matchOrder(i, p, true);
      if (o && frozenWhy()) {
        log(i, "agent", `Standing order ${o.id} would cover this, but ${frozenWhy()} Paging you instead.`);
        await page(i);
      } else if (o && S.ordersPaused) {
        log(i, "agent", `Standing order ${o.id} would cover this, but standing orders are paused. Paging you instead.`);
        await page(i);
      } else if (o) {
        i.orderId = o.id; o.usesLeft -= 1; o.used = (o.used || 0) + 1;
        log(i, "agent", `Standing order ${o.id} covers this exact failure (granted ${new Date(o.granted).toLocaleDateString()} by your words “${o.said}”). Applying fix ${p.n} without waking you. ${o.usesLeft ? `${o.usesLeft} use${o.usesLeft === 1 ? "" : "s"} left.` : "That was its last use; the next one pages you unless you renew it."}`);
        await apply(i, p, { ok: true, channel: "standing order " + o.id }, o.said);
      } else {
        if (outside) log(i, "agent", `Standing order ${outside.id} only covers nights (${String(outside.window.from).padStart(2, "0")}:00–${String(outside.window.to).padStart(2, "0")}:00), so I'm asking you.`);
        await page(i);
      }
      return i;
    }

    function trim() {
      const keep = set.keepIncidents;
      if (S.incidents.length <= keep) return;
      const drop = S.incidents.filter((x) => CLOSED.includes(x.status)).slice(0, S.incidents.length - keep).map((x) => x.id);
      S.incidents = S.incidents.filter((x) => !drop.includes(x.id));
    }

    async function digest(obj) { return A.digest ? A.digest(J(obj)) : null; }

    async function apply(i, p, c, heard) {
      const svc = svcOf(i.serviceId);
      const dry2 = A.dryRun ? await A.dryRun(svc, p.action) : { ok: true, text: "" };
      if (!dry2.ok) { p.state = "refused"; await escalate(i, `Second dry run failed just before applying: ${dry2.text}`); return; }
      const fz = frozenWhy();
      if (fz) { log(i, "agent", `${fz} Fix ${p.n} was not applied.`); return; }
      p.state = "approved"; i.status = "applying";
      log(i, "agent", `Applying fix ${p.n}.`);
      const res = await A.exec(svc, p.action);
      if (!res.ok) { p.state = "failed"; await escalate(i, `Fix ${p.n} failed to apply: ${res.output}`); return; }
      p.state = "applied"; p.appliedAt = now();
      recordChange(svc.id, { kind: p.action.kind === "rollback" ? "release" : p.action.kind === "restart" ? "other" : "config", key: p.action.key, version: p.action.to, text: p.describe, by: "WisprOps (" + c.channel + ")", source: "wisprops", incidentId: i.id });
      const receipt = { id: "rcpt-" + i.n + "-" + p.n, incidentId: i.id, service: svc.name, at: now(), heard, phrase: p.phrase, channel: c.channel, flagged: !!c.flagged,
        meta: c.meta || null, saidBack: c.saidBack || null, action: p.action, describe: p.describe, dryRun: p.dryRun, cause: i.cause.summary, checks: null, outcome: "verifying", orderId: i.orderId };
      S.receipts.push(receipt); i.receipts.push(receipt.id);
      // A restart gets a longer verify budget than a config key: the process has to boot.
      i.status = "verifying"; i.verify = { proposal: p.n, since: now(), okStreak: 0, deadline: now() + set.verifyWindowMs * (p.action.kind === "restart" ? 2 : 1) };
      log(i, "agent", `Applied. ${res.output || ""} Now proving it: I need ${set.verifyOk} healthy checks in a row and the post-condition to hold.`);
    }

    async function postCondition(svc, action) {
      if (action.kind === "restore_config" && A.readConfig) { const c = await A.readConfig(svc); return { ok: same(c && c[action.key], action.value), text: `${action.key} is ${J(c && c[action.key])}` }; }
      if (action.kind === "rollback" && A.version) { const v = await A.version(svc); return { ok: v === action.to, text: `release is ${v}` }; }
      return { ok: true, text: "process answering" };
    }

    async function verifyStep(i) {
      const svc = svcOf(i.serviceId), r = rt(svc.id), v = i.verify;
      const p = i.proposals.find((x) => x.n === v.proposal);
      if (r.last && r.last.ok && r.last.at >= v.since) v.okStreak += 1; else if (r.last && r.last.at >= v.since) v.okStreak = 0;
      const post = await postCondition(svc, p.action);
      const receipt = S.receipts.find((x) => x.id === "rcpt-" + i.n + "-" + p.n);
      if (v.okStreak >= set.verifyOk && post.ok) {
        i.status = "resolved"; i.resolvedAt = now(); i.mttr = i.resolvedAt - i.opened;
        if (receipt) { receipt.checks = { healthyInARow: v.okStreak, postCondition: post.text, at: now() }; receipt.outcome = "recovered"; receipt.digest = await digest(Object.assign({}, receipt, { digest: undefined })); }
        if (i.orderId) {
          log(i, "agent", `Recovered in ${ago(i.mttr)} under your standing order. ${set.verifyOk} healthy checks, ${post.text}. You were not woken.`);
        } else {
          log(i, "agent", `Recovered in ${ago(i.mttr)}: ${set.verifyOk} healthy checks in a row and ${post.text}. If this exact failure happens again, I can fix it myself: say “grant standing order for seven days”.`, { offerOrder: true });
        }
        if (i.woken && A.notify) await A.notify({ kind: "resolved", title: `${svc.name} recovered`, body: `Verified in ${ago(i.mttr)}: ${p.describe} You can go back to sleep.`, incidentId: i.id });
        await snapshot(svc, true);
        return;
      }
      if (now() > v.deadline) {
        if (receipt) { receipt.checks = { healthyInARow: v.okStreak, postCondition: post.text, at: now() }; receipt.outcome = "did not recover"; receipt.digest = await digest(Object.assign({}, receipt, { digest: undefined })); }
        await escalate(i, `Fix ${p.n} applied but ${svc.name} did not recover within ${Math.round((v.deadline - v.since) / 1000)}s (${v.okStreak} healthy checks, ${post.text}).`);
      }
    }

    /** Fixed by hand, or recovered by itself: close it honestly and withdraw whatever was pending. */
    async function selfRecoveryStep(i) {
      const r = rt(i.serviceId);
      if (!r.last || !r.last.ok || r.okStreak < set.verifyOk || r.last.at <= i.opened) return;
      if (i.cause && i.cause.kind === "external_alert") return; // the health check was passing all along; a human closes these
      const applied = i.proposals.find((x) => x.state === "applied");
      if (applied) {
        i.status = "resolved"; i.resolvedAt = now(); i.mttr = i.resolvedAt - i.opened; i.late = true;
        const receipt = S.receipts.find((x) => x.id === "rcpt-" + i.n + "-" + applied.n);
        if (receipt) { receipt.checks = { healthyInARow: r.okStreak, postCondition: "recovered after the verify window", at: now() }; receipt.outcome = "recovered (late)"; receipt.digest = await digest(Object.assign({}, receipt, { digest: undefined })); }
        log(i, "agent", `${i.service} came back after the verify window: ${r.okStreak} healthy checks in a row since fix ${applied.n}. Closing as resolved, late.`);
        if (i.woken && A.notify) await A.notify({ kind: "resolved", title: `${i.service} recovered`, body: `Recovered after fix ${applied.n}, later than expected (${ago(i.mttr)}).`, incidentId: i.id });
        await snapshot(svcOf(i.serviceId), true);
        return;
      }
      const p = pending(i);
      if (p) { p.state = "withdrawn"; p.withdrawnBy = "recovered"; }
      i.status = "recovered on its own"; i.resolvedAt = now(); i.mttr = i.resolvedAt - i.opened;
      log(i, "agent", `${i.service} has passed ${r.okStreak} health checks in a row without a fix from me (someone fixed it by hand, or it recovered by itself). Closing this incident${p ? `; fix ${p.n} is withdrawn and its phrase no longer works` : ""}.`);
      if (i.woken && A.notify) await A.notify({ kind: "resolved", title: `${i.service} recovered`, body: `It recovered without a WisprOps fix after ${ago(i.mttr)}. Nothing was applied.`, incidentId: i.id });
      await snapshot(svcOf(i.serviceId), true);
    }

    async function escalationStep(i) {
      const after = Number(set.escalateAfterMin) || 0;
      if (!after || !i.woken || i.ackAt || i.escalatedAt || !OPEN.includes(i.status) || snoozedUntil(i.serviceId)) return;
      if (now() - (i.pagedAt || i.opened) < after * 60000) return;
      i.escalatedAt = now();
      log(i, "system", `Nobody acknowledged in ${after} min, so the backup on-call was paged.`);
      if (A.notify) await A.notify({ kind: "escalate", title: `Unacknowledged: ${i.service} is down`, body: `${i.cause ? i.cause.summary : ""} The primary on-call has not answered for ${after} min.`, incidentId: i.id });
    }

    let booted = false;
    async function tick() {
      if (!booted) {
        booted = true;
        for (const i of S.incidents.filter((x) => x.status === "applying" || x.status === "triage")) {
          const p = i.proposals.find((x) => x.state === "approved");
          if (p) p.state = "interrupted";
          await escalate(i, `WisprOps restarted while this incident was ${i.status === "applying" ? "applying fix " + (p ? p.n : "") : "in triage"}. I can't tell whether the change landed, so nothing more will be applied.`);
        }
      }
      for (const svc of cfg.services) {
        const r = rt(svc.id);
        let probe;
        try { probe = await A.probe(svc); } catch (e) { probe = { ok: false, error: e.message }; }
        r.last = Object.assign({}, probe, { at: now() });
        const snoozed = snoozedUntil(svc.id);
        if (!snoozed && S.snoozes[svc.id]) { delete S.snoozes[svc.id]; activity("system", `Snooze on ${svc.name} ended. Watching and paging again.`); }
        r.snoozed = !!snoozed;
        if (probe.ok) {
          r.fails = 0; r.okStreak += 1; r.status = "ok"; r.heldBy = null;
          if (!openFor(svc.id) && r.okStreak >= set.goldenAfter) await snapshot(svc);
        } else {
          r.okStreak = 0; r.fails += 1; r.status = probe.error ? "down" : "failing";
          if (!snoozed && !r.heldBy && r.fails >= set.failAfter && !openFor(svc.id)) await openIncident(svc, probe);
        }
      }
      for (const i of S.incidents.filter((x) => x.status === "verifying")) await verifyStep(i);
      for (const i of S.incidents.filter((x) => ["triage", "proposed", "escalated"].includes(x.status))) await selfRecoveryStep(i);
      for (const i of S.incidents.filter((x) => OPEN.includes(x.status))) await escalationStep(i);
      await orderStep();
      await save();
    }

    /** A day before a standing order runs out, say so, with the words that renew it. */
    async function orderStep() {
      const t = now();
      for (const o of S.orders) {
        if (o.revoked || o.warned || o.expires <= t || o.usesLeft <= 0) continue;
        if (o.expires - t > 24 * 3600000) continue;
        const since = Math.max(o.granted, ...(o.renewals || []).map((x) => x.at));
        if (t - since < 12 * 3600000) continue; // a one-day order doesn't need a warning the moment it's granted
        o.warned = t;
        const text = `Standing order ${o.id} (${o.service}: ${o.describe}) expires ${new Date(o.expires).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}. To keep it, dictate “renew standing order ${o.id.replace("-", " ")} for seven days” with Wispr Flow.`;
        activity("agent", text);
        if (A.notify) await A.notify({ kind: "order", title: `Standing order ${o.id} expires soon`, body: text });
      }
    }

    /** An alert from your existing monitoring (Alertmanager, Grafana, Uptime Kuma, any webhook). */
    async function alert(serviceRef, info) {
      const svc = svcOf(serviceRef) || findService(serviceRef);
      info = { source: String((info && info.source) || "webhook").slice(0, 40), summary: String((info && info.summary) || "alert firing").slice(0, 500) };
      if (!svc) { activity("system", `Ignored a ${info.source} alert for unknown service “${String(serviceRef).slice(0, 60)}”.`); await save(); return { ok: false, reason: "unknown service" }; }
      const open = openFor(svc.id);
      if (open) { log(open, "system", `Another ${info.source} alert: ${info.summary}`); await save(); return { ok: true, incidentId: open.id, merged: true }; }
      if (snoozedUntil(svc.id)) { activity("system", `${info.source} alert for ${svc.name} arrived during its snooze: ${info.summary}`); await save(); return { ok: true, snoozed: true }; }
      const r = rt(svc.id);
      let probe;
      try { probe = await A.probe(svc); } catch (e) { probe = { ok: false, error: e.message }; }
      r.last = Object.assign({}, probe, { at: now() });
      const p = probe.ok ? Object.assign({}, probe, { ok: false, alertOnly: true, alert: info }) : Object.assign({}, probe, { alert: info });
      const i = await openIncident(svc, p);
      await save();
      return { ok: true, incidentId: i.id };
    }

    function ack(i, via) {
      if (i.ackAt) return false;
      i.ackAt = now(); i.ackVia = via || "console"; i.mtta = i.ackAt - (i.pagedAt || i.opened);
      return true;
    }

    function latestOpen() {
      const open = S.incidents.filter((i) => OPEN.includes(i.status));
      return open.filter((i) => pending(i)).pop() || open.pop() || null;
    }

    /** The most recently resolved incident whose approved fix was verified and has no standing order yet. */
    function latestGrantable() {
      const ok = S.incidents.filter((i) => i.status === "resolved" && !i.orderId && i.proposals.some((p) => p.state === "applied" && !p.action.remedy));
      return ok.sort((a, b) => (a.resolvedAt || a.opened || 0) - (b.resolvedAt || b.opened || 0)).pop() || null;
    }

    /** One entry point for everything said or typed in the console. incidentId is optional. */
    async function command(text, meta, incidentId) {
      meta = meta || {};
      const intent = parseIntent(text);
      const said = String(text || "").trim().slice(0, 2000);
      const via = meta.via || "unknown";
      const here = incidentId ? inc(incidentId) : null;
      if (incidentId && !here) return { reply: "No incident with that id. Nothing was changed." };
      if (here && !["handoff", "go", "select", "changes"].includes(intent.kind) && ack(here, via) && here.woken) log(here, "system", "Acknowledged (you answered the page).");
      switch (intent.kind) {
        case "snooze": {
          const targets = intent.target && !/^(?:all|everything|every service|all services|sab|sabhi)$/.test(intent.target) ? [findService(intent.target)].filter(Boolean) : here ? [svcOf(here.serviceId)] : cfg.services;
          if (!targets.length) { const reply = `I don't know a service called “${intent.target}”. Services: ${cfg.services.map((s) => s.name).join(", ")}.`; activity("you", said, { via }); activity("agent", reply); await save(); return { reply }; }
          const until = now() + intent.minutes * 60000;
          targets.forEach((s) => { S.snoozes[s.id] = { until, by: said, via, at: now() }; });
          const reply = `Snoozed ${targets.map((s) => s.name).join(", ")} for ${intent.minutes} min. I keep watching but won't open incidents or page until ${fmtClock(until)}. Say “resume paging” to end it early.`;
          activity("you", said, { via }); activity("agent", reply);
          if (here) log(here, "agent", reply);
          await save(); return { reply };
        }
        case "unsnooze": {
          const s = intent.target ? findService(intent.target) : null;
          const ids = s ? [s.id] : Object.keys(S.snoozes);
          ids.forEach((id) => delete S.snoozes[id]);
          const reply = ids.length ? `Paging is back on for ${ids.map((id) => (svcOf(id) || { name: id }).name).join(", ")}.` : "Nothing was snoozed.";
          activity("you", said, { via }); activity("agent", reply);
          await save(); return { reply };
        }
        case "pauseOrders":
        case "resumeOrders": {
          if (intent.kind === "resumeOrders") {
            const c = consent(meta, "resume standing orders".length, set, said);
            if (!c.ok) { const reply = "Refused. Turning standing orders back on lets me fix things without asking, so it needs a Wispr Flow dictation. " + c.reason; activity("you", said, { via }); activity("agent", reply); await save(); return { reply }; }
          }
          S.ordersPaused = intent.kind === "pauseOrders";
          const reply = S.ordersPaused ? "All standing orders are paused. Every failure will page you until you say “resume standing orders”." : "Standing orders are active again.";
          activity("you", said, { via }); activity("agent", reply);
          if (here) log(here, "agent", reply);
          await save(); return { reply };
        }
        case "handoff": {
          const reply = handoff();
          activity("you", said, { via }); activity("agent", reply);
          await save(); return { reply, say: reply };
        }
        case "freeze": {
          let reply;
          if (S.frozen) reply = `Already frozen since ${fmtClock(S.frozen.at)}.`;
          else { S.frozen = { at: now(), by: redact(said).text, via }; reply = "Production is frozen. Nothing will be applied, by you or by any standing order, until you say “unfreeze production” with Wispr Flow. I keep watching and paging."; }
          activity("you", said, { via }); activity("agent", reply);
          if (here) log(here, "agent", reply);
          await save(); return { reply, say: reply };
        }
        case "unfreeze": {
          let reply;
          if (set.applyEnabled === false) reply = "Changes are switched off in the config (settings.applyEnabled). Voice can't override that.";
          else if (!S.frozen) reply = "Production isn't frozen.";
          else {
            const c = consent(meta, "unfreeze production".length - 4, set, said);
            if (!c.ok) reply = "Refused. Unfreezing lets changes run again, so it needs a Wispr Flow dictation. " + c.reason;
            else { S.frozen = null; reply = `Unfrozen through ${c.channel}. Approved fixes and standing orders can run again.`; }
          }
          activity("you", said, { via }); activity("agent", reply);
          if (here) log(here, "agent", reply);
          await save(); return { reply };
        }
        case "renew": {
          const t = now();
          let list = S.orders.filter((o) => !o.revoked && (o.expires > t - 7 * 86400000));
          if (intent.id) list = list.filter((o) => o.id === intent.id);
          else if (here) list = list.filter((o) => o.serviceId === here.serviceId);
          let reply;
          if (!list.length) reply = intent.id ? `There is no standing order ${intent.id} to renew (revoked ones stay revoked).` : "There is no standing order to renew here. Revoked orders stay revoked; grant a new one after the next verified fix.";
          else if (new Set(list.map((o) => o.serviceId)).size > 1) reply = `Which one? ${list.map((o) => `${o.id} (${o.service})`).join(", ")}. Say “renew standing order ${list[0].id.replace("-", " ")} for seven days”.`;
          else {
            const c = consent(meta, "renew standing order".length - 2, set, said);
            if (!c.ok) reply = "Refused. A standing order lets me fix things without asking, so renewing it needs a Wispr Flow dictation. " + c.reason;
            else {
              const days = Math.min(intent.days, set.maxOrderDays), uses = Math.min(intent.uses, set.maxOrderUses);
              list.forEach((o) => { o.expires = t + days * 86400000; o.usesLeft = uses; o.warned = null; if (intent.night) o.window = { from: set.nightFrom, to: set.nightTo }; o.renewals = (o.renewals || []).concat([{ at: t, said: redact(said).text, channel: c.channel, days, uses }]); });
              reply = `Renewed ${list.map((o) => o.id).join(", ")} through ${c.channel}: ${days} day${days === 1 ? "" : "s"}, ${uses} uses${intent.night ? ", nights only" : ""}.${days < intent.days || uses < intent.uses ? " (Capped by your policy.)" : ""}`;
            }
          }
          activity("you", said, { via }); activity("agent", reply);
          if (here) log(here, "agent", reply);
          await save(); return { reply };
        }
        case "changes": {
          const ids = here ? [here.serviceId] : cfg.services.map((s) => s.id);
          const list = S.changes.filter((c) => ids.includes(c.serviceId) && c.at > now() - 24 * 3600000).slice(-6);
          const reply = list.length ? "Changes in the last 24 hours: " + list.map((c) => `${fmtClock(c.at)} ${c.service}: ${c.text}${c.by ? " by " + c.by : ""}`).join(". ") + "." : "No changes recorded in the last 24 hours.";
          activity("you", said, { via }); activity("agent", reply);
          await save(); return { reply, say: reply };
        }
        case "go": return { reply: `Opening ${intent.page === "receipts" ? "the audit log" : intent.page}.`, go: intent.page };
        case "select": {
          const t = S.incidents.find((x) => x.n === intent.n);
          return t ? { reply: `Incident #${t.n}: ${t.service}, ${t.status}.`, select: t.id, go: "board" } : { reply: `There is no incident #${intent.n}.` };
        }
        default: break;
      }
      if (!here && ["approve", "undo", "grant"].includes(intent.kind)) {
        const waiting = S.incidents.filter((i) => OPEN.includes(i.status) && pending(i));
        if (waiting.length > 1) {
          const reply = `${waiting.length} incidents are waiting (${waiting.map((i) => `#${i.n} ${i.service}`).join(", ")}). Pick one first, so I apply the fix you mean. Nothing was changed.`;
          activity("you", said, { via }); activity("agent", reply); await save(); return { reply };
        }
      }
      // A standing order is granted after the fact, on an incident that is already resolved, so with nothing selected
      // it goes to the latest verified fix you approved (not to whatever is open now).
      const target = here || (intent.kind === "grant" ? latestGrantable() : intent.kind === "debrief" ? latestOpen() || S.incidents[S.incidents.length - 1] : latestOpen());
      if (!target) {
        const reply = intent.kind === "debrief" ? "There is no incident to debrief yet. Nothing was saved." : intent.kind === "explain" ? statusLine() : intent.kind === "grant" ? "There is no verified fix to grant a standing order for yet. Approve a fix first; once it is verified, say it again." : intent.kind === "approve" || intent.kind === "undo" ? "There is no open incident, so there is nothing to approve." :"Nothing is broken right now. You can say “snooze <service> for thirty minutes”, “pause standing orders” or “what happened last night”.";
        activity("you", said, { via }); activity("agent", reply);
        await save(); return { reply };
      }
      return talk(target.id, text, meta);
    }

    function statusLine() {
      const bad = cfg.services.filter((s) => rt(s.id).status !== "ok");
      const sn = Object.keys(S.snoozes).filter((id) => snoozedUntil(id));
      return `${cfg.services.length - bad.length} of ${cfg.services.length} services healthy.${bad.length ? " Not healthy: " + bad.map((s) => `${s.name} (${rt(s.id).status})`).join(", ") + "." : ""}${sn.length ? " Snoozed: " + sn.map((id) => svcOf(id).name).join(", ") + "." : ""}${S.ordersPaused ? " Standing orders are paused." : ""}${frozenWhy() ? " " + frozenWhy() : ""}`;
    }

    /** A line about one incident. meta describes how it arrived (see consent). */
    async function talk(incidentId, text, meta) {
      let out;
      const i = inc(incidentId);
      if (!i) return { reply: "No incident with that id." };
      meta = meta || {};
      const intent = parseIntent(text);
      if (["snooze", "unsnooze", "pauseOrders", "resumeOrders", "handoff", "freeze", "unfreeze", "renew", "changes", "go", "select"].includes(intent.kind)) return command(text, meta, incidentId);
      log(i, "you", text, { via: meta.via || "unknown", insert: meta.insert || 0, keys: meta.keys || 0 });
      if (ack(i, meta.via) && intent.kind !== "ack" && i.woken) log(i, "system", "Acknowledged (you answered the page).");
      if (i.status === "applying") { out = { reply: "A fix is being applied right now. Wait for it to finish." }; log(i, "agent", out.reply); await save(); return out; }
      const p = pending(i);

      if (meta.duringReadback && p && intent.kind !== "approve") {
        p.state = "withdrawn"; p.withdrawnBy = "barge-in";
        log(i, "agent", `You spoke over the read-back, so fix ${p.n} is withdrawn and approving it no longer works.`);
        const np = await propose(i, p.action);
        out = { reply: `Withdrawn fix ${p.n}.` + (np ? ` It is back on the table as fix ${np.n}.` : "") };
        await save(); return out;
      }

      switch (intent.kind) {
        case "approve": {
          if (!p) { const any = i.proposals.find((x) => x.n === intent.n); out = { reply: any ? `Fix ${intent.n} is ${any.state}; there is nothing pending to approve.` : "There is no fix waiting for approval." }; log(i, "agent", out.reply); break; }
          if (intent.n !== p.n) { const old = i.proposals.find((x) => x.n === intent.n); out = { reply: old ? `Fix ${intent.n} was ${old.state}. The fix on the table is fix ${p.n}: say “${p.phrase}”.` : `There is no fix ${intent.n}. Say “${p.phrase}”.` }; log(i, "agent", out.reply); break; }
          // With a say-back, the whole sentence has to arrive as one dictation, not just its first words.
          const c = intent.back ? consent(meta, String(text).trim().length, set, text) : consent(meta, ("approve fix " + p.n).length - 2, set);
          if (!c.ok) { log(i, "agent", "Refused: " + c.reason); out = { reply: "Refused. " + c.reason }; break; }
          const svc = svcOf(i.serviceId);
          if (!intent.back && set.sayItBack !== false) {
            out = { reply: `Say it back, so I know you heard what it does. In one Wispr Flow dictation: “${p.sayBack || sayBackExample(p.n, p.action, svc)}”.` };
            log(i, "agent", `Not yet: “approve fix ${FIXW[p.n] || p.n}” alone doesn't show you heard the fix. ${out.reply}`);
            break;
          }
          let said = null;
          if (intent.back) {
            // Whatever you say about the fix has to be true, even when the short phrase would have been enough.
            const chk = checkSayBack(p, svc, cfg.services, intent.back, intent.rawBack);
            if (!chk.ok) {
              const why = chk.problems.join("; ");
              log(i, "agent", `Refused: ${why}. Nothing was applied.`, { sayBack: chk.facts });
              out = { reply: `Refused: ${why}. Nothing was applied. Fix ${p.n}: ${p.describe}` };
              break;
            }
            said = chk.facts;
          }
          if (frozenWhy()) { out = { reply: `${frozenWhy()} Fix ${p.n} stays on the table; say “unfreeze production” with Wispr Flow first.` }; log(i, "agent", out.reply); break; }
          c.meta = { via: meta.via, insert: meta.insert || 0, keys: meta.keys || 0 };
          c.saidBack = said;
          log(i, "agent", `Consent heard through ${c.channel}: “${text.trim()}”.${said ? ` You said it back: ${said.map((f) => f.label + " ✓").join(" · ")}.` : ""}`, said ? { sayBack: said } : undefined);
          await apply(i, p, c, text.trim());
          out = { reply: `Applying fix ${p.n}.` };
          break;
        }
        case "ack":
          out = { reply: i.woken ? "Acknowledged. Your backup will not be paged." + (p ? ` Fix ${p.n} is waiting: say “${p.phrase}” or “read it back”.` : "") : "Noted. Nobody was paged for this one." };
          log(i, "agent", out.reply);
          break;
        case "unclear":
          out = { reply: `That sounded like a question, a “no”, or more than the exact words, so nothing was changed.${p ? ` To apply it, say “${p.phrase}”.` : ""}` };
          log(i, "agent", out.reply);
          break;
        case "vague":
          out = { reply: p ? `I need the exact words. Say “${p.phrase}”.` : "Nothing is waiting for approval." };
          log(i, "agent", out.reply);
          break;
        case "undo": {
          const done = i.proposals.find((x) => x.n === intent.n && x.state === "applied");
          if (!done) { out = { reply: `Fix ${intent.n} has not been applied, so there is nothing to undo.` }; log(i, "agent", out.reply); break; }
          const inv = ACTIONS[done.action.kind].inverse(done.action);
          if (!inv) { out = { reply: `A ${done.action.kind} has no inverse. Nothing to undo.` }; log(i, "agent", out.reply); break; }
          const c = consent(meta, ("undo fix " + intent.n).length - 2, set);
          if (!c.ok) { log(i, "agent", "Refused: " + c.reason); out = { reply: "Refused. " + c.reason }; break; }
          if (frozenWhy()) { out = { reply: `${frozenWhy()} Nothing was undone.` }; log(i, "agent", out.reply); break; }
          const dry = A.dryRun ? await A.dryRun(svcOf(i.serviceId), inv) : { ok: true };
          if (!dry.ok) { out = { reply: `Not undoing: the dry run failed (${dry.text}). Nothing was changed.` }; log(i, "agent", out.reply); break; }
          const res = await A.exec(svcOf(i.serviceId), inv);
          const orig = S.receipts.find((x) => x.id === "rcpt-" + i.n + "-" + done.n);
          if (res.ok) {
            done.state = "undone"; i.status = "handed back"; i.verify = null; rt(i.serviceId).heldBy = i.id;
            if (orig && orig.outcome === "verifying") orig.outcome = "undone before verified";
            log(i, "agent", `Undid fix ${intent.n}: ${ACTIONS[inv.kind].describe(inv, svcOf(i.serviceId))} The incident is yours now.`);
          } else {
            await escalate(i, `Undo of fix ${intent.n} failed: ${res.output}`);
          }
          S.receipts.push({ id: "rcpt-" + i.n + "-undo-" + intent.n, incidentId: i.id, service: i.service, at: now(), heard: text.trim(), phrase: "undo fix " + intent.n, channel: c.channel, flagged: !!c.flagged, meta: { via: meta.via, insert: meta.insert || 0, keys: meta.keys || 0 }, action: inv, describe: "Undo of fix " + intent.n, outcome: res.ok ? "undone" : "undo failed" });
          out = { reply: res.ok ? `Undone. It's yours.` : "Undo failed." };
          break;
        }
        case "grant": {
          const applied = i.proposals.find((x) => x.state === "applied");
          if (i.status !== "resolved" || !applied || i.orderId) { out = { reply: "I can only offer a standing order after a fix you approved has been verified." }; log(i, "agent", out.reply); break; }
          if (applied.action.remedy) { out = { reply: "That fix was a restart nothing explained. I won't take a guess on without you, so the next one pages you." }; log(i, "agent", out.reply); break; }
          const c = consent(meta, "grant standing order".length, set);
          if (!c.ok) { log(i, "agent", "Refused: " + c.reason); out = { reply: "Refused. " + c.reason }; break; }
          const days = Math.min(intent.days, set.maxOrderDays), uses = Math.min(intent.uses, set.maxOrderUses);
          const o = { id: "so-" + (S.orders.length + 1), serviceId: i.serviceId, service: i.service, signature: signature(i), action: applied.action, describe: applied.describe,
            granted: now(), expires: now() + days * 86400000, usesLeft: uses, used: 0, said: redact(text.trim()).text, channel: c.channel, flagged: !!c.flagged, fromIncident: i.id, revoked: false, window: intent.night ? { from: set.nightFrom, to: set.nightTo } : null };
          S.orders.push(o);
          log(i, "agent", `Standing order ${o.id} granted through ${c.channel}: if ${i.service} fails the same way, I ${applied.action.kind === "rollback" ? "roll back any release that breaks it to the last healthy one" : `apply “${applied.describe}”`} myself, verify it, and let you sleep. Valid ${days} day${days === 1 ? "" : "s"}, ${uses} uses${o.window ? `, nights only (${String(o.window.from).padStart(2, "0")}:00–${String(o.window.to).padStart(2, "0")}:00)` : ""}.${days < intent.days || uses < intent.uses ? " (Capped by your policy.)" : ""} Say “revoke standing order” to end it.`);
          out = { reply: `Granted ${o.id}.` };
          break;
        }
        case "revoke": {
          const live = S.orders.filter((o) => !o.revoked && o.serviceId === i.serviceId);
          live.forEach((o) => { o.revoked = true; o.revokedAt = now(); o.revokedBy = text.trim(); });
          out = { reply: live.length ? `Revoked ${live.map((o) => o.id).join(", ")}. The next failure will page you.` : "No active standing order for this service." };
          log(i, "agent", out.reply);
          break;
        }
        case "pr": {
          if (!A.openPR) { out = { reply: "Pull requests are not configured here." }; break; }
          const cp = consent(meta, "open the pull request".length - 4, set, text);
          if (!cp.ok) { log(i, "agent", "Refused: " + cp.reason); out = { reply: "Refused. " + cp.reason }; break; }
          const res = await A.openPR(i, postmortem(i));
          i.pr = res;
          log(i, "agent", res.text);
          out = { reply: res.text };
          break;
        }
        case "note": {
          const r = redact(intent.text.slice(0, 4000));
          i.notes = (i.notes || []).concat([{ at: now(), text: r.text, via: meta.via === "wispr" || meta.via === "sim" ? "dictated with Wispr Flow" : meta.via === "telegram" ? "Telegram" : "typed" }]);
          out = { reply: "Noted for the postmortem and the morning handoff." + (r.redacted ? ` (${r.redacted} private detail${r.redacted === 1 ? "" : "s"} redacted before saving.)` : "") };
          log(i, "agent", out.reply);
          break;
        }
        case "debrief": {
          const d = parseDebrief(intent.text.slice(0, 8000));
          const how = meta.via === "wispr" ? "dictated with Wispr Flow" : meta.via === "sim" ? "simulated Wispr" : meta.via === "telegram" ? "Telegram" : "typed";
          const burst = meta.via === "wispr" ? Math.max(0, Number(meta.insert) || 0) : 0;
          // Rules change what WisprOps enforces, so like runbook rules they only come from a dictation in the signed-in console.
          const rules = meta.via === "wispr" || meta.via === "sim" ? d.rules.map((r) => addRunbook(i.serviceId, r.text, "from a debrief, " + how)).filter((x) => x && x.rule) : [];
          const R = (x) => redact(x).text;
          const entry = { at: now(), via: how, words: d.words, burst, text: R(intent.text.slice(0, 8000)), actions: d.actions.map((x) => Object.assign({}, x, { text: R(x.text) })),
            causes: d.causes.map(R), good: d.good.map(R), context: d.context.map(R), rules: rules.map((x) => x.rule.text), textOnly: d.rules.length - rules.length };
          i.debriefs = (i.debriefs || []).concat([entry]);
          const owners = Array.from(new Set(entry.actions.map((x) => x.owner)));
          const pl = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
          const bits = [pl(entry.actions.length, "action item") + (owners.length ? ` (${owners.join(", ")})` : ""), pl(entry.causes.length, "cause")];
          if (entry.good.length) bits.push(pl(entry.good.length, "thing") + " that went well");
          if (entry.rules.length) bits.push(pl(entry.rules.length, "new rule") + ` (${entry.rules.join("; ")})`);
          out = { reply: `Debrief filed on #${i.n}: ${d.words} words${burst ? " in one Wispr dictation" : ""} → ${bits.join(", ")}. It's in the postmortem.${entry.textOnly ? " The rule in it was kept as text only: rules need a Wispr dictation." : ""}`, postmortem: i.id };
          log(i, "agent", out.reply);
          break;
        }
        case "readback":
          out = p ? { reply: "Reading fix " + p.n + ".", say: readback(p), readbackOf: p.n } : { reply: "Nothing pending to read back." };
          break;
        case "explain":
          out = { reply: i.cause ? i.cause.summary + (p ? ` Fix ${p.n} is waiting: ${p.describe}` : ` Status: ${i.status}.`) : "Still looking." };
          log(i, "agent", out.reply);
          break;
        case "standdown":
          if (p) { p.state = "withdrawn"; p.withdrawnBy = "you"; }
          i.status = "stood down"; rt(i.serviceId).heldBy = i.id;
          out = { reply: "Standing down. Nothing will be applied and I won't page you again for this service until it is healthy; the incident is yours." };
          log(i, "agent", out.reply);
          break;
        default:
          out = { reply: p ? `Fix ${p.n} is still waiting: say “${p.phrase}”, “read it back”, or “I'll handle it”. Start with “note:” to leave a handoff note, or “debrief:” to talk it through for the postmortem.` : "I didn't catch a command. Start with “note:” to leave a handoff note, or say “what happened?”." };
          log(i, "agent", out.reply);
      }
      await save();
      return out;
    }

    async function acknowledge(incidentId, via) {
      const i = inc(incidentId); if (!i) return false;
      if (ack(i, via || "button")) log(i, "system", `Acknowledged from the ${via || "console"}. Your backup will not be paged.`);
      await save(); return true;
    }

    function addNote(incidentId, text, source) {
      const i = inc(incidentId); if (!i) return false;
      i.notes = (i.notes || []).concat([{ at: now(), text: redact(String(text).slice(0, 4000)).text, via: source || "note" }]);
      log(i, "system", `Note added from ${source || "a note"}.`);
      save();
      return true;
    }

    /** opts.rules === false: keep it as text only (notes from MCP or an unsigned client can't become rules). */
    function addRunbook(serviceId, text, via, opts) {
      const svc = svcOf(serviceId) || findService(serviceId);
      text = String(text || "").trim();
      if (!svc || !text) return null;
      const list = (S.runbooks[svc.id] = S.runbooks[svc.id] || []);
      const note = { id: "rb-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), at: now(), text: redact(text.slice(0, 2000)).text, via: via || "typed" };
      const rule = opts && opts.rules === false ? null : parseRule(text);
      if (rule) note.rule = rule;
      list.push(note);
      if (list.length > 20) list.shift();
      save();
      return note;
    }
    function removeRunbook(serviceId, id) {
      const list = S.runbooks[serviceId] || [];
      const k = list.findIndex((n) => n.id === id);
      if (k < 0) return false;
      list.splice(k, 1); save(); return true;
    }
    async function snooze(serviceId, minutes, via) {
      const svc = svcOf(serviceId); if (!svc) return false;
      if (!minutes) { delete S.snoozes[svc.id]; activity("you", `Ended the snooze on ${svc.name}.`, { via: via || "button" }); }
      else { S.snoozes[svc.id] = { until: now() + minutes * 60000, by: `${minutes} min from the console`, via: via || "button", at: now() }; activity("you", `Snoozed ${svc.name} for ${minutes} min.`, { via: via || "button" }); }
      await save(); return true;
    }
    async function setOrdersPaused(paused, via) {
      S.ordersPaused = !!paused;
      activity("you", paused ? "Paused all standing orders." : "Resumed standing orders.", { via: via || "button" });
      await save(); return true;
    }
    async function revokeOrder(id, via) {
      const o = S.orders.find((x) => x.id === id); if (!o) return false;
      o.revoked = true; o.revokedAt = now(); o.revokedBy = via || "console";
      activity("you", `Revoked standing order ${o.id} (${o.service}).`, { via: via || "button" });
      await save(); return true;
    }

    function postmortem(i) {
      const svc = svcOf(i.serviceId) || { name: i.service };
      const rc = i.receipts.map((id) => S.receipts.find((r) => r.id === id)).filter(Boolean);
      const undo = S.receipts.filter((r) => r.incidentId === i.id && /undo/.test(r.id));
      const deb = [];
      (i.debriefs || []).forEach((d) => {
        deb.push("", `## Debrief (${d.via}, ${d.words} words${d.burst ? " in one dictation" : ""}, ${fmtClock(d.at)})`);
        if (d.causes.length) deb.push("", "### Why it happened", ...d.causes.map((x) => `- ${x}`));
        if (d.actions.length) deb.push("", "### Action items", ...d.actions.map((x) => `- [ ] **${x.owner}**: ${x.text}${x.due ? ` (${x.due})` : ""}`));
        if (d.good.length) deb.push("", "### What went well", ...d.good.map((x) => `- ${x}`));
        if (d.rules.length) deb.push("", "### Rules added to the runbook", ...d.rules.map((x) => `- ${x}. WisprOps enforces this from now on.`));
        if (d.context.length) deb.push("", "### Context", ...d.context.map((x) => `- ${x}`));
      });
      const lines = [
        `# Postmortem: ${svc.name}, ${new Date(i.opened).toLocaleString()}`,
        "",
        `**Status:** ${i.status}${i.mttr ? ` · recovered in ${ago(i.mttr)}` : ""} · ${i.woken ? "on-call was paged" : "nobody was woken (standing order)"}${i.mtta != null && i.woken ? ` · acknowledged after ${ago(i.mtta)}` : ""}${i.escalatedAt ? " · escalated to the backup" : ""}`,
        `**Detected by:** ${i.source || "health check"}`,
        "",
        "## What broke",
        i.cause ? i.cause.summary : "Unknown.",
        ...deb,
        "",
        "## Evidence",
        ...(i.evidence || []).map((e) => `- ${e.text}`),
        "",
        "## Changes before it broke",
        ...((i.changes || []).map((id) => S.changes.find((c) => c.id === id)).filter(Boolean).map((c) => `- ${fmtClock(c.at)} ${c.text}${c.by ? ` (by ${c.by})` : ""}${c.source ? ` · ${c.source}` : ""}`).concat((i.changes || []).length ? [] : ["- Nothing recorded in the change ledger."])),
        "",
        "## What was done",
        ...(i.proposals.length ? i.proposals.map((p) => `- Fix ${p.n} (${p.state}): ${p.describe}`) : ["- Nothing was applied."]),
        "",
        "## Consent",
        ...(rc.length || undo.length ? rc.concat(undo).map((r) => `- ${fmtClock(r.at)} “${r.heard}” via ${r.channel}${r.flagged ? " (flagged: typed)" : ""} → ${r.outcome}${r.digest ? ` · digest \`${String(r.digest).slice(0, 16)}\`` : ""}`) : ["- No change was applied."]),
        "",
        "## Timeline",
        ...i.timeline.map((t) => `- ${fmtClock(t.at)} **${t.who}**: ${t.text}`),
      ];
      if (i.notes && i.notes.length) lines.push("", "## Notes", ...i.notes.map((n) => `- (${n.via}) ${n.text}`));
      const mine = i.timeline.filter((t) => t.who === "you");
      if (mine.length) lines.push("", "## In your own words", ...mine.map((t) => `- ${fmtClock(t.at)} “${t.text}” (${t.via === "wispr" ? "Wispr Flow" : t.via === "sim" ? "simulated Wispr" : t.via || "typed"})`));
      if (i.recurrence) lines.push("", "## Seen before", `- This was the ${ORD(i.recurrence.count)} time in 7 days. A standing order, or a fix at the source, stops the repeats.`);
      lines.push("", "## Follow-up", i.cause && i.cause.kind === "config_drift" ? `- Restore \`${i.cause.key}\` in the source of truth so the next deploy does not bring it back.` : i.cause && i.cause.kind === "bad_deploy" ? `- Find the bug in release ${i.cause.key} and add a test before redeploying it.` : "- Find out why this happened and add a test for it.");
      return lines.join("\n") + "\n";
    }

    /** Morning handoff: everything since `since` (default: 18 hours ago). */
    function handoff(since) {
      since = since == null ? now() - 18 * 3600000 : since;
      const list = S.incidents.filter((i) => i.opened >= since || OPEN.includes(i.status));
      const sn = Object.keys(S.snoozes).filter((id) => snoozedUntil(id)).map((id) => (svcOf(id) || { name: id }).name);
      const tail = [];
      if (sn.length) tail.push(`Still snoozed: ${sn.join(", ")}.`);
      if (S.ordersPaused) tail.push("Standing orders are paused.");
      if (frozenWhy()) tail.push(frozenWhy());
      const soon = S.orders.filter((o) => !o.revoked && o.usesLeft > 0 && o.expires > now() && o.expires - now() < 2 * 86400000);
      if (soon.length) tail.push(`Expiring soon: ${soon.map((o) => o.id + " (" + o.service + ")").join(", ")}. Say “renew standing order for seven days” to keep ${soon.length === 1 ? "it" : "them"}.`);
      if (!list.length) return ["Quiet night. No incidents, nobody woken."].concat(tail).join("\n");
      const woken = list.filter((i) => i.woken).length, slept = list.filter((i) => i.orderId && i.status === "resolved").length;
      const open = list.filter((i) => !CLOSED.includes(i.status));
      const notes = list.flatMap((i) => (i.notes || []).map((n) => `${i.service}: ${n.text}`));
      return [
        `Good morning. ${list.length} incident${list.length === 1 ? "" : "s"} overnight; you were woken ${woken} time${woken === 1 ? "" : "s"}${slept ? `, ${slept} handled under a standing order while you slept` : ""}.`,
        ...list.map((i) => {
          const fix = i.proposals.find((p) => p.state === "applied" || p.state === "undone");
          return `${fmtClock(i.opened)} ${i.service}: ${i.cause ? i.cause.summary : "under investigation"} ${fix ? `Fix: ${fix.describe}` : ""} ${i.status === "resolved" ? `Recovered in ${ago(i.mttr)}${i.orderId ? " under " + i.orderId : ""}.` : i.status === "recovered on its own" ? `Recovered without a fix after ${ago(i.mttr)}.` : `Status: ${i.status}.`}`.replace(/\s+/g, " ").trim();
        }),
        ...(notes.length ? ["Notes left for you: " + notes.join(" · ")] : []),
        ...((acts) => (acts.length ? ["Action items from debriefs: " + acts.join(" · ")] : []))(list.flatMap((i) => (i.debriefs || []).flatMap((d) => d.actions.map((x) => `${x.owner}: ${x.text}${x.due ? ` (${x.due})` : ""}`)))),
        open.length ? `Still needs you: ${open.map((i) => i.service + " (" + i.status + ")").join(", ")}.` : "Nothing is waiting on you.",
        ...tail,
      ].join("\n");
    }

    function stats() {
      const done = S.incidents.filter((i) => i.status === "resolved");
      const mttr = done.length ? Math.round(done.reduce((a, i) => a + i.mttr, 0) / done.length) : null;
      const acked = S.incidents.filter((i) => i.woken && i.mtta != null);
      return { incidents: S.incidents.length, resolved: done.length, mttrMs: mttr, mttaMs: acked.length ? Math.round(acked.reduce((a, i) => a + i.mtta, 0) / acked.length) : null,
        woken: S.incidents.filter((i) => i.woken).length, escalations: S.incidents.filter((i) => i.escalatedAt).length,
        underOrder: done.filter((i) => i.orderId).length, approvals: S.receipts.filter((r) => /Wispr/.test(r.channel)).length,
        refusals: S.incidents.reduce((a, i) => a + i.timeline.filter((t) => /^Refused:/.test(t.text)).length, 0),
        repeats: S.incidents.filter((i) => i.recurrence).length, changes: S.changes.length, frozen: !!frozenWhy(),
        minutesSaved: Math.round(S.incidents.filter((i) => i.status === "resolved").reduce((a, i) => a + Math.max(0, 53 * 60000 - (i.mttr || 0)), 0) / 60000) };
    }

    async function freeze(via) {
      if (S.frozen) return true;
      S.frozen = { at: now(), by: "freeze button", via: via || "button" };
      activity("you", "Froze production.", { via: via || "button" });
      await save(); return true;
    }
    return { state: S, tick, talk, command, alert, acknowledge, addNote, addRunbook, removeRunbook, snooze, setOrdersPaused, revokeOrder, postmortem, handoff, stats, statusLine, readback, pending, reportChange, freeze, frozenWhy,
      certificate: (id) => certificate(S.receipts.find((r) => r.id === id), S),
      on: (fn) => (listeners.add(fn), () => listeners.delete(fn)), services: cfg.services, settings: set, svcOf, rt, findService, snoozedUntil };
  }

  /** Audit log rows: every change and every standing-order grant or revocation, oldest first. */
  function auditRows(S) {
    const iso = (t) => (t ? new Date(t).toISOString() : "");
    const how = (m) => (!m ? "" : m.via === "sim" ? "simulated Wispr (demo)" : `${m.insert || 0} chars in one insertion, ${m.keys || 0} keystrokes`);
    const rows = (S.receipts || []).map((r) => ({ at: iso(r.at), type: /undo/.test(r.id) ? "undo" : r.orderId ? "fix (standing order)" : "fix", id: r.id, service: r.service, incident: r.incidentId, words: r.heard || "", channel: r.channel || "", arrived: how(r.meta), flagged: r.flagged ? "yes" : "",
      change: r.describe || "", dryRun: r.dryRun || "", outcome: r.outcome || "", verifiedBy: r.checks ? `${r.checks.healthyInARow} healthy checks; ${r.checks.postCondition}` : "", digest: r.digest || "" }));
    (S.orders || []).forEach((o) => {
      rows.push({ at: iso(o.granted), type: "standing order granted", id: o.id, service: o.service, incident: o.fromIncident, words: o.said, channel: o.channel, arrived: "", flagged: o.flagged ? "yes" : "", change: o.describe, dryRun: "", outcome: `${o.usesLeft} uses left, expires ${iso(o.expires)}`, verifiedBy: "", digest: "" });
      if (o.revoked) rows.push({ at: iso(o.revokedAt), type: "standing order revoked", id: o.id, service: o.service, incident: "", words: o.revokedBy || "", channel: "", arrived: "", flagged: "", change: o.describe, dryRun: "", outcome: "revoked", verifiedBy: "", digest: "" });
    });
    return rows.sort((a, b) => (a.at < b.at ? -1 : 1));
  }
  function toCSV(rows) {
    if (!rows.length) return "";
    const cols = Object.keys(rows[0]);
    const cell = (v) => { const s = String(v == null ? "" : v); return /[",\n\r]/.test(s) || /^[=+\-@]/.test(s) ? '"' + (/^[=+\-@]/.test(s) ? "'" : "") + s.replace(/"/g, '""') + '"' : s; };
    return [cols.join(","), ...rows.map((r) => cols.map((c) => cell(r[c])).join(","))].join("\n") + "\n";
  }

  const api = { createAgent, emptyState, parseIntent, consent, checkSayBack, sayBackExample, parseDebrief, diffConfig, auditRows, toCSV, redact, reduceLogs, parseRule, describeService, certificate, ACTIONS, ALLOWED, PHRASES, OPEN, CLOSED, CONSENT_KINDS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.WisprOps = api;
})(typeof window !== "undefined" ? window : globalThis);
