# Demo video script (target 2:45, hard limit 3:00)

The rules require showing the project working and how it uses Token Factory and Nemotron.

| Time | Screen | Narration |
|---|---|---|
| 0:00–0:15 | Hero: requests flowing onto the three tracks | "Most AI apps send every request to one frontier model. Many of those calls don't need it, but nobody knows which ones are safe to move. Switchyard finds out, call by call." |
| 0:15–0:35 | Paste the ai-cookbook URL and start the run; scan results appear | "Point it at a repo. It reads the code without running it and finds every LLM call. Here, 11 GPT-4o calls: routing, extraction, a prompt-injection check, and writing." |
| 0:35–1:00 | One call site card: open "Super investigated the codebase" (tool steps), then the tests Ultra wrote | "For each call, a Nemotron Super agent searches and reads the codebase to find who calls it, with what real inputs, and which fields drive the app's logic. Nemotron Ultra, with reasoning on, writes typical and tricky tests with ideal answers. Tavily pulls real-world inputs." |
| 1:00–1:35 | Bake-off table filling in; click a red dot to show a failure | "Then a bake-off: Lightning, Super and Ultra on every test. Hard checks in code, like schema and labels, plus a grader. The cheapest model that clears the bar wins. If none does, the call stays on GPT-4o, and Switchyard says so." |
| 1:35–1:55 | The repair badge; before/after of the same case | "If a cheaper model nearly passes, Ultra adapts the prompt and re-tests. Here Lightning goes from [x]% to [y]%." |
| 1:55–2:20 | Patch tab: diff, then the "Ran the patched code" panel (expand one row: the call, the POST to Token Factory, the returned object) | "The patch moves each call to Token Factory behind an env var. Then Switchyard runs the patched code for real: it calls each switched function and records the request. Here it hit Token Factory with Lightning and returned the parsed object. Where the function needs a Pydantic object, Nemotron writes that call itself." |
| 2:20–2:40 | Savings tab with the volume slider; Run cost tab | "Result: [n] of 11 calls switched, [z]% cheaper per request. The whole analysis cost [$] on Token Factory, because Switchyard routes its own work the same way." |
| 2:40–2:50 | Terminal: `switchyard check` passing | "And `switchyard check` re-runs the tests in CI, so a switch that drifts fails the build, not your users." |

Recording notes:
- Use a recorded run with **Replay** at 4x for the pipeline parts, so the video doesn't wait on the API.
- Show the Token Factory dashboard usage page for about 2 seconds as proof of the Token Factory calls.
