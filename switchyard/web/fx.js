/* Small page effects, after Faultline's PointerFX and nav (Apache-2.0, see NOTICE):
   a light on cards that follows the pointer, and the header link for the section in view. */
(function () {
  "use strict";
  const fine = window.matchMedia("(pointer: fine)").matches;

  // card light: sets --mx/--my on the card under the pointer (fine pointers only)
  if (fine) {
    let card = null, x = 0, y = 0, queued = false;
    const paint = () => {
      queued = false;
      if (!card) return;
      const r = card.getBoundingClientRect();
      card.style.setProperty("--mx", `${x - r.left}px`);
      card.style.setProperty("--my", `${y - r.top}px`);
    };
    document.addEventListener("pointermove", (e) => {
      const el = e.target.closest?.(".card, .site, .t-card");
      if (card && card !== el) { card.style.removeProperty("--mx"); card.style.removeProperty("--my"); }
      card = el; x = e.clientX; y = e.clientY;
      if (card && !queued) { queued = true; requestAnimationFrame(paint); }
    }, { passive: true });
  }

  // header: mark the link whose section is in view (the judge tour while it is open)
  const links = [...document.querySelectorAll(".top-nav a[data-nav]")];
  const sections = links.map((a) => [a, document.querySelector(a.dataset.nav.startsWith("#") ? a.dataset.nav : null)]).filter(([, s]) => s);
  let ticking = false;
  function update() {
    ticking = false;
    const touring = document.body.classList.contains("touring");
    let on = null;
    if (touring) on = links.find((a) => a.dataset.nav === "tour");
    else {
      const line = window.innerHeight * 0.35;
      for (const [a, s] of sections) {
        const r = s.getBoundingClientRect();
        if (r.height && r.top <= line && r.bottom > line) on = a;
      }
      // #how sits inside #app: prefer the innermost match, which the loop order already gives
    }
    links.forEach((a) => a.classList.toggle("on", a === on));
  }
  const kick = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
  window.addEventListener("scroll", kick, { passive: true });
  window.addEventListener("hashchange", () => setTimeout(kick, 50));
  new MutationObserver(kick).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  kick();
})();
