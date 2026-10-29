"""Render video/switchyard.html to an MP4, frame by frame (deterministic, no screen recording).

    pip install playwright imageio-ffmpeg && playwright install chromium   # or use Edge: --channel msedge
    python video/render.py                      # -> video/switchyard.mp4 (1920x1080, 30 fps)
    python video/render.py --stills 3,20,60     # just PNG stills at those seconds, to check a scene
    python video/render.py --music track.mp3    # optional: lay a music track under it (faded out at the end)
    python video/render.py --page reel.html --size 1080x1920 --audio reel/voice.m4a --out video/switchyard-reel.mp4
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "switchyard.mp4"))
    ap.add_argument("--page", default="switchyard.html", help="switchyard.html (16:9) or reel.html (vertical, voiced)")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--audio", help="voice track to mux in (reel: reel/voice.m4a)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--stills", help="comma-separated seconds: write PNGs instead of a video")
    ap.add_argument("--channel", default="msedge", help="browser channel (msedge, chrome) or '' for Playwright's Chromium")
    ap.add_argument("--music", help="optional audio file to put under the video")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel or None)
        w, h = (int(x) for x in args.size.split("x"))
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
        page.goto((HERE / args.page).as_uri() + "?render=1")
        page.wait_for_function("window.__ready !== undefined")
        page.evaluate("window.__ready")
        page.wait_for_timeout(500)
        duration = page.evaluate("window.__duration")

        if args.stills:
            outdir = HERE / "stills"
            outdir.mkdir(exist_ok=True)
            for s in args.stills.split(","):
                t = float(s)
                page.evaluate(f"window.__seek({t})")
                page.screenshot(path=str(outdir / f"{Path(args.page).stem}-t{t:06.1f}.png"))
            print(f"stills in {outdir}")
            return

        end = min(args.end or duration, duration)
        frames = int((end - args.start) * args.fps)
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ff, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(args.fps), "-c:v", "mjpeg", "-i", "-"]
        audio = [str((HERE / a) if not Path(a).is_absolute() else a) for a in (args.audio, args.music) if a]
        for a in audio:
            cmd += ["-ss", str(args.start), "-i", a]
        if len(audio) == 2:      # voice on top, music bed underneath, faded out at the end
            fade = max(0, end - args.start - 3)
            cmd += ["-filter_complex", f"[2:a]volume=0.18,afade=t=out:st={fade}:d=3[m];[1:a][m]amix=inputs=2:normalize=0:duration=first[a]",
                    "-map", "0:v", "-map", "[a]"]
        elif audio:
            cmd += ["-map", "0:v", "-map", "1:a"]
        if audio:
            cmd += ["-shortest", "-c:a", "aac", "-ar", "48000", "-b:a", "192k"]
        cmd += ["-vf", "scale=in_range=pc:out_range=tv,format=yuv420p",   # standard range: plays everywhere
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", args.out]
        ff_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(frames):
            page.evaluate(f"window.__seek({args.start + i / args.fps})")
            ff_proc.stdin.write(page.screenshot(type="jpeg", quality=93))
            if i % (args.fps * 10) == 0:
                print(f"{i / args.fps:6.1f}s / {end - args.start:.1f}s", flush=True)
        ff_proc.stdin.close()
        ff_proc.wait()
        browser.close()
    print(f"wrote {args.out} ({frames / args.fps:.1f}s)")


if __name__ == "__main__":
    main()
