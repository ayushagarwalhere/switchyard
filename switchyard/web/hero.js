// Switchyard hero: a calm 3D rail yard at dusk. Requests (glowing particles) roll in on one
// track and get switched onto three Nemotron tracks: most go to Lightning, fewer to Super,
// a few to Ultra. Loaded as a module so a CDN failure never breaks the app itself.
import * as THREE from "three";

const hero = document.getElementById("hero");
const mount = document.getElementById("heroCanvas");
const label = document.getElementById("heroLabel");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- smooth scroll (Lenis) ---------------------------------------- */
let lenis = null;
async function initLenis() {
  if (reduceMotion) return;
  try {
    const mod = await import("https://cdn.jsdelivr.net/npm/lenis@1.1.13/dist/lenis.mjs");
    const Lenis = mod.default || mod.Lenis;
    lenis = new Lenis({ duration: 1.15, easing: (t) => 1 - Math.pow(1 - t, 4), smoothWheel: true });
    window.__lenis = lenis;
    const raf = (time) => { lenis.raf(time); requestAnimationFrame(raf); };
    requestAnimationFrame(raf);
    document.documentElement.classList.add("lenis");
  } catch (_) { /* native scroll is fine */ }
}
function scrollToEl(sel) {
  const el = document.querySelector(sel);
  if (!el) return;
  if (lenis) lenis.scrollTo(el, { offset: -72 });
  else el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}
window.SY_scrollTo = scrollToEl;
// delegated so links added later (judge tour) work too
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-scroll]");
  if (!a) return;
  e.preventDefault();
  if (document.body.classList.contains("touring")) window.Tour?.close(true);
  requestAnimationFrame(() => scrollToEl(a.dataset.scroll));
  if (a.dataset.focus) setTimeout(() => document.querySelector(a.dataset.focus)?.focus({ preventScroll: true }), 900);
});
initLenis();

/* ---------- header state + first-appearance animations -------------------- */
const top = document.querySelector(".top");
const onScroll = () => top?.classList.toggle("scrolled", window.scrollY > 40);
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

// Animate a call-site card only the first time it appears (app.js re-renders on every update).
const seen = new Set();
new MutationObserver(() => {
  document.querySelectorAll(".site:not([data-seen])").forEach((el, i) => {
    el.dataset.seen = "1";
    const key = (el.querySelector(".site-h .fn")?.textContent || "") + (el.querySelector(".site-h .loc")?.textContent || "") || Math.random();
    if (!seen.has(key)) {
      seen.add(key);
      el.style.animationDelay = `${Math.min(i, 6) * 70}ms`;
      el.classList.add("enter");
    }
  });
}).observe(document.body, { childList: true, subtree: true });

/* ---------- WebGL scene ---------------------------------------------------- */
function webglOK() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch (_) { return false; }
}
if (!mount || !webglOK()) {
  hero?.classList.add("hero--fallback");
} else {
  try { initScene(); } catch (e) { console.warn("hero scene disabled:", e); hero.classList.add("hero--fallback"); }
}

function palette() {
  const light = document.documentElement.dataset.theme === "light";
  return light
    // Faultline-style neutrals: a near-black (or paper) canvas with only a hint of ember on the horizon
    ? { skyTop: 0xf6f6f4, skyMid: 0xf1f0ee, horizon: 0xf3e7de, fog: 0xefeeec, ground: 0xebeae7, rail: 0x6b6f78, tie: 0xc9c7c2, main: 0x3f4350, dust: 0x8a8f98, light: true }
    : { skyTop: 0x08090a, skyMid: 0x0d0e10, horizon: 0x2a1c1a, fog: 0x121315, ground: 0x0a0b0c, rail: 0x8a8f98, tie: 0x24262b, main: 0xf7f8f8, dust: 0x8a8f98, light: false };
}

