/* Switchyard guide: a little 3D bird that walks visitors through the page, one step at a time.
   Self-contained: builds its own DOM, lazy-loads three.js (same build the hero uses, via the import map)
   only when it opens, and falls back to an SVG bird when WebGL or the model is unavailable.
   Opens from the floating button, from #guide in the URL, or once for first-time visitors. */
(function () {
  "use strict";

  const RM = window.matchMedia("(prefers-reduced-motion: reduce)");
  const SEEN_KEY = "sy-guide-seen";
  const MODEL_URL = "/assets/mascot.glb";
  const GLTF_URL = "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/GLTFLoader.js";

  // app.js rewrites the hash once it opens the featured run, so remember what the visitor arrived with,
  // and take #guide out before app.js reads it as a run id.
  const arrivedHash = location.hash;
  const wantGuide = arrivedHash === "#guide";
  if (wantGuide) history.replaceState(null, "", location.pathname + location.search);

  const q = (s, el = document) => el.querySelector(s);
  const visible = (el) => !!(el && el.isConnected && el.getClientRects().length);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
  async function waitFor(fn, ms = 4000) {
    const t0 = performance.now();
    while (performance.now() - t0 < ms) { const v = fn(); if (v) return v; await sleep(80); }
    return fn();
  }

  /* ---------- the walkthrough ----------------------------------------------------------------- */
  async function ensureRun() {
    if (q("#runView:not([hidden]) .tabs")) return true;
    // the run list may still be loading ("Loading…" until app.js fills it; "No runs yet." if empty)
    await waitFor(() => q("#runList button") || q("#runList li.small"), 6000);
    if (q("#runView:not([hidden]) .tabs")) return true;   // app.js opened the featured run meanwhile
    const btn = q("#runList .badge")?.closest("button") || q("#runList button");
    if (!btn) return false;
    btn.click();
    return !!(await waitFor(() => q("#runView:not([hidden]) .tabs")));
  }
  async function showTab(name) {
    if (!(await ensureRun())) return;
    const b = q(`#runView .tabs button[data-tab="${name}"]`);
    if (b && !b.classList.contains("active")) b.click();
    await frame();
  }
  const hasRuns = () => !!(q("#runList button") || q("#runView .tabs") || (q("#runList") && !q("#runList li.small")));
  const sandboxOn = () => visible(q("#sandboxView"));
  const deployTarget = () => (visible(q("#deployBtn")) ? q("#deployBtn").closest(".row2") || q("#deployBtn") : q("#buildCard"));

  const STEPS = [
    {
      title: "Hi, I'm your guide",
      text: "Switchyard is the Nebius ecosystem for everyday developers. It starts with routing: every AI call in your app moves to the cheapest NVIDIA Nemotron model that passes its tests.",
      target: () => [q(".hero-title"), q(".hero-sub")],
    },
    {
      title: "Build an app",
      text: "Describe an app in plain English and Nemotron writes it, with tests that run before you see it. Already have one? Open its folder here.",
      target: () => q("#buildCard"),
    },
    {
      title: "Try it in the sandbox",
      text: () => sandboxOn()
        ? "Your app runs here in a locked-down container on your machine. Click around the preview or browse its code."
        : "Once your app is built, Run in sandbox starts it in a locked-down local container and previews it right on this page.",
      target: () => (sandboxOn() ? q("#sandboxView") : q("#buildCard")),
    },
    {
      title: "Routing, on real repos",
      text: "These are recorded runs. Each one found a repo's AI calls, tested them on Nemotron, and switched the ones that passed.",
      target: () => q("#runList")?.closest(".card"),
    },
    {
      title: "Call sites",
      text: "Each card is one AI call: the model it used, the Nemotron model it moved to, and the tests behind the switch. Click a coloured square to read a test.",
      available: hasRuns,
      prepare: () => showTab("sites"),
      target: () => [q("#runView .tabs"), q('#runView .tab[data-pane="sites"] .site')],
    },
    {
      title: "Savings",
      text: "What the switch saves at your volume. Drag the slider to see the monthly bill before and after.",
      available: hasRuns,
      prepare: () => showTab("savings"),
      target: () => [q("#runView .tabs"), q('#runView .tab[data-pane="savings"] .money .hl') || q('#runView .tab[data-pane="savings"] .card')],
    },
    {
      title: "The patch",
      text: "The actual code change, compile-checked and ready to review, plus a security audit of every switched call.",
      available: hasRuns,
      prepare: () => showTab("patch"),
      target: () => [q("#runView .tabs"), q('#runView .tab[data-pane="patch"] .panel-grid') || q('#runView .tab[data-pane="patch"]')],
    },
    {
      title: "Free security audit",
      text: "Paste any public GitHub repo to check its AI calls for leaked keys and unsafe use of model output. Nothing runs and no key is needed.",
      target: () => q("#auditForm")?.closest(".card"),
    },
    {
      title: "Ship it, keep it running",
      text: () => `${visible(q("#deployBtn")) ? "Deploy to Nebius" : "Once your app is built, Deploy to Nebius"} puts it on a live URL, and on-call keeps it healthy. The runtime router and PR reviewer keep every request and every change on the right model.`,
      target: () => (visible(q("#buildCard")) ? deployTarget() : null),
    },
    {
      title: "That's the tour",
      text: "Want the deep dive? The Judge tour walks through one recorded run in three minutes. Thanks for stopping by!",
      target: () => (visible(q(".top-nav .nav-tour")) ? q(".top-nav .nav-tour") : q(".hero .btn-tour")),
    },
  ];
  const textOf = (s) => (typeof s.text === "function" ? s.text() : s.text);
  const targetsOf = (s) => [].concat(s.target() || []).filter(visible);

  /* ---------- DOM ------------------------------------------------------------------------------- */
  const BIRD_SVG = `<svg viewBox="0 0 64 64" aria-hidden="true" class="guide-svgbird">
    <ellipse cx="32" cy="58" rx="13" ry="2.6" fill="currentColor" opacity=".18"/>
    <path d="M25 52v4M39 52v4" stroke="#e48a4c" stroke-width="3" stroke-linecap="round"/>
    <ellipse cx="32" cy="34" rx="20" ry="20" fill="#fff1b0" stroke="#dea06b" stroke-width="2.5"/>
    <path class="gw-l" d="M13 34c-6 1-8 6-6 9 4-1 7-3 8-6" fill="#fff1b0" stroke="#dea06b" stroke-width="2.2" stroke-linejoin="round"/>
    <path class="gw-r" d="M51 34c6 1 8 6 6 9-4-1-7-3-8-6" fill="#fff1b0" stroke="#dea06b" stroke-width="2.2" stroke-linejoin="round"/>
    <ellipse cx="25" cy="31" rx="2.6" ry="3.6" fill="#6b4630"/><ellipse cx="39" cy="31" rx="2.6" ry="3.6" fill="#6b4630"/>
    <ellipse cx="20.5" cy="38" rx="3.6" ry="2.2" fill="#fdb98a" opacity=".85"/><ellipse cx="43.5" cy="38" rx="3.6" ry="2.2" fill="#fdb98a" opacity=".85"/>
    <path d="M28.5 36.5h7l-3.5 4.2z" fill="#ffae70" stroke="#e48a4c" stroke-width="1.2" stroke-linejoin="round"/>
  </svg>`;

  const launch = document.createElement("button");
  launch.type = "button";
  launch.className = "guide-launch";
  launch.hidden = true;
  launch.setAttribute("aria-label", "Take the tour of Switchyard with your guide");
  launch.innerHTML = `${BIRD_SVG}<span>Take the tour</span>`;

  const spot = document.createElement("div");
  spot.className = "guide-spot";
  spot.setAttribute("aria-hidden", "true");

  const root = document.createElement("div");
  root.className = "guide";
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", "Guided tour of Switchyard");
  root.innerHTML = `
    <div class="guide-bubble">
      <div class="guide-head"><span class="guide-who">Your guide</span><div class="guide-dots"></div></div>
      <h2 class="guide-title"></h2>
      <p class="guide-text" aria-hidden="true"><span class="guide-typed"></span><span class="guide-rest"></span></p>
      <p class="guide-sr" aria-live="polite"></p>
      <div class="guide-ctrl">
        <button type="button" class="guide-skip">Skip</button>
        <span class="guide-grow"></span>
        <button type="button" class="guide-back">Back</button>
        <button type="button" class="guide-next">Next</button>
      </div>
    </div>
    <div class="guide-bird" title="Hello!"><div class="guide-fallback">${BIRD_SVG}</div></div>`;

  document.body.append(spot, root, launch);

  // If anything on the page is wider than a phone screen, mobile browsers widen the layout viewport and
  // `position: fixed; right: …` lands off-screen. Pin the dock to what's actually visible instead.
  const vv = window.visualViewport;
  const probe = document.createElement("i");
  probe.className = "guide-probe";            // fixed at right/bottom 0: marks where fixed boxes anchor
  probe.setAttribute("aria-hidden", "true");
  document.body.append(probe);
  // the part of the layout viewport that's actually on screen
  const view = () => (vv ? { l: vv.offsetLeft, t: vv.offsetTop, r: vv.offsetLeft + vv.width, b: vv.offsetTop + vv.height }
    : { l: 0, t: 0, r: innerWidth, b: innerHeight });
  function pin() {
    let x0 = 0, gapR = 0, gapB = 0;
    if (vv && Math.abs(vv.scale - 1) < 0.01) {
      const p = probe.getBoundingClientRect(), v = view();
      x0 = Math.max(0, Math.round(v.l));
      gapR = Math.max(0, Math.round(p.left - v.r));
      gapB = Math.max(0, Math.round(p.top - v.b));
    }
    [root, launch].forEach((el) => {
      el.style.setProperty("--guide-x0", `${x0}px`);
      el.style.setProperty("--guide-gap", `${gapR}px`);
      el.style.setProperty("--guide-gap-b", `${gapB}px`);
    });
  }
  pin();
  vv?.addEventListener("resize", pin);
  new ResizeObserver(pin).observe(document.body);
  vv?.addEventListener("scroll", pin);
  window.addEventListener("resize", pin);
  const $title = q(".guide-title", root), $typed = q(".guide-typed", root), $rest = q(".guide-rest", root);
  const $sr = q(".guide-sr", root), $dots = q(".guide-dots", root);
  const $back = q(".guide-back", root), $next = q(".guide-next", root), $skip = q(".guide-skip", root);
  const $bird = q(".guide-bird", root), $bubble = q(".guide-bubble", root);

  /* ---------- state ----------------------------------------------------------------------------- */
  let open = false;
  let steps = [];
  let cur = -1;
  let seq = 0;
  let raf = 0;
  let typeTimer = 0;
  let talking = false;
  let morphUntil = 0;
  let lastFocus = null;
  const box = { x: 0, y: 0, w: 0, h: 0, on: false };

  /* ---------- spotlight -------------------------------------------------------------------------- */
  function unionRect(els) {
    let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
    els.forEach((el) => { const k = el.getBoundingClientRect(); l = Math.min(l, k.left); t = Math.min(t, k.top); r = Math.max(r, k.right); b = Math.max(b, k.bottom); });
    return { left: l, top: t, right: r, bottom: b, width: r - l, height: b - t };
  }
  const headerBottom = () => Math.max(0, q(".top")?.getBoundingClientRect().bottom || 0);
  function bubbleLimit(rect) {
    // how far down the page is free: stop at the bubble/bird if they sit over the target's columns
    let lim = view().b;
    [$bubble, $bird].forEach((el) => {
      const k = el.getBoundingClientRect();
      if (k.width && k.left < rect.right && k.right > rect.left) lim = Math.min(lim, k.top);
    });
    return lim;
  }
  function scrollToTarget(els) {
    if (els.some((el) => el.closest(".top")) && view().t < 1) return;     // already on screen in the sticky header
    const r = unionRect(els);
    const top = Math.max(headerBottom(), view().t) + 16;
    const bottom = bubbleLimit(r) - 16;
    if (r.top >= top && r.bottom <= bottom) return;
    const avail = bottom - top;
    const delta = r.height <= avail ? r.top - (top + (avail - r.height) * 0.3) : r.top - top;
    const y = Math.max(0, window.scrollY + delta);
    if (window.__lenis) window.__lenis.scrollTo(y, { immediate: RM.matches, duration: 1.1 });
    else window.scrollTo({ top: y, behavior: RM.matches ? "auto" : "smooth" });
  }
  function updateSpot(now) {
    const s = steps[cur];
    const els = s && box.on ? targetsOf(s) : [];
    if (!els.length) { spot.classList.remove("on"); return; }
    const r = unionRect(els), pad = 10, m = 6;
    let x = r.left - pad, y = r.top - pad, w = r.width + pad * 2, h = r.height + pad * 2;
    // keep the outline on screen when the target is taller than the viewport
    const v = view();
    const y0 = Math.max(y, v.t + m), y1 = Math.min(y + h, v.b - m);
    const x0 = Math.max(x, v.l + m), x1 = Math.min(x + w, v.r - m);
    x = x0; y = y0; w = Math.max(0, x1 - x0); h = Math.max(0, y1 - y0);
    if (now < morphUntil && !RM.matches) {
      const k = 0.18;
      box.x += (x - box.x) * k; box.y += (y - box.y) * k; box.w += (w - box.w) * k; box.h += (h - box.h) * k;
    } else Object.assign(box, { x, y, w, h });
    spot.style.transform = `translate(${box.x}px, ${box.y}px)`;
    spot.style.width = `${box.w}px`;
    spot.style.height = `${box.h}px`;
    spot.classList.toggle("on", h > 8);
  }

  /* ---------- speech ---------------------------------------------------------------------------- */
  function say(title, text) {
    clearInterval(typeTimer);
    $title.textContent = title;
    $sr.textContent = `${title}. ${text}`;
    if (RM.matches) { $typed.textContent = text; $rest.textContent = ""; talking = false; bird?.still(); return; }
    let n = 0;
    talking = true;
    $typed.textContent = "";
    $rest.textContent = text;            // invisible: holds the bubble's final size while typing
    typeTimer = setInterval(() => {
      n = Math.min(text.length, n + 2);
      $typed.textContent = text.slice(0, n);
      $rest.textContent = text.slice(n);
      if (n >= text.length) { clearInterval(typeTimer); talking = false; }
    }, 26);
  }
  function finishTyping() {
    if (!talking) return;
    clearInterval(typeTimer);
    $typed.textContent += $rest.textContent;
    $rest.textContent = "";
    talking = false;
  }

  /* ---------- steps ----------------------------------------------------------------------------- */
  function renderDots() {
    $dots.innerHTML = steps.map((s, i) => `<button type="button" data-i="${i}" aria-label="Step ${i + 1} of ${steps.length}: ${s.title}"></button>`).join("");
  }
  async function show(idx, dir = 1) {
    const my = ++seq;
    let i = idx;
    for (; i >= 0 && i < steps.length; i += dir) {
      const s = steps[i];
      if (s.prepare) { try { await s.prepare(); } catch (_) { /* step is skipped below if its target is missing */ } }
      if (my !== seq || !open) return;
      if (targetsOf(s).length) break;
    }
    if (i >= steps.length) { if (dir > 0) close(); return; }
    if (i < 0) return;
    cur = i;
    const s = steps[i];
    [...$dots.children].forEach((d, k) => { d.classList.toggle("on", k === i); d.classList.toggle("done", k < i); d.setAttribute("aria-current", k === i ? "step" : "false"); });
    $back.disabled = i === 0;
    const last = i === steps.length - 1;
    $next.textContent = last ? "Done" : "Next";
    $skip.hidden = last;
    box.on = true;
    morphUntil = performance.now() + 900;
    say(s.title, textOf(s));
    bird?.hop();
    await frame();
    if (my !== seq) return;
    scrollToTarget(targetsOf(s));
  }
  const next = () => (cur >= steps.length - 1 ? close() : show(cur + 1, 1));
  const back = () => (cur > 0 ? show(cur - 1, -1) : null);

  function openGuide(how = "button") {
    if (open) return;
    if (document.body.classList.contains("touring")) {
      if (how === "auto") return;
      window.Tour?.close(true);
    }
    steps = STEPS.filter((s) => (s.available ? s.available() : targetsOf(s).length > 0));
    if (!steps.length) return;
    open = true;
    try { localStorage.setItem(SEEN_KEY, "1"); } catch (_) { /* private mode: fine */ }
    lastFocus = document.activeElement;
    renderDots();
    root.hidden = false;
    launch.hidden = true;
    document.body.classList.add("guiding");
    requestAnimationFrame(() => root.classList.add("in"));
    initBird();
    loop();
    show(0, 1);
    if (how !== "auto") $next.focus({ preventScroll: true });
  }
  function close() {
    if (!open) return;
    open = false;
    seq++;
    clearInterval(typeTimer);
    talking = false;
    box.on = false;
    spot.classList.remove("on");
    root.classList.remove("in");
    root.hidden = true;
    document.body.classList.remove("guiding");
    cancelAnimationFrame(raf); raf = 0;
    if (!document.body.classList.contains("touring")) launch.hidden = false;
    if (root.contains(document.activeElement) || document.activeElement === document.body) {
      (lastFocus && lastFocus.isConnected && lastFocus !== document.body ? lastFocus : launch).focus({ preventScroll: true });
    }
  }

  function loop() {
    if (raf) return;
    const tick = (now) => {
      raf = 0;
      if (!open || document.hidden) return;
      updateSpot(now);
      bird?.frame(now / 1000, talking);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
  }

  $next.onclick = next;
  $back.onclick = back;
  $skip.onclick = close;
  launch.onclick = () => openGuide("button");
  $dots.addEventListener("click", (e) => { const b = e.target.closest("[data-i]"); if (b) show(+b.dataset.i, +b.dataset.i >= cur ? 1 : -1); });
  q(".guide-text", root).addEventListener("click", finishTyping);
  $bird.addEventListener("click", () => bird?.hop(true));

  document.addEventListener("keydown", (e) => {
    if (!open || e.altKey || e.ctrlKey || e.metaKey || q("dialog[open]")) return;
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.target.closest?.("input, textarea, select, [contenteditable='true'], [contenteditable='']")) return;
    if (e.key === "ArrowRight") { e.preventDefault(); next(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); back(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && open) loop(); });
  window.addEventListener("hashchange", () => {
    if (location.hash !== "#guide") return;
    history.replaceState(null, "", location.pathname + location.search);
    openGuide("hash");
  });
  // the Judge tour takes over the page: step aside while it's open
  new MutationObserver(() => {
    const touring = document.body.classList.contains("touring");
    if (touring) { close(); launch.hidden = true; } else if (!open) launch.hidden = false;
  }).observe(document.body, { attributes: true, attributeFilter: ["class"] });

  /* ---------- the bird (three.js) ------------------------------------------------------------------ */
  let bird = null;
  let birdTried = false;
  let yawTarget = -0.3;
  window.addEventListener("pointermove", (e) => {
    if (!open) return;
    yawTarget = -0.3 + (e.clientX / innerWidth - 0.75) * 0.6;
  }, { passive: true });

  async function initBird() {
    if (birdTried) return;
    birdTried = true;
    try {
      const THREE = await import("three");
      const { GLTFLoader } = await import(GLTF_URL);
      const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "low-power" });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setClearColor(0x000000, 0);
      const gltf = await new GLTFLoader().loadAsync(MODEL_URL);
      bird = makeBird(THREE, renderer, gltf);
      $bird.prepend(renderer.domElement);
      renderer.domElement.addEventListener("webglcontextlost", (e) => { e.preventDefault(); bird = null; $bird.classList.remove("gl"); renderer.domElement.remove(); });
      $bird.classList.add("gl");
      bird.still();
    } catch (err) {
      bird = null;          // the SVG bird stays
      $bird.classList.add("nogl");
    }
  }

  function makeBird(THREE, renderer, gltf) {
    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(24, 1, 0.01, 20);
    cam.position.set(0, 0.34, 1.62);
    cam.lookAt(0, 0.215, 0);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x404040, 0.5));
    const rig = new THREE.Group();
    const model = gltf.scene;
    rig.add(model);
    scene.add(rig);
    model.traverse((o) => { if (o.isMesh) o.frustumCulled = false; });
    model.updateMatrixWorld(true);

    const bones = {};
    model.traverse((o) => { if (o.isBone) { const k = o.name.replace(/_\d+$/, ""); if (!bones[k]) bones[k] = o; } });
    const rest = new Map();
    Object.values(bones).forEach((b) => rest.set(b, { q: b.quaternion.clone(), s: b.scale.clone() }));
    const pq = new THREE.Quaternion();
    // a world-space axis expressed in the bone's parent space, so rotations read the same on every bone
    const axisIn = (b, v) => { b.parent.getWorldQuaternion(pq); return v.clone().applyQuaternion(pq.invert()).normalize(); };
    const X = new THREE.Vector3(1, 0, 0), Z = new THREE.Vector3(0, 0, 1);
    const ax = {};
    ["Arm_L", "Arm_R", "Hand_L", "Hand_R"].forEach((n) => bones[n] && (ax[n] = axisIn(bones[n], Z)));
    ["BeakBottom", "BeakTop", "Face"].forEach((n) => bones[n] && (ax[n] = axisIn(bones[n], X)));
    // the model's eyes are slanted dashes; level them and make them short ovals for a friendlier guide.
    // On these bones local x runs along the dash and local z across it, so z is what closes in a blink.
    [["Eye_L", -0.3], ["Eye_R", 0.3]].forEach(([n, a]) => {
      const b = bones[n];
      if (!b) return;
      const r = rest.get(b);
      r.q.premultiply(new THREE.Quaternion().setFromAxisAngle(axisIn(b, Z), a));
      b.quaternion.copy(r.q);
      r.s.x *= 0.42;
      r.s.z *= 2.0;
    });

    const tq = new THREE.Quaternion();
    const turn = (n, angle) => {
      const b = bones[n], a = ax[n];
      if (!b || !a) return;
      tq.setFromAxisAngle(a, angle);
      b.quaternion.copy(tq).multiply(rest.get(b).q);
    };
    const squash = (n, k) => {
      const b = bones[n];
      if (!b) return;
      b.scale.copy(rest.get(b).s);
      b.scale.z *= k;
    };

    let size = 0;
    function fit() {
      const w = Math.round($bird.clientWidth) || 120;
      if (w === size) return;
      size = w;
      renderer.setSize(w, w, false);
    }
    new ResizeObserver(() => { fit(); if (RM.matches || !open) draw(); }).observe($bird);

    let yaw = -0.3, beak = 0, flap = 0, hopT = 1, blinkAt = 2, last = 0;
    function pose(t, dt, isTalking) {
      const calm = RM.matches;
      yaw += (yawTarget - yaw) * Math.min(1, dt * 3);
      rig.rotation.y = calm ? -0.3 : yaw;
      hopT = Math.min(1, hopT + dt * 2.2);
      const hop = calm ? 0 : Math.sin(hopT * Math.PI) * 0.035;
      rig.position.y = calm ? 0 : Math.sin(t * 2.1) * 0.01 + hop;
      rig.rotation.z = calm ? 0 : Math.sin(t * 1.05) * 0.03;
      flap = Math.max(0, flap - dt * 1.4);
      const wing = calm ? 0 : Math.sin(t * 2.4) * 0.07 + Math.sin(t * 17) * 0.5 * flap;
      turn("Arm_L", wing); turn("Arm_R", -wing);
      turn("Hand_L", wing * 0.6); turn("Hand_R", -wing * 0.6);
      const want = isTalking && !calm ? (0.5 + 0.5 * Math.sin(t * 20)) * 0.42 : 0;
      beak += (want - beak) * Math.min(1, dt * 20);
      turn("BeakBottom", beak);
      turn("BeakTop", -beak * 0.25);
      turn("Face", calm ? 0 : Math.sin(t * 0.9) * 0.04 + beak * 0.1);
      let lid = 1;
      if (!calm) {
        if (t > blinkAt + 0.16) blinkAt = t + 2.2 + Math.random() * 3;
        if (t >= blinkAt) lid = Math.abs((t - blinkAt) / 0.08 - 1) * 0.9 + 0.1;
      }
      squash("Eye_L", lid); squash("Eye_R", lid);
    }
    function draw() { fit(); renderer.render(scene, cam); }
    return {
      frame(t, isTalking) {
        if (RM.matches) return;
        const dt = Math.min(0.05, last ? t - last : 0.016);
        last = t;
        pose(t, dt, isTalking);
        draw();
      },
      still() { pose(0, 0.016, false); draw(); },
      hop(big) { if (!RM.matches) { hopT = 0; flap = big ? 1 : 0.7; } },
    };
  }

  /* ---------- launch -------------------------------------------------------------------------- */
  function boot() {
    if (!document.body.classList.contains("touring")) launch.hidden = false;
    if (wantGuide) { setTimeout(() => openGuide("hash"), 400); return; }
    let seen = true;
    try { seen = localStorage.getItem(SEEN_KEY) === "1"; } catch (_) { /* no storage: don't nag every visit */ }
    if (seen || arrivedHash) return;
    // first visit: wait for the page (and the recorded runs) to settle, then say hello once
    const go = async () => {
      await waitFor(() => q("#runList button") || !q("#runList .muted"), 5000);
      await sleep(1400);
      if (open || document.hidden || document.body.classList.contains("touring")) return;
      if (window.scrollY > innerHeight * 0.6) return;     // they're already exploring; the button is there
      openGuide("auto");
    };
    if (document.readyState === "complete") go(); else window.addEventListener("load", go, { once: true });
  }
  boot();

  window.Guide = { open: () => openGuide("button"), close, next, back };
})();
