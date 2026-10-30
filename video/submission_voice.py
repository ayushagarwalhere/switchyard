"""Voice the submission video: one narrator, one TTS clip per line, a beak envelope per clip, one voice track.

    pip install edge-tts numpy imageio-ffmpeg
    python video/submission_voice.py      # -> video/submission/timeline.js + video/submission/voice.m4a
    python video/render.py --page submission.html --audio submission/voice.m4a --out video/switchyard-submission.mp4

Numbers come from the published runs: 20261029-142754-6b1f (ai-cookbook), 20261029-150732-a954 (Tavily inputs),
20261030-070606-8ae0 (held-out repo), 20261030-095302-230b vs 20261030-095707-0376 (Tavily Extract),
and FEEDBACK.md section 7 (thinking tokens).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np

from reel_voice import envelope, pcm, tts

HERE = Path(__file__).resolve().parent
OUT = HERE / "submission"
VOICE = ("en-US-AndrewNeural", "+3%", "+0Hz")

# (scene, line). A scene lasts from its first line to the next scene's first line.
LINES = [
    ("hook", "Most AI apps send every request to one expensive frontier model. Even the tiny ones."),
    ("hook", "This app spent seven hundred dollars a month on LLM calls. After Switchyard: seventeen."),
    ("intro", "Meet Switchyard: the Nebius ecosystem for everyday developers, built on NVIDIA Nemotron and Nebius Token Factory."),
    ("scan", "Point it at a repo. It finds every LLM call, in Python and TypeScript. Here: eleven calls, all on GPT-4o."),
    ("investigate", "A Nemotron Super agent reads the codebase to learn how each call is really used."),
    ("tavily", "Then Nemotron Ultra writes the tests. With Tavily."),
    ("tavily", "Tavily search pulls real-world text for every call, so the tests look like production traffic. "
               "And Tavily Extract gives document calls whole web pages."),
    ("tavily2", "It changes decisions. On real inputs, a call that passed synthetic tests failed, so it stayed put. "
                "On full pages, a summarizer needed Super instead of Lightning."),
    ("bakeoff", "Then, a bake-off. Lightning, Super and Ultra answer every test. Hard checks run in code, plus a Nemotron grader. "
                "The cheapest model that clears the bar wins."),
    ("repair", "When a cheaper model falls short, Ultra reads its failures and adapts the prompt. Here, Lightning went from twenty-five to a hundred percent."),
    ("patch", "Then Switchyard writes the patch. Every switched call sits behind an env var, for one-line rollback, and every changed file is compile-checked."),
    ("audit", "Then it attacks every switched prompt with injection probes, garak-style, and re-tests the guardrail fix."),
    ("results", "On a real repo: seven of eleven calls moved to Lightning. Ninety-seven point six percent cheaper, for fifty cents of testing."),
    ("results", "On a held-out repo, three of five moved to Super. Long-form writing stayed put. It says no when it should."),
    ("eco", "Around that core is the Nebius ecosystem."),
    ("eco", "switchyard new writes a tested app from one sentence. Secure and PR review catch leaked keys and risky AI code."),
    ("eco", "switchyard deploy pushes the container to Nebius Container Registry, and starts a Nebius Serverless endpoint. "
            "On-call keeps it healthy."),
    ("eco", "And the router sends each feature to its tested Nemotron model, with fallback and metering."),
    ("everywhere", "Use it from your terminal, the web app, an MCP server, agent skills for Claude Code, Codex and Cursor, "
                   "or a GitHub Action that opens the pull request."),
    ("feedback", "We also wrote up what we measured, for NVIDIA and Nebius. Nemotron thinks by default, and bills for it: "
                 "three hundred forty-two tokens for a six-token answer. One flag turns it off."),
    ("cta", "Switchyard. Every AI call on the right Nemotron model, with tests that prove it. Take the judge tour on the live demo."),
]
CHAPTERS = {"hook": "The problem", "intro": "Switchyard", "scan": "01 · Scan", "investigate": "02 · Investigate",
            "tavily": "03 · Tests + Tavily", "tavily2": "03 · Tests + Tavily", "bakeoff": "04 · Bake-off",
            "repair": "05 · Repair", "patch": "06 · Patch", "audit": "07 · Attack", "results": "Results",
            "eco": "The Nebius ecosystem", "everywhere": "Everywhere", "feedback": "Feedback for NVIDIA + Nebius",
            "cta": "Switchyard"}
GAP, SCENE_PAUSE, START, TAIL = 0.3, 0.55, 0.6, 2.2


def main() -> None:
    clips = OUT / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    timeline, t, prev = [], START, None
    for i, (scene, text) in enumerate(LINES):
        mp3, said = clips / f"{i:02d}.mp3", clips / f"{i:02d}.txt"
        key = f"{VOICE}|{text}"
        # reuse a clip when its text is unchanged and it isn't cut short (~14 characters a second when spoken)
        fresh = mp3.exists() and said.exists() and said.read_text(encoding="utf-8") == key
        for attempt in range(4):
            if not fresh:
                asyncio.run(tts(text, *VOICE, mp3))
            x = pcm(mp3)
            if len(x) / 16000 > len(text) / 22:
                break
            fresh = False
            print(f"line {i}: clip too short ({len(x) / 16000:.1f}s), retrying")
        said.write_text(key, encoding="utf-8")
        nz = np.nonzero(np.abs(x) > 0.01)[0]
        a, b = (nz[0], nz[-1]) if len(nz) else (0, len(x))
        if prev is not None and scene != prev:
            t += SCENE_PAUSE
        timeline.append({"i": i, "scene": scene, "chapter": CHAPTERS[scene], "text": text, "start": round(t, 3),
                         "dur": round((b - a) / 16000, 3), "clip_offset": round(a / 16000, 3), "env": envelope(x[a:b])})
        t += (b - a) / 16000 + GAP
        prev = scene
    total = round(t + TAIL, 2)
    scenes = []
    for ln in timeline:
        if not scenes or scenes[-1]["id"] != ln["scene"]:
            scenes.append({"id": ln["scene"], "start": ln["start"]})
    for k, s in enumerate(scenes):
        s["end"] = scenes[k + 1]["start"] if k + 1 < len(scenes) else total
    (OUT / "timeline.js").write_text("window.TIMELINE = " + json.dumps({"total": total, "scenes": scenes, "lines": timeline}) + ";\n",
                                     encoding="utf-8")
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
    print(f"{len(timeline)} lines, {len(scenes)} scenes, {total:.1f}s -> {OUT}")
    for s in scenes:
        print(f"  {s['start']:6.1f}-{s['end']:6.1f}  {s['id']}")


if __name__ == "__main__":
    main()
