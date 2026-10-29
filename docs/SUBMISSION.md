# Devpost submission draft

**Track:** Coding and Agentic Engineering
**Tagline:** Move every LLM call in your app to NVIDIA Nemotron on Nebius Token Factory, one call site at a time, with tests that prove each switch and an audit that checks it can't be hijacked.

## Inspiration
Most AI apps send every request to one frontier model. Teams know that many of those calls (routing, classification, extraction, short rewrites) would run fine on a smaller open model, but nobody switches, because nobody can say *which* calls are safe. Migrating is a week of hand-written evals per call. So the bill stays high and open models stay unused.

## What it does
Point Switchyard at a repository. It:
1. **Finds every LLM call** in Python *and* JavaScript/TypeScript code (static analysis, nothing executed): OpenAI, Anthropic, LiteLLM, the Vercel AI SDK that most Next.js apps use, and LangChain (`ChatOpenAI` traced through `prompt | llm` chains to its `ChatPromptTemplate`), including the prompt template, parameters and Pydantic output schema. Models handed to agent frameworks are listed and skipped with the reason, never tested on made-up prompts.
2. **Investigates each call** with a tool-using Nemotron 3 Super agent (search, read file): who calls it, with what real inputs, and which fields of the answer drive the app's logic. Then it writes down the call's contract.
3. **Writes a test set per call** with Nemotron 3 Ultra (reasoning on): typical and tricky cases with ideal answers. Tavily supplies real-world inputs, and recorded production traffic can be used as the reference.
4. **Runs a bake-off** of Nemotron Lightning, Super and Ultra on every case. Hard checks run in code (valid JSON, schema, labels, fields), plus a Nemotron grader. With an OpenAI key, the app's original model answers the same tests, so every switch comes with a head-to-head.
5. **Repairs**: when a cheaper model nearly passes, Ultra adapts the prompt and the cheaper model is re-tested.
6. **Writes the patch**: each call moves to the cheapest model that clears the bar, behind an env var. Calls that fail stay where they are. Every changed file is compile-checked, and Switchyard can open the PR with a full report.
7. **Runs the patched code for real**: each switched function is called once against Token Factory and every HTTP request is recorded. When a function needs structured arguments, Nemotron writes the call, runs it, and fixes it from the traceback.
8. **Audits it**: static checks for leaked keys, model output reaching `eval`/shell/SQL, user input in system prompts and missing timeouts or token caps. Then, following NVIDIA garak's probe-and-detector design, it attacks each switched prompt on its new Nemotron model with injection, system-prompt-leak and exfiltration probes. When one gets through, it adds a NeMo Guardrails-style input rule and re-runs the attack to show whether the fix holds.
9. **Keeps it honest**: `switchyard check` re-runs the saved tests in CI and fails the build if a switched call drifts.

**Built for developers, not demos.** `pipx install`, `switchyard setup` (key checked and saved once), then `switchyard run` in the app's folder: it shows the calls and the cost estimate, asks before spending, and ends with "apply on a new branch?". The same flows ship as **agent skills** for Claude Code, Codex and Cursor (`npx skills add ayushagarwalhere/switchyard`; scanned by NVIDIA SkillSpector in CI), as a **GitHub Action** (migrate-and-open-a-PR, nightly check, audit to the Security tab via SARIF), and as a **Python API**.

