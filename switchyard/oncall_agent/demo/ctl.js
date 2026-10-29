/* Control script for the demo service. Used by `wisprops break …` and as the demo's
   configured restart/rollback commands.
   node demo/ctl.js reset | break config|deploy|crash | restart | release <id> */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const RT = path.join(__dirname, "runtime");
fs.mkdirSync(RT, { recursive: true });
const w = (f, s) => fs.writeFileSync(path.join(RT, f), s);
const log = (line) => fs.appendFileSync(path.join(RT, "patient.log"), `${new Date().toISOString()} ${line}\n`);
const [cmd, arg] = process.argv.slice(2);

/** Tell the running WisprOps who made the change, the way a deploy bot or CI would (POST /api/change). */
async function report(body) {
  try {
    const key = fs.readFileSync(path.join(__dirname, ".wisprops", "secret"), "utf8").trim();
    const port = JSON.parse(fs.readFileSync(path.join(__dirname, "wisprops.config.json"), "utf8")).port || 7300;
    await fetch(`http://127.0.0.1:${port}/api/change`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + key }, body: JSON.stringify(Object.assign({ service: "checkout-api", source: "demo" }, body)), signal: AbortSignal.timeout(2000) });
  } catch (e) { /* WisprOps isn't running: its snapshot still finds the change */ }
}

function reset() {
  w("config.json", JSON.stringify({ db_pool: 20, cache_ttl_s: 60, payments_timeout_ms: 3000 }, null, 2) + "\n");
  w("release.txt", "v41\n");
  try { fs.unlinkSync(path.join(RT, "down")); } catch (e) { /* not down */ }
  w("patient.log", "");
}

switch (cmd) {
  case "reset": reset(); console.log("demo reset: healthy, release v41, db_pool 20"); break;
  case "break":
    if (arg === "config") { const f = path.join(RT, "config.json"); const c = JSON.parse(fs.readFileSync(f, "utf8")); c.db_pool = 0; fs.writeFileSync(f, JSON.stringify(c, null, 2) + "\n"); log("INFO config reloaded by deploy-bot"); console.log("set db_pool to 0"); report({ kind: "config", key: "db_pool", text: "config sync applied the staging profile", by: "deploy-bot" }); }
    else if (arg === "deploy") { w("release.txt", "v42\n"); log("INFO deployed v42"); console.log("deployed release v42 (it has a bug)"); report({ kind: "release", version: "v42", text: "deployed v42 (pricing refactor)", by: "ci, merge of #418 by sam" }); }
    else if (arg === "crash") { w("down", "1"); log("FATAL out of memory"); console.log("the process stopped answering"); }
    else { console.error("break what? config | deploy | crash"); process.exit(2); }
    break;
  case "restart": try { fs.unlinkSync(path.join(RT, "down")); } catch (e) { /* already up */ } log("INFO restarted"); console.log("restarted"); break;
  case "release":
    if (!/^[\w.\-]{1,64}$/.test(arg || "")) { console.error("bad release id"); process.exit(2); }
    w("release.txt", arg + "\n"); log(`INFO now serving ${arg}`); console.log("now serving " + arg); break;
  default: console.error("usage: node demo/ctl.js reset | break config|deploy|crash | restart | release <id>"); process.exit(2);
}
