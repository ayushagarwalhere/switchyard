# Submission video

## The narrated submission video (2:44, 16:9, ready to upload)

[`video/submission.html`](../video/submission.html) is the Devpost video: one narrator, subtitles, real screenshots of the web app, and the guide bird from the web app small in the corner, its beak moving with the voice. It covers the problem, every step of a migration (scan, investigate, tests with Tavily Search and Extract, bake-off, repair, patch, attack), what Tavily changed in the recorded runs, the results, the Nebius ecosystem (`new`, `code`, `secure`, `pr-review`, `deploy` to Nebius Container Registry and Serverless, `oncall`, `router`), where it runs, and the feedback for NVIDIA and Nebius. Every number comes from a published run or FEEDBACK.md.

```bash
pip install edge-tts numpy playwright imageio-ffmpeg
switchyard serve --port 8000 && python video/shots.py   # fresh screenshots of the app -> video/shots/
python video/submission_voice.py                       # narration -> video/submission/timeline.js + voice.m4a
python video/render.py --page submission.html --audio submission/voice.m4a --out video/switchyard-submission.mp4
```

To change a line, edit `LINES` in [`video/submission_voice.py`](../video/submission_voice.py) and run the last two commands; only changed lines are voiced again, and scenes follow the new timing. Open `video/submission.html` in a browser and click to preview it with sound (`?t=60` starts at 1:00).

## The voiced 3D reel (1:58, vertical)

[`video/reel.html`](../video/reel.html) is a vertical (1080x1920) low-poly 3D short: a developer and Switchy, the lime robot, talk through the problem, the cause, and Switchyard with every feature. Cards in between show the real numbers from the published run, and the subtitles follow the voices.

```bash
pip install edge-tts numpy playwright imageio-ffmpeg
python video/reel_voice.py      # the dialogue -> voices (Microsoft neural TTS), mouth timing, video/reel/voice.m4a
python video/render.py --page reel.html --size 1080x1920 --audio reel/voice.m4a --out video/switchyard-reel.mp4
```

To change a line, edit `LINES` in [`video/reel_voice.py`](../video/reel_voice.py) and run both commands again; the timing, cuts and cards follow the new audio. Open `video/reel.html` in a browser and click to preview it with sound.

## The animated video (2:45, ready to upload)

[`video/switchyard.html`](../video/switchyard.html) is a motion-graphics video in the reel style: hook, problem, cause, solution, how it works, results, every feature, call to action. Every number on screen comes from the published run. Render it to MP4 (1920x1080, 30 fps):

```bash
pip install playwright imageio-ffmpeg
python video/render.py                       # -> video/switchyard.mp4 (uses Edge; --channel chrome or '' for Playwright's Chromium)
python video/render.py --music track.mp3     # optional music bed, faded out at the end
python video/render.py --stills 5,60,110     # check single frames
```

Open `video/switchyard.html` in a browser to preview it live (`?t=60` jumps to 1:00).

The text on screen carries the story, so it works without sound. To add a voice-over, read this over the matching scene:

| Time | Scene | Voice-over |
|---|---|---|
| 0:00–0:10 | The bill: $699.89 a month, crossed out, $16.80 | "This app spends seven hundred dollars a month on LLM calls. After Switchyard: seventeen. Same app." |
| 0:10–0:24 | The problem: 11 calls, all on GPT-4o | "Eleven calls, every one on a frontier model. Seven of them are routing, classification and extraction: jobs a small model does fine." |
| 0:24–0:40 | The cause: three cards | "Nobody switches, because prompts have no tests, they're tuned for one model, and checking by hand takes weeks." |
| 0:40–0:54 | The solution: tracks splitting to Lightning, Super, Ultra | "Switchyard finds every LLM call, tests it on NVIDIA Nemotron, and moves each one to the cheapest model that passes. One command." |
| 0:54–1:46 | How it works: 8 steps | "It scans the code. A Nemotron Super agent investigates each call. Nemotron Ultra writes the tests, with real inputs from Tavily. Every Nemotron tier runs a bake-off. Near-misses get their prompt adapted by Ultra. Then it patches the code, runs it for real against Token Factory, and attacks every switched prompt." |
| 1:46–2:02 | Results | "On a real repo: 7 of 11 calls switched, 97.6 percent cheaper, and the whole study cost fifty cents. Four writing calls stayed put: it says no when it should." |
| 2:02–2:14 | The app | "Every switch ships with the tests that prove it." |
| 2:14–2:33 | Everything in the box | "A CLI, coding-agent skills, an MCP server, a GitHub Action, regression checks, a security audit, Python and TypeScript." |
| 2:33–2:45 | Call to action | "Stop paying frontier prices for routing. Switchyard." |

## Alternative: a screen recording of the app


The rules require showing the project working and how it uses Token Factory and Nemotron.

| Time | Screen | Narration |
|---|---|---|
| 0:00–0:15 | Hero: requests flowing onto the three tracks | "Most AI apps send every request to one frontier model. Many of those calls don't need it, but nobody knows which ones are safe to move. Switchyard finds out, call by call." |
| 0:15–0:35 | Paste the ai-cookbook URL and start the run; scan results appear | "Point it at a repo. It reads the code without running it and finds every LLM call. Here, 11 GPT-4o calls: routing, extraction, a prompt-injection check, and writing." |
| 0:35–1:00 | One call site card: open "Super investigated the codebase" (tool steps), then the tests Ultra wrote | "For each call, a Nemotron Super agent searches and reads the codebase to find who calls it, with what real inputs, and which fields drive the app's logic. Nemotron Ultra, with reasoning on, writes typical and tricky tests with ideal answers. Tavily pulls real-world inputs." |
| 1:00–1:35 | Bake-off table filling in; click a red dot to show a failure | "Then a bake-off: Lightning, Super and Ultra on every test. Hard checks in code, like schema and labels, plus a grader. The cheapest model that clears the bar wins. If none does, the call stays on GPT-4o, and Switchyard says so." |
| 1:35–1:50 | The repair badge; before/after of the same case | "If a cheaper model nearly passes, Ultra adapts the prompt and re-tests. Here Lightning goes from 83% to 100% on validate_calendar_request." |
| 1:50–2:10 | Patch tab: diff, then the "Ran the patched code" panel (expand one row) | "The patch moves each call to Token Factory behind an env var. Then Switchyard runs the patched code for real and records the request: Token Factory, Lightning, parsed object back." |
| 2:10–2:25 | Security audit panel: a PR1 finding, "What the model answered", then the green "re-tested" tick | "Cheaper models can be easier to hijack, so Switchyard attacks every switched prompt on its new model, garak-style. When an injection gets through, it adds a guardrail rule and re-runs the attack to prove the fix." |
| 2:25–2:40 | Savings tab with the volume slider; Run cost tab | "Result: 7 of 11 calls switched, all to Lightning, 97.6% cheaper per request. The whole analysis cost 50 cents on Token Factory." |
| 2:40–2:55 | Terminal in an app folder: `switchyard run` → cost estimate → "Start? Y" → "Apply on a new branch? Y"; then a coding agent with the skill: "move my LLM calls to Nemotron" | "For developers it's three commands, or one sentence to your coding agent. `switchyard check` keeps every switch honest in CI." |

Recording notes:
- Use a recorded run with **Replay** at 4x for the pipeline parts, so the video doesn't wait on the API.
- Show the Token Factory dashboard usage page for about 2 seconds as proof of the Token Factory calls.
