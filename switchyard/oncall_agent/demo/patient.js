/* Demo service for WisprOps: a tiny "checkout-api" with real failure modes.
   It reads demo/runtime/config.json and demo/runtime/release.txt on every request, and stops
   listening while demo/runtime/down exists (a crashed process, as seen from outside). */
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const RT = path.join(__dirname, "runtime");
const PORT = Number(process.env.PATIENT_PORT || 7301);
const log = (line) => fs.appendFileSync(path.join(RT, "patient.log"), `${new Date().toISOString()} ${line}\n`);
const read = (f, d) => { try { return fs.readFileSync(path.join(RT, f), "utf8"); } catch (e) { return d; } };

let server = null;
function handler(req, res) {
  let cfg = {};
  try { cfg = JSON.parse(read("config.json", "{}")); } catch (e) { cfg = { __broken: true }; }
  const release = read("release.txt", "v41").trim();
  if (req.url === "/healthz" || req.url === "/") {
    if (cfg.__broken) { log("ERROR config.json is not valid JSON"); res.writeHead(500); return res.end("config unreadable"); }
    if (!(cfg.db_pool > 0)) { log(`ERROR db pool exhausted (db_pool=${cfg.db_pool}), request failed`); res.writeHead(500); return res.end("db pool exhausted"); }
    if (/bad|v42/.test(release)) { log("ERROR TypeError: Cannot read properties of undefined (reading 'currency') at pricing.js:88"); res.writeHead(500); return res.end("TypeError in pricing"); }
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, release, db_pool: cfg.db_pool }));
  }
  res.writeHead(404); res.end();
}
function listen() { server = http.createServer(handler); server.listen(PORT, "127.0.0.1", () => log(`INFO listening on :${PORT} (release ${read("release.txt", "v41").trim()})`)); }
function stop() { if (server) { server.close(); server.closeAllConnections && server.closeAllConnections(); server = null; log("FATAL process stopped answering"); } }

listen();
setInterval(() => {
  const down = fs.existsSync(path.join(RT, "down"));
  if (down && server) stop();
  if (!down && !server) listen();
}, 300);
console.log(`demo checkout-api on http://127.0.0.1:${PORT}/healthz`);
