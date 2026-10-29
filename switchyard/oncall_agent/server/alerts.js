/* Turns webhook bodies from common monitoring tools into { service, source, summary } alerts.
   Only firing / down alerts are returned; resolved ones are ignored (WisprOps proves recovery itself).
   Supported: Prometheus Alertmanager, Grafana alerting, Uptime Kuma, and a generic
   {"service": "...", "summary": "..."} body. ?service=<id> on the URL overrides the service. */
"use strict";

const pick = (o, keys) => { for (const k of keys) if (o && o[k]) return String(o[k]); return ""; };

function parseAlerts(body, serviceOverride) {
  const out = [];
  const push = (service, source, summary) => { service = serviceOverride || service; if (service) out.push({ service: String(service).slice(0, 100), source, summary: String(summary || "alert firing").replace(/\s+/g, " ").slice(0, 500) }); };
  if (!body || typeof body !== "object") return out;

  // Alertmanager and Grafana unified alerting share this shape.
  if (Array.isArray(body.alerts)) {
    const source = body.orgId != null || body.title || /grafana/i.test(String(body.externalURL || "")) ? "Grafana" : "Alertmanager";
    for (const a of body.alerts.slice(0, 50)) {
      if (!a || typeof a !== "object") continue;
      if (a.status && a.status !== "firing") continue;
      const l = a.labels || {}, an = a.annotations || {};
      push(pick(l, ["wisprops_service", "service", "app", "job", "container", "deployment", "instance"]), source,
        [l.alertname, pick(an, ["summary", "description", "message"])].filter(Boolean).join(": "));
    }
    return out;
  }
  // Uptime Kuma: heartbeat.status 0 = down, 1 = up.
  if (body.monitor && typeof body.monitor === "object" && (body.heartbeat || body.msg)) {
    if (body.heartbeat && Number(body.heartbeat.status) !== 0) return out;
    push(body.monitor.name, "Uptime Kuma", (body.heartbeat && body.heartbeat.msg) || body.msg);
    return out;
  }
  // Generic.
  if (body.status && /^(resolved|ok|up)$/i.test(String(body.status))) return out;
  push(pick(body, ["service", "serviceId", "name"]), String(body.source || "webhook").slice(0, 40), pick(body, ["summary", "message", "text", "title"]));
  return out;
}

module.exports = { parseAlerts };
