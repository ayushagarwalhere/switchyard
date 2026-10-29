"""Voice the reel: one TTS clip per line, a mouth-movement envelope per clip, one mixed voice track.

    pip install edge-tts numpy imageio-ffmpeg
    python video/reel_voice.py          # -> video/reel/timeline.js + video/reel/voice.m4a
    python video/render.py --page reel.html --size 1080x1920 --audio reel/voice.m4a --out video/switchyard-reel.mp4

Every number in the dialogue comes from the published run (runs/demo/20261029-142754-6b1f).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import edge_tts
import imageio_ffmpeg
import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "reel"
FPS = 30
VOICES = {"dev": ("en-US-BrianNeural", "+4%", "+0Hz"), "bot": ("en-US-AvaNeural", "+3%", "+0Hz")}

# (speaker, line, card shown while it plays, shot) - shot: dev / bot / two / card
LINES = [
    # 01 the problem
    ("dev", "Switchy, our LLM bill just hit seven hundred dollars a month. For one small app!", None, "dev"),
    ("bot", "That's the problem. Every request goes to GPT-4o, the most expensive model. Even the tiny ones.", "bill", "card"),
    ("dev", "Tiny ones?", None, "dev"),
    ("bot", "Seven of your eleven calls just route, classify, or pull out a few fields.", "calls", "card"),
    ("bot", "A small model does that fine, for a fraction of the price.", None, "bot"),
    # 02 the cause
    ("dev", "Then why haven't we switched to a cheaper model?", None, "dev"),
    ("bot", "Here's the cause. Nobody can prove a cheaper model is good enough, because your prompts have no tests.", "cause", "card"),
    ("bot", "Prompts are tuned for one model. Swap it, and the JSON breaks. And checking every call by hand takes weeks.", "cause", "card"),
    ("dev", "So we just keep paying.", None, "dev"),
    # 03 the solution
    ("bot", "Not anymore. Meet Switchyard. One command.", "run", "card"),
    ("dev", "What does it actually do?", None, "two"),
    ("bot", "It finds every LLM call in your code, investigates how it's used, "
            "and writes real tests with Nemotron Ultra and Tavily.", "tests", "card"),
    ("bot", "Then Lightning, Super and Ultra race on every test. The cheapest model that passes wins.", "bake", "card"),
    ("dev", "And if the cheap one almost passes?", None, "dev"),
    ("bot", "Ultra fixes the prompt. Lightning went from eighty-three to a hundred percent.", "repair", "card"),
    ("bot", "Then it patches your code, runs it for real on Token Factory, and attacks every new prompt.", "patch", "card"),
    ("dev", "Okay. Results?", None, "dev"),
    ("bot", "Seven of eleven calls moved to Lightning. Ninety-seven point six percent cheaper. "
            "The whole study cost fifty cents.", "results", "card"),
    ("dev", "Seven hundred a month, down to seventeen?", "bill2", "card"),
    ("bot", "And the four writing calls stayed on GPT-4o. It says no when it should.", None, "bot"),
    # 04 every feature
    ("dev", "How do I use it?", None, "dev"),
    ("bot", "From your terminal, your coding agent, an MCP server, or a GitHub Action that opens the pull request. "
            "Python and TypeScript.", "features", "card"),
    ("bot", "Plus security audits, nightly regression checks, and one-line rollback.", "features2", "card"),
    ("dev", "Run it.", None, "two"),
    ("bot", "Switchyard. Built on NVIDIA Nemotron and Nebius Token Factory.", "cta", "card"),
]
# chapter label shown from this line on (index into LINES)
SECTIONS = {0: "01 · The problem", 5: "02 · The cause", 9: "03 · The solution", 20: "04 · Everything in the box"}
SECTION_PAUSE = 0.6   # a breath before each new chapter
GAP = 0.32          # silence between lines
START = 0.5


async def tts(text: str, voice: str, rate: str, pitch: str, path: Path) -> None:
    await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(path))


def pcm(path: Path) -> np.ndarray:
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    raw = subprocess.run([ff, "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def envelope(x: np.ndarray, sr: int = 16000) -> list[float]:
    """Mouth openness per video frame, 0..1."""
    hop = sr // FPS
    frames = [float(np.sqrt(np.mean(x[i:i + hop] ** 2))) for i in range(0, len(x), hop)]
    peak = max(frames) or 1.0
    return [round(min(1.0, (f / peak) ** .7 * 1.15), 3) for f in frames]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    clips = OUT / "clips"
    clips.mkdir(exist_ok=True)
    timeline, t = [], START
    for i, (who, text, card, shot) in enumerate(LINES):
        voice, rate, pitch = VOICES[who]
        mp3 = clips / f"{i:02d}.mp3"
        asyncio.run(tts(text, voice, rate, pitch, mp3))
        x = pcm(mp3)
        nz = np.nonzero(np.abs(x) > 0.01)[0]            # trim TTS lead-in/out silence
        a, b = (nz[0], nz[-1]) if len(nz) else (0, len(x))
        trim_start, dur = a / 16000, (b - a) / 16000
        if i in SECTIONS and i:
            t += SECTION_PAUSE
        timeline.append({"i": i, "who": who, "text": text, "card": card, "shot": shot, "start": round(t, 3),
                         "section": SECTIONS.get(i),
                         "dur": round(dur, 3), "clip_offset": round(trim_start, 3), "env": envelope(x[a:b])})
        t += dur + GAP + (0.35 if card else 0)          # cards get a beat longer on screen
    total = round(t + 1.2, 2)
    (OUT / "timeline.js").write_text("window.TIMELINE = " + json.dumps({"total": total, "lines": timeline}) + ";\n",
                                     encoding="utf-8")
    # mix: each clip delayed to its start, trimmed of its lead-in
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd, parts = [ff, "-y", "-loglevel", "error"], []
    for ln in timeline:
        cmd += ["-ss", str(ln["clip_offset"]), "-t", str(ln["dur"] + 0.05), "-i", str(clips / f"{ln['i']:02d}.mp3")]
    for k, ln in enumerate(timeline):
        ms = int(ln["start"] * 1000)
        parts.append(f"[{k}:a]aresample=48000,adelay={ms}|{ms},apad[a{k}]")
    mix = "".join(f"[a{k}]" for k in range(len(timeline)))
    flt = ";".join(parts) + f";{mix}amix=inputs={len(timeline)}:normalize=0:duration=longest,atrim=0:{total},loudnorm=I=-16:TP=-1.5[out]"
    cmd += ["-filter_complex", flt, "-map", "[out]", "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(OUT / "voice.m4a")]
    subprocess.run(cmd, check=True)
    print(f"{len(timeline)} lines, {total:.1f}s -> {OUT / 'timeline.js'}, {OUT / 'voice.m4a'}")


if __name__ == "__main__":
    main()
