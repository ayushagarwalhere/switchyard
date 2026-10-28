# Devpost submission draft

Fill the `[from recorded run]` numbers once the demo run is recorded. Every number must come from a real run: `switchyard publish <run-id>` prints them (calls switched, % cheaper, repairs, analysis cost) and writes the same numbers into the README.

**Track:** Coding and Agentic Engineering
**Tagline:** Move every LLM call in your app to NVIDIA Nemotron on Nebius Token Factory, one call site at a time, with tests that prove each switch.

## Inspiration
Most AI apps send every request to one frontier model. Teams know that many of those calls (routing, classification, extraction, short rewrites) would run fine on a smaller open model, but nobody switches, because nobody can say *which* calls are safe. Migrating is a week of hand-written evals per call. So the bill stays high and open models stay unused.

## What it does
Point Switchyard at a repository. It:
1. **Finds every LLM call** in the code (static analysis, nothing executed), including the prompt template, parameters and Pydantic output schema.
2. **Investigates each call** with a tool-using Nemotron 3 Super agent (search, read file): who calls it, with what real inputs, and which fields of the answer drive the app's logic. Then it writes down the call's contract.
3. **Writes a test set per call** with Nemotron 3 Ultra (reasoning on): typical and tricky cases with ideal answers. Tavily supplies real-world inputs, and recorded production traffic can be used as the reference.
4. **Runs a bake-off** of Nemotron Lightning, Super and Ultra on every case. Hard checks run in code (valid JSON, schema, labels, fields), plus a Nemotron grader. With an OpenAI key, the app's original model answers the same tests, so every switch comes with a head-to-head.
5. **Repairs**: when a cheaper model nearly passes, Ultra adapts the prompt and the cheaper model is re-tested.
6. **Writes the patch**: each call moves to the cheapest model that clears the bar, behind an env var. Calls that fail stay where they are. Every changed file is compile-checked, and Switchyard can open the PR with a full report.
7. **Runs the patched code for real**: each switched function is called once against Token Factory and every HTTP request is recorded. When a function needs structured arguments, Nemotron writes the call, runs it, and fixes it from the traceback.
8. **Keeps it honest**: `switchyard check` re-runs the saved tests in CI and fails the build if a switched call drifts.

On [daveebbelaar/ai-cookbook](https://github.com/daveebbelaar/ai-cookbook) workflow patterns (11 GPT-4o calls), Switchyard switched [from recorded run] calls, making requests [from recorded run]% cheaper. The whole analysis cost [from recorded run] in Token Factory credits.

## How we built it
- **Python + `ast`** for the scanner and the code rewriter, which edits exact AST positions and preserves line endings.
- **Nebius Token Factory** (OpenAI-compatible) for every model call, with a cost ledger and a hard budget cap per run.
- **Nemotron family by job**: Super for analysis and grading, Ultra with reasoning for tests and repairs, and Lightning/Super/Ultra as migration targets. Switchyard follows its own advice.
- **Structured outputs**: Pydantic models are turned into JSON schemas statically and sent the same way the OpenAI SDK's `.parse()` does.
- **Tavily** for realistic inputs. **LangSmith** for tracing and for importing production traffic.
- **FastAPI** with server-sent events for the live web app. **Three.js** for the rail-yard hero.

## Challenges we ran into
- Turning arbitrary application code into testable templates: f-strings, `.format()`, constants, prompts spread across variables.
- Rewriting code safely when only *some* calls migrate. The original client must stay for the calls that don't move, so migrated calls get a dedicated Token Factory client.
- Making "good enough" measurable. We combined deterministic checks with a grader, and set a bar requiring both a pass rate *and* a score close to the best model.

## Accomplishments we're proud of
- It refuses to migrate calls that fail, and says why.
- It doesn't stop at "the prompt works": it runs the patched application code and shows the request reaching Token Factory with the chosen model.
- The prompt-repair loop: Ultra adapting prompts so Lightning can take over calls it initially failed.
- A real developer workflow: `run → apply / pr → check` in CI.

## What we learned
[fill from experience: e.g. how Lightning vs Super behaved on structured outputs, reasoning on/off trade-offs]

## What's next
- TypeScript / JavaScript call sites.
- Fine-tuning a Nemotron model on Token Factory for call sites where no stock model passes, using the test set Switchyard already wrote.
- A GitHub App that runs `check` on every PR.

## Built with
python, fastapi, nvidia-nemotron, nebius-token-factory, tavily, langsmith, three.js, openai-sdk

## Feedback on Nebius Token Factory and NVIDIA tools (required section)
[fill from real experience during the build. Start from `switchyard doctor --out doctor.md`: it probes chat, json_object, json_schema, tool calling, the enable_thinking switch and the Responses API on each Nemotron model, so the table is real data. Add: rate limits hit, latency, docs gaps (e.g. no per-model capability list for tool calling; Nemotron isn't in the fine-tuning model list; custom-weight deployment is beta-on-request).]