function initScene() {
  const BRANCH = [
    { key: "lightning", color: 0x4fdcae, weight: 0.62, title: "Nemotron Lightning", sub: "fast, cheap: classification, extraction, routing" },
    { key: "super", color: 0x9a7cff, weight: 0.28, title: "Nemotron Super", sub: "balanced: summaries, structured writing" },
    { key: "ultra", color: 0xff7a38, weight: 0.10, title: "Nemotron Ultra", sub: "deep reasoning, only where it's needed" },
  ];

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  mount.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 400);
  const lookAt = new THREE.Vector3(5, 0.6, -6);
  const camBase = new THREE.Vector3(-11, 4.2, 15);

  let P = palette();
  scene.fog = new THREE.FogExp2(P.fog, 0.034);

  /* sky dome with a soft vertical gradient */
  const skyMat = new THREE.ShaderMaterial({
    side: THREE.BackSide, depthWrite: false, fog: false,
    uniforms: { top: { value: new THREE.Color(P.skyTop) }, mid: { value: new THREE.Color(P.skyMid) }, hor: { value: new THREE.Color(P.horizon) }, fogc: { value: new THREE.Color(P.fog) }, glowAmt: { value: P.light ? 0.22 : 0.30 } },
    vertexShader: `varying vec3 vP; void main(){ vP = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
    // starts exactly at the fog colour on the horizon (no seam with the ground), warms up just
    // above it, then fades to night
    fragmentShader: `uniform vec3 top; uniform vec3 mid; uniform vec3 hor; uniform vec3 fogc; uniform float glowAmt; varying vec3 vP;
      void main(){ float h = vP.y;
        vec3 c = mix(fogc, hor, smoothstep(0.0, 0.10, h));
        c = mix(c, mid, smoothstep(0.10, 0.34, h));
        c = mix(c, top, smoothstep(0.28, 0.8, h));
        // warm afterglow toward the setting sun, faded out right at the horizon line
        vec3 sunDir = normalize(vec3(0.42, 0.0, -0.9));
        float s = max(dot(normalize(vec3(vP.x, 0.0, vP.z)), sunDir), 0.0);
        float glow = pow(s, 6.0) * exp(-max(h, 0.0) * 7.0) * smoothstep(0.0, 0.12, h);
        c += vec3(1.0, 0.55, 0.32) * glow * glowAmt;
        gl_FragColor = vec4(c, 1.0);
#include <colorspace_fragment>
      }`,
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(200, 32, 16), skyMat);
  scene.add(sky);

  const glowTex = makeGlowTexture();

  /* ground + a faint grid; built-in materials so fog and colour management match the sky */
  const groundMat = new THREE.MeshBasicMaterial({ color: P.ground });
  const ground = new THREE.Mesh(new THREE.CircleGeometry(260, 64), groundMat);
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.02;
  scene.add(ground);
  const grid = new THREE.GridHelper(400, 100, P.rail, P.rail);
  grid.material.transparent = true;
  grid.material.opacity = P.light ? 0.12 : 0.07;
  grid.material.depthWrite = false;
  grid.position.y = -0.01;
  scene.add(grid);

  /* tracks: one shared trunk, then three branches fanning out */
  const trunkPts = [new THREE.Vector3(-46, 0, 10), new THREE.Vector3(-24, 0, 6), new THREE.Vector3(-8, 0, 2.2), new THREE.Vector3(0, 0, 0)];
  const branchPts = [
    [new THREE.Vector3(8, 0, -1.6), new THREE.Vector3(22, 0, -4.5), new THREE.Vector3(60, 0, -12)],
    [new THREE.Vector3(8, 0, -3.2), new THREE.Vector3(20, 0, -12), new THREE.Vector3(48, 0, -34)],
    [new THREE.Vector3(7, 0, -4.6), new THREE.Vector3(14, 0, -19), new THREE.Vector3(26, 0, -56)],
  ];
  const curves = branchPts.map((pts) => new THREE.CatmullRomCurve3([...trunkPts, ...pts], false, "centripetal", 0.5));
  const trunkLen = new THREE.CatmullRomCurve3(trunkPts).getLength();
  const SPLIT = curves.map((c) => trunkLen / c.getLength()); // fraction of each curve that is shared trunk

  const railMats = [];
  const hitMeshes = [];
  const tieMat = new THREE.MeshBasicMaterial({ color: P.tie });
  const tieGeo = new THREE.BoxGeometry(0.22, 0.06, 1.9);
  curves.forEach((curve, bi) => {
    const color = new THREE.Color(BRANCH[bi].color);
    const n = 220;
    for (const side of [-0.55, 0.55]) {
      const pts = [];
      for (let i = 0; i <= n; i++) {
        const t = i / n;
        const p = curve.getPointAt(t);
        const tan = curve.getTangentAt(t);
        const nor = new THREE.Vector3(-tan.z, 0, tan.x).normalize();
        pts.push(p.clone().addScaledVector(nor, side).setY(0.06));
      }
      // vertex colours: trunk part neutral, branch part tinted
      const geo = new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts), n, 0.05, 5, false);
      const cols = [];
      const pos = geo.attributes.position;
      for (let i = 0; i < pos.count; i++) {
        const seg = Math.floor(i / 6) / n;
        const k = THREE.MathUtils.smoothstep(seg, SPLIT[bi] - 0.02, SPLIT[bi] + 0.08);
        const c = new THREE.Color(P.main).lerp(color, k);
        cols.push(c.r, c.g, c.b);
      }
      geo.setAttribute("color", new THREE.Float32BufferAttribute(cols, 3));
      const mat = new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.72 });
      railMats[bi] = railMats[bi] || [];
      railMats[bi].push(mat);
      scene.add(new THREE.Mesh(geo, mat));
    }
    // sleepers along the branch part (trunk gets them once, from branch 0)
    const start = bi === 0 ? 0 : SPLIT[bi] + 0.02;
    const count = Math.floor((1 - start) * curve.getLength() / 1.25);
    const ties = new THREE.InstancedMesh(tieGeo, tieMat, count);
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    for (let i = 0; i < count; i++) {
      const t = start + (i / count) * (1 - start);
      const p = curve.getPointAt(t);
      const tan = curve.getTangentAt(t);
      q.setFromUnitVectors(new THREE.Vector3(1, 0, 0), new THREE.Vector3(tan.x, 0, tan.z).normalize());
      m.compose(p.setY(0.02), q, new THREE.Vector3(1, 1, 1));
      ties.setMatrixAt(i, m);
    }
    scene.add(ties);
    // invisible fat tube for hover picking (branch part only)
    const branchOnly = [];
    for (let i = 0; i <= 60; i++) branchOnly.push(curve.getPointAt(SPLIT[bi] + (i / 60) * (1 - SPLIT[bi])));
    const hit = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(branchOnly), 60, 1.6, 6, false),
      new THREE.MeshBasicMaterial({ visible: false }));
    hit.userData.branch = bi;
    hitMeshes.push(hit);
    scene.add(hit);
    // signal lamps along each branch
    for (let i = 1; i <= 4; i++) {
      const t = SPLIT[bi] + (i / 5) * (1 - SPLIT[bi]);
      const p = curve.getPointAt(t);
      const tan = curve.getTangentAt(t);
      const nor = new THREE.Vector3(-tan.z, 0, tan.x).normalize();
      const lamp = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, color, transparent: true, opacity: 0.65, depthWrite: false, blending: THREE.AdditiveBlending }));
      lamp.position.copy(p).addScaledVector(nor, -1.6).setY(1.5);
      lamp.scale.setScalar(1.3);
      lamp.userData = { branch: bi, base: 0.65 };
      scene.add(lamp);
      const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 1.5, 6), new THREE.MeshBasicMaterial({ color: P.tie }));
      pole.position.copy(lamp.position).setY(0.75);
      scene.add(pole);
      (railMats[bi].lamps ||= []).push(lamp);
    }
  });

  /* the switch: a soft pulse where the tracks part */
  const switchGlow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, color: 0xcfe6ff, transparent: true, opacity: 0.35, depthWrite: false, blending: THREE.AdditiveBlending }));
  switchGlow.position.set(1.2, 0.4, -0.6);
  switchGlow.scale.setScalar(5);
  scene.add(switchGlow);

  /* requests: glowing particles with short trails */
  const N = 150, TRAIL = 4;
  const total = N * TRAIL;
  const pGeo = new THREE.BufferGeometry();
  const pPos = new Float32Array(total * 3);
  const pCol = new Float32Array(total * 3);
  const pSize = new Float32Array(total);
  const pAlpha = new Float32Array(total);
  pGeo.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
  pGeo.setAttribute("color", new THREE.BufferAttribute(pCol, 3));
  pGeo.setAttribute("size", new THREE.BufferAttribute(pSize, 1));
  pGeo.setAttribute("alpha", new THREE.BufferAttribute(pAlpha, 1));
  const pMat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { scale: { value: 1 } },
    vertexShader: `attribute float size; attribute float alpha; attribute vec3 color; varying vec3 vC; varying float vA; uniform float scale;
      void main(){ vC = color; vA = alpha; vec4 mv = modelViewMatrix * vec4(position,1.0); gl_PointSize = size * scale * (150.0 / -mv.z); gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `varying vec3 vC; varying float vA;
      void main(){ float d = length(gl_PointCoord - 0.5); float a = smoothstep(0.5, 0.0, d); a *= a; gl_FragColor = vec4(vC * (0.6 + a), a * vA); }`,
  });
  scene.add(new THREE.Points(pGeo, pMat));
  const pick = () => { const r = Math.random(); return r < BRANCH[0].weight ? 0 : r < BRANCH[0].weight + BRANCH[1].weight ? 1 : 2; };
  const parts = Array.from({ length: N }, () => ({ b: pick(), t: Math.random(), v: 0.018 + Math.random() * 0.012, lane: (Math.random() - 0.5) * 0.5 }));
  const branchCols = BRANCH.map((b) => new THREE.Color(b.color));
  const white = new THREE.Color(0xf4f8ff);
  const tmp = new THREE.Vector3(), tan = new THREE.Vector3(), col = new THREE.Color();

  /* ambient dust drifting in the air */
  const D = 420;
  const dGeo = new THREE.BufferGeometry();
  const dPos = new Float32Array(D * 3);
  for (let i = 0; i < D; i++) { dPos[i * 3] = (Math.random() - 0.4) * 110; dPos[i * 3 + 1] = Math.random() * 14 + 0.3; dPos[i * 3 + 2] = (Math.random() - 0.7) * 90; }
  dGeo.setAttribute("position", new THREE.BufferAttribute(dPos, 3));
  const dMat = new THREE.PointsMaterial({ map: glowTex, color: P.dust, size: 0.16, transparent: true, opacity: 0.5, depthWrite: false, blending: THREE.AdditiveBlending });
  const dust = new THREE.Points(dGeo, dMat);
  scene.add(dust);

  /* interaction */
  const mouse = new THREE.Vector2(0, 0), target = new THREE.Vector2(0, 0);
  const ray = new THREE.Raycaster();
  let hovered = -1, pointerInside = false, lastPointer = { x: 0, y: 0 };
  hero.addEventListener("pointermove", (e) => {
    const r = mount.getBoundingClientRect();
    target.set(((e.clientX - r.left) / r.width) * 2 - 1, -(((e.clientY - r.top) / r.height) * 2 - 1));
    lastPointer = { x: e.clientX - r.left, y: e.clientY - r.top };
    pointerInside = true;
  });
  hero.addEventListener("pointerleave", () => { pointerInside = false; target.set(0, 0); });

  function setHover(bi) {
    if (bi === hovered) return;
    hovered = bi;
    hero.classList.toggle("hovering", bi >= 0);
    if (bi >= 0) {
      label.innerHTML = `<b style="color:#${BRANCH[bi].color.toString(16).padStart(6, "0")}">${BRANCH[bi].title}</b><span>${BRANCH[bi].sub}</span>`;
      label.classList.add("show");
    } else label.classList.remove("show");
  }

  /* sizing + visibility */
  function resize() {
    const w = mount.clientWidth, h = mount.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.fov = w < 700 ? 58 : 38;
    // shift the frustum so the switch sits right of the headline (desktop only)
    if (w >= 1000) camera.setViewOffset(w, h, -w * 0.17, h * 0.04, w, h); else camera.clearViewOffset();
    camera.updateProjectionMatrix();
    pMat.uniforms.scale.value = renderer.getPixelRatio() * (h / 900) * 1.4;
  }
  new ResizeObserver(resize).observe(mount);
  resize();

  let visible = true, running = false;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; kick(); }, { threshold: 0.01 }).observe(hero);
  document.addEventListener("visibilitychange", kick);

  /* theme changes recolour the scene */
  new MutationObserver(() => {
    P = palette();
    scene.fog.color.set(P.fog);
    skyMat.uniforms.top.value.set(P.skyTop); skyMat.uniforms.mid.value.set(P.skyMid); skyMat.uniforms.hor.value.set(P.horizon); skyMat.uniforms.fogc.value.set(P.fog);
    groundMat.color.set(P.ground); grid.material.color.set(P.rail); grid.material.opacity = P.light ? 0.12 : 0.07;
    tieMat.color.set(P.tie); dMat.color.set(P.dust);
    white.set(P.light ? 0x4a5a70 : 0xf4f8ff);
    skyMat.uniforms.glowAmt.value = P.light ? 0.22 : 0.30;
    pMat.blending = P.light ? THREE.NormalBlending : THREE.AdditiveBlending; pMat.needsUpdate = true;
    applyGlowMode();
    if (!running) renderFrame(0, performance.now() / 1000);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  function applyGlowMode() {
    const lampBase = P.light ? 0.32 : 0.65;
    railMats.forEach((mats) => (mats.lamps || []).forEach((l) => {
      l.userData.base = lampBase;
      l.material.blending = P.light ? THREE.NormalBlending : THREE.AdditiveBlending;
      l.material.needsUpdate = true;
    }));
    switchGlow.material.blending = P.light ? THREE.NormalBlending : THREE.AdditiveBlending;
    switchGlow.material.color.set(P.light ? 0xfff3e6 : 0xcfe6ff);
    switchGlow.material.needsUpdate = true;
    switchGlow.userData.k = P.light ? 0.45 : 1;
  }
  if (P.light) { white.set(0x4a5a70); pMat.blending = THREE.NormalBlending; }
  applyGlowMode();

  const clock = new THREE.Clock();
  function renderFrame(dt, time) {
    // camera: slow drift + eased parallax
    mouse.lerp(target, Math.min(1, dt * 2.2));
    const drift = reduceMotion ? 0 : Math.sin(time * 0.07) * 1.6;
    camera.position.set(camBase.x + mouse.x * 2.4 + drift, camBase.y + mouse.y * 1.1 + (reduceMotion ? 0 : Math.sin(time * 0.11) * 0.25), camBase.z - mouse.x * 0.6);
    camera.lookAt(lookAt);

    // particles
    for (let i = 0; i < N; i++) {
      const p = parts[i];
      if (!reduceMotion) p.t += p.v * dt * (0.75 + (p.t < SPLIT[p.b] ? 0.0 : 0.35));
      if (p.t > 1) { p.t -= 1; p.b = pick(); }
      for (let k = 0; k < TRAIL; k++) {
        const tt = Math.max(0, p.t - k * 0.0045);
        const curve = curves[p.b];
        curve.getPointAt(tt, tmp);
        curve.getTangentAt(tt, tan);
        tmp.x += -tan.z * p.lane; tmp.z += tan.x * p.lane; tmp.y = 0.28;
        const j = i * TRAIL + k;
        pPos[j * 3] = tmp.x; pPos[j * 3 + 1] = tmp.y; pPos[j * 3 + 2] = tmp.z;
        const s = SPLIT[p.b];
        const mix = THREE.MathUtils.smoothstep(tt, s - 0.01, s + 0.05);
        col.copy(white).lerp(branchCols[p.b], mix);
        const hi = hovered === p.b ? 1.35 : hovered >= 0 ? 0.45 : 1;
        pCol[j * 3] = col.r; pCol[j * 3 + 1] = col.g; pCol[j * 3 + 2] = col.b;
        pSize[j] = (k === 0 ? 1.55 : 0.95 - k * 0.16) * (hovered === p.b ? 1.25 : 1);
        // fade in at the start, out at the far end
        const edge = THREE.MathUtils.smoothstep(tt, 0, 0.05) * (1 - THREE.MathUtils.smoothstep(tt, 0.86, 1));
        pAlpha[j] = edge * (k === 0 ? 1 : 0.55 - k * 0.12) * hi;
      }
    }
    pGeo.attributes.position.needsUpdate = true;
    pGeo.attributes.color.needsUpdate = true;
    pGeo.attributes.size.needsUpdate = true;
    pGeo.attributes.alpha.needsUpdate = true;

    // dust drift
    if (!reduceMotion) {
      dust.rotation.y = Math.sin(time * 0.02) * 0.03;
      dust.position.y = Math.sin(time * 0.15) * 0.3;
    }
    // switch pulse
    switchGlow.material.opacity = (0.22 + (reduceMotion ? 0.1 : (Math.sin(time * 1.4) * 0.5 + 0.5) * 0.18)) * (switchGlow.userData.k ?? 1);

    // hover picking
    if (pointerInside) {
      ray.setFromCamera(mouse.clone().copy(target), camera);
      const hit = ray.intersectObjects(hitMeshes, false)[0];
      setHover(hit ? hit.object.userData.branch : -1);
      if (hovered >= 0) label.style.transform = `translate(${lastPointer.x + 18}px, ${lastPointer.y - 12}px)`;
    } else setHover(-1);
    railMats.forEach((mats, bi) => {
      const on = hovered === bi, dim = hovered >= 0 && !on;
      mats.forEach((m) => { m.opacity += ((on ? 1 : dim ? 0.28 : 0.72) - m.opacity) * Math.min(1, dt * 6); });
      (mats.lamps || []).forEach((l) => { l.material.opacity += ((on ? 1 : dim ? 0.2 : l.userData.base) - l.material.opacity) * Math.min(1, dt * 6); });
    });

    renderer.render(scene, camera);
  }

  function loop() {
    if (!visible || document.hidden) { running = false; return; }
    running = true;
    const dt = Math.min(clock.getDelta(), 0.05);
    renderFrame(dt, clock.elapsedTime);
    requestAnimationFrame(loop);
  }
  function kick() {
    if (reduceMotion) { renderFrame(0, 0); return; }
    if (!running && visible && !document.hidden) { clock.getDelta(); loop(); }
  }
  if (reduceMotion) {
    // spread requests along the tracks for a calm still frame
    parts.forEach((p, i) => (p.t = (i / N) * 0.95));
    renderFrame(0, 0);
    window.addEventListener("pointermove", () => renderFrame(0.05, 0), { passive: true });
  } else kick();
  hero.classList.add("hero--ready");
}

function makeGlowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.25, "rgba(255,255,255,.55)");
  grd.addColorStop(0.6, "rgba(255,255,255,.12)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
