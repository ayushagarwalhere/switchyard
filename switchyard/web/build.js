/* Build & ship panel: idea -> app (Nemotron) -> secure -> change -> deploy to Nebius. Local server only. */
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let appPath = null;
  let busy = false;

  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  }

  function log(text, append = true) {
    const el = $("#buildLog");
    el.hidden = false;
    el.textContent = append && el.textContent ? `${el.textContent}\n${text}` : text;
    el.scrollTop = el.scrollHeight;
  }

  function setBusy(on) {
    busy = on;
    document.querySelectorAll("#buildCard button").forEach((b) => (b.disabled = on));
  }

  async function follow(job, onDone) {
    let seen = 0;
    for (;;) {
      await new Promise((r) => setTimeout(r, 2500));
      const st = await api(`/api/jobs/${job.job_id}`);
      const lines = st.log || [];
      if (lines.length > seen) { lines.slice(seen).forEach((l) => log(l)); seen = lines.length; }
      if (st.status === "done") return onDone(st.result);
      if (st.status === "failed") { log(`Failed: ${st.error}`); return null; }
    }
  }

  async function run(fn) {
    if (busy) return;
    setBusy(true);
    try { await fn(); } catch (e) { log(`Error: ${e.message}`); } finally { setBusy(false); loadDeployments(); }
  }

  function showApp(res) {
    appPath = res.dest;
    $("#appPanel").hidden = false;
    $("#appTitle").textContent = res.plan?.title || res.plan?.name || "Your app";
    $("#appPath").textContent = res.dest;
  }

  $("#buildForm").onsubmit = (e) => {
    e.preventDefault();
    run(async () => {
      log("Nemotron Super is planning the app…", false);
      const job = await api("/api/build", { method: "POST", body: JSON.stringify({ idea: $("#idea").value }) });
      await follow(job, (res) => {
        if (!res) return;
        showApp(res);
        log(`\n${res.tests_ok ? "Tests pass" : "Tests fail"} · ${res.secure.findings} security finding(s) · $${res.usage.cost.toFixed(3)}`);
        log(`Run it: cd "${res.dest}" && pip install -r requirements.txt && uvicorn app.main:app --reload`);
        (res.plan?.features || []).slice(0, 5).forEach((f) => log(`  · ${f}`));
      });
    });
  };

  $("#changeForm").onsubmit = (e) => {
    e.preventDefault();
    const change = $("#changeText").value.trim();
    if (!change || !appPath) return;
    run(async () => {
      log(`Change: ${change}`, false);
      const job = await api("/api/change", { method: "POST", body: JSON.stringify({ path: appPath, change }) });
      await follow(job, (res) => {
        if (!res) return;
        log(`\n${res.summary}\n${res.diff_stat || ""}\nTests: ${res.tests_ok ? "pass" : "FAIL"} · $${res.usage.cost.toFixed(3)} · review with git diff`);
        $("#changeText").value = "";
      });
    });
  };

  $("#secureBtn").onclick = () => run(async () => {
    log("Checking secrets, dependencies, config and every LLM call…", false);
    const res = await api("/api/secure", { method: "POST", body: JSON.stringify({ path: appPath }) });
    if (!res.findings.length) log("No findings. Safe to ship.");
    res.findings.slice(0, 12).forEach((f) => log(`${f.severity} ${f.rule} ${f.file}:${f.line} ${f.title}: ${f.detail}`));
    res.notes.forEach((n) => log(`note: ${n}`));
    log(res.blocking ? `${res.blocking} blocking finding(s): fix them before deploying.` : "Nothing blocks a deploy.");
  });

  $("#deployBtn").onclick = () => {
    if (!confirm("Deploy to a Nebius Serverless endpoint? It bills until you delete it (Deployments, below).")) return;
    run(async () => {
      log("Deploying to Nebius…", false);
      const job = await api("/api/deploy", { method: "POST", body: JSON.stringify({ path: appPath, agreed: true }) });
      await follow(job, (res) => { if (res) log(`\nLive: ${res.url} (${res.healthy ? "healthy" : "not answering yet"})`); });
    });
  };

  async function loadDeployments() {
    const list = await api("/api/deployments").catch(() => []);
    $("#deployList").innerHTML = list.length ? `<h3 class="small" style="margin:14px 0 6px">Deployments</h3>` + list.map((d) =>
      `<div class="deploy-row small"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a>
        <span class="muted">${d.healthy ? "healthy" : "check status"}</span>
        <button class="link-btn" data-del="${esc(d.name)}" type="button">delete</button></div>`).join("") : "";
    document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => {
      if (!confirm(`Delete ${b.dataset.del}? Its URL stops working and billing stops.`)) return;
      run(async () => { await api(`/api/deployments/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); log(`Deleted ${b.dataset.del}.`, false); });
    }));
  }

  api("/api/config").then((cfg) => {
    if (!cfg.build) return;
    $("#buildCard").hidden = false;
    loadDeployments();
  }).catch(() => {});
})();
