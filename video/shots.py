"""Screenshots of the running web app for the submission video (1920x1080).

    switchyard serve --port 8000          # in another terminal (no key needed: recorded runs)
    python video/shots.py                 # -> video/shots/*.png
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
MAIN, TAVILY = "20261029-142754-6b1f", "20261029-150732-a954"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        pg.add_init_script("try{localStorage.setItem('sy-guide-seen','1')}catch(e){}")
        hide = "document.head.insertAdjacentHTML('beforeend','<style>.guide-launch{display:none!important}</style>')"
        pg.on("load", lambda *_: pg.evaluate(hide))

        def snap(name, wait=900):
            pg.wait_for_timeout(wait)
            pg.screenshot(path=str(OUT / f"{name}.png"))
            print(name)

        pg.goto(BASE + "/")
        snap("hero", 2500)
        for sel, name in (("#steps", "pillars"), ("#buildCard", "build"), ("#auditForm", "audit")):
            el = pg.query_selector(sel)
            if el and el.is_visible():
                el.scroll_into_view_if_needed()
                pg.evaluate("window.scrollBy(0, -120)")
                snap(name)

        for tab in ("sites", "patch", "savings", "spend"):
            pg.goto(f"{BASE}/#{MAIN}:{tab}")
            pg.wait_for_selector("#runView .tabs", timeout=15000)
            pg.wait_for_timeout(600)
            pg.evaluate("document.querySelector('#runView').scrollIntoView({block:'start'})")
            snap(f"run-{tab}")

        pg.goto(f"{BASE}/#{TAVILY}:sites")
        pg.wait_for_selector("#runView .tabs", timeout=15000)
        pg.wait_for_timeout(800)
        card = pg.query_selector("#runView .site:has(.chip.tav)")
        if card:
            card.scroll_into_view_if_needed()
            pg.wait_for_timeout(500)
            card.screenshot(path=str(OUT / "tavily-card.png"))
            print("tavily-card")

        pg.goto(BASE + "/#tour")
        pg.wait_for_selector(".t-chap", timeout=15000)
        pg.wait_for_timeout(1500)
        snap("tour-intro")
        for el in pg.query_selector_all(".t-chap"):
            cid = el.get_attribute("data-chap")
            el.scroll_into_view_if_needed()
            pg.evaluate("(e) => e.scrollIntoView({block:'start'})", el)
            snap(f"tour-{cid}", 1300)
        b.close()


if __name__ == "__main__":
    main()