On [daveebbelaar/ai-cookbook](https://github.com/daveebbelaar/ai-cookbook) workflow patterns (11 calls: 8 on GPT-4o, 3 on GPT-4o-mini), Switchyard switched **7 of 11 calls to Nemotron 3.5 Lightning**, making those requests **97.6% cheaper** at list prices. 2 of them passed only after Ultra adapted the prompt. The 4 long-form writing calls stayed on their original model because no Nemotron model reached the bar, or the one that did cost more. The whole study cost **$0.50** in Token Factory credits (791 calls, 8 minutes). On a Next.js app (OpenAI SDK + Vercel AI SDK), 4 of 4 calls switched, 94.4% cheaper, for $0.10.

## How we built it
- **Python + `ast`** for the scanner and the code rewriter, which edits exact AST positions and preserves line endings.
- **Nebius Token Factory** (OpenAI-compatible) for every model call, with a cost ledger and a hard budget cap per run.
- **Nemotron family by job**: Super for analysis and grading, Ultra with reasoning for tests and repairs, and Lightning/Super/Ultra as migration targets. Switchyard follows its own advice.
- **Structured outputs**: Pydantic models are turned into JSON schemas statically and sent the same way the OpenAI SDK's `.parse()` does.
- **Tavily** for realistic inputs. **LangSmith** for tracing and for importing production traffic.
- **FastAPI** with server-sent events for the live web app. **Three.js** for the rail-yard hero.
- **NVIDIA open source as design input**: garak's probe/detector split for the audit probes, NeMo Guardrails' input rails for the suggested fixes, SkillSpector for scanning our own agent skills, and SARIF output like SkillSpector's so findings land in GitHub code scanning.

## Challenges we ran into
- Turning arbitrary application code into testable templates: f-strings, `.format()`, constants, prompts spread across variables.
- Rewriting code safely when only *some* calls migrate. The original client must stay for the calls that don't move, so migrated calls get a dedicated Token Factory client.
- Making "good enough" measurable. We combined deterministic checks with a grader, and set a bar requiring both a pass rate *and* a score close to the best model.

## Accomplishments we're proud of
- It refuses to migrate calls that fail, and says why.
- It doesn't stop at "the prompt works": it runs the patched application code and shows the request reaching Token Factory with the chosen model.
- The prompt-repair loop: Ultra adapting prompts so Lightning can take over calls it initially failed.
- A real developer workflow: `setup → run → apply / pr → check`, the same steps from a coding agent, a GitHub Action or Python.
- Security findings come with fixes that were re-tested, not just advice.

## What we learned
- **Our grader was the bug, not the models.** The first real run switched 1 call of 11. Reading the failures showed exact-match checks on confidence scores, a grader that saw only the first 3,000 characters, tool calls graded on the function name instead of the arguments, and a demo `user_input = "..."` at the bottom of a file that the scanner substituted for the real input. Fixing those took the same code to 7 of 11, with every remaining failure one we'd defend.
- **Schemas need to be in the prompt.** Guided decoding enforces structure, not meaning: without the field descriptions in the prompt, Nemotron filled the right shape with the wrong content.
- **The cheapest model is usually enough for structured calls.** Every classification, routing and extraction call landed on Lightning. Open-ended writing was where the bigger models, or the original, stayed necessary.
- **Tests can be wrong too.** When Lightning, Super and Ultra all gave the same answer against a reference, the reference was the error. Switchyard now sets those tests aside and says so.

## What's next
- Following prompts imported from other JavaScript modules when building test templates.
- Fine-tuning a Nemotron model on Token Factory for call sites where no stock model passes, using the test set Switchyard already wrote.
- A Nemotron safety-guard model as an extra detector for the audit probes.

## Built with
python, fastapi, nvidia-nemotron, nebius-token-factory, tavily, langsmith, three.js, openai-sdk, github-actions, sarif, agent-skills

## Feedback on Nebius Token Factory and NVIDIA tools (required section)

Everything below comes from our own runs on 2026-10-29: `switchyard doctor` probes plus two full migration runs on ai-cookbook (760 and 827 Token Factory calls, $0.61 and $0.52).

**What worked well**
- **One OpenAI-compatible endpoint for the whole Nemotron family.** Moving an app was a `base_url` and `model` change, so our patches stay small and easy to review. The `/v1/models` listing showed 18 models, including the three we use.
- **Feature coverage was complete.** All three Nemotron models (3.5 Lightning, 3 Super, 3 Ultra) passed every probe in [docs/doctor.md](doctor.md): plain chat, `json_object`, `json_schema` structured output, tool calling, the `enable_thinking` switch, and the Responses API. Neither run needed a single fallback path.
- **Price makes "test before you switch" practical.** A full migration study of 11 call sites cost about $0.55: 8 tests each, 3 models, a grader, prompt repair and security probes. Ultra writing the tests was the largest line item. Running Super as the grader for ~300 judgements cost about $0.10.
- **Tool calling on Super was good enough for an investigation agent.** It read and searched real repos over ~70 tool calls per run for about $0.05.

**What we'd improve**
- **`json_schema` field descriptions don't reach the model.** Token Factory enforces the schema's *shape* but doesn't show the model the field descriptions. OpenAI's `.parse()` users rely on those descriptions. Measured on the same request: with the schema only, Super named an event `"create_calendar_event"` and Lightning returned a date without a time. With the same schema also in the system prompt, both returned `"Team Meeting"` and a full ISO 8601 timestamp. Switchyard now adds the schema to the prompt in every migrated `.parse()` call. Passing descriptions through (or documenting this) would save every migrating team the same surprise.
- **Latency doesn't follow size.** Median per-call latency on our test sets was Lightning 6.8–9.3s, Super 4.6–5.2s, Ultra 5.7–6.8s. The smallest model was the slowest, which surprised us and matters for latency-sensitive routes. A published latency/throughput figure per model would help people choose.
- **Long structured outputs can run away.** With thinking off, Ultra hit our 4,096-token cap on 2 of 8 plan-generation cases, producing unfinished JSON. Guidance on recommended `max_tokens`, or stop behaviour for `json_schema` mode, would help.
- **A per-model capability table in the docs** (tools, `json_schema`, reasoning switch, Responses API) would save everyone a probe script like ours.
- **Fine-tuning:** Nemotron models aren't in the fine-tuning list. For call sites where no stock model passes, fine-tuning a small Nemotron on the test set Switchyard already writes would be the natural next step.
- **Smaller models need format hints.** Lightning often added chatter around JSON. Ultra's one-line prompt hint fixed many cases, which is exactly what Switchyard's repair step automates.

**NVIDIA open source we built on:** garak's probe/detector design shaped our injection audit. NeMo Guardrails-style input rules are the fixes we re-test. SkillSpector scans our agent skills in CI and passes.
