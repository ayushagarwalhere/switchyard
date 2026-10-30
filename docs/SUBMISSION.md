# Devpost submission draft

**Track:** Coding and Agentic Engineering
**Tagline:** Find out which LLM calls in your app can move to NVIDIA Nemotron, proven per call, and get the PR. A Nebius ecosystem for everyday developers, built around that migration.

**Live demo:** <https://16-178-179-90.sslip.io/> ([Judge tour](https://16-178-179-90.sslip.io/#tour))

## Testing instructions (for the Devpost "Testing instructions" field)

1. **Open the live demo: <https://16-178-179-90.sslip.io/>.** No key, no sign-up. Press **Judge tour** (or go to <https://16-178-179-90.sslip.io/#tour>): about 3 minutes through one recorded migration of a real repo (ai-cookbook, 11 LLM calls) with the run's real data: the scan, one call up close, the tests Nemotron Ultra wrote, the Lightning/Super/Ultra bake-off, the prompt repair, the patch, the patched code running against Token Factory, and the bill.
2. **The run with Tavily inputs:** <https://16-178-179-90.sslip.io/#20261029-150732-a954:sites>. Open any call's tests to see the web sources they were written from. Tavily is called at runtime during a live `switchyard run`: once per call site, in `tavily_sources()` called from `Run.process_site()` in `switchyard/pipeline.py`, cached on disk, at most 12 searches per run. Browsing recorded runs makes no Tavily calls.
3. **Free audit of a public repo:** paste any public GitHub URL into the **Free security audit** box on the home page. Static checks of every LLM call (leaked keys, model output reaching `eval`/shell/SQL, user input in system prompts, missing timeouts and token caps). No key, nothing is spent.
4. **Live runs** (a new migration from the browser) need an access code on the public demo so a public URL can't drain our credits. <!-- TODO(access-code): access code for live runs, if the EC2 demo was deployed with --with-key; otherwise delete this item. --> Access code: _to be added_.
5. **If the demo is down**, the same recorded runs work locally with no key and no spend:
   ```bash
   pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.1"
   switchyard serve        # then open http://127.0.0.1:8000/#tour
   ```
6. **To run it yourself** on a real repo (about 3 minutes, under $1 of Token Factory credits): `switchyard setup` (paste a Token Factory key), then `switchyard run` in the app's folder. Step-by-step in [docs/JUDGES.md](JUDGES.md#run-it-yourself-about-3-minutes-under-1-of-credits).

## Inspiration
Most AI apps send every request to one frontier model. Teams know that many of those calls (routing, classification, extraction, short rewrites) would run fine on a smaller open model, but nobody switches, because nobody can say *which* calls are safe. Migrating is a week of hand-written evals per call. So the bill stays high and open models stay unused.

## What it does
The core is the migration. Point Switchyard at a repository. It:
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

**Also in the box**, marked by how far each is proven. *Proven* = recorded real runs on Token Factory, viewable on the live demo; *Working* = covered by the offline test suite and used by us, no recorded public run; *Preview* = implemented and unit-tested, not exercised live.
- Proven: `switchyard run` (the migration above) and `switchyard audit` (also the free audit on the demo).
- Working: `switchyard router` (a live OpenAI-compatible gateway: each feature goes to its tested model, with fallback to the next tier and per-feature metering), `switchyard new` (Nemotron writes a tested FastAPI + HTML/JS app from one sentence), `switchyard code` (a Nemotron coding agent that edits, tests and fixes), `switchyard secure` (secrets, OSV dependencies, config, Dockerfile, LLM calls), `switchyard pr-review` (cost, Nemotron fit and security on a PR's AI code), `switchyard deploy --local`, `switchyard oncall` (vendored WispOps: finds the breaking change, proposes one fix, applies it on approval, proves recovery), the MCP server, agent skills and the GitHub Action.
- Preview: `switchyard deploy` to Nebius Serverless (Container Registry + endpoint). Our Nebius tenant is suspended until billing is activated, so this path hasn't run against the live service.

**Results.** On [daveebbelaar/ai-cookbook](https://github.com/daveebbelaar/ai-cookbook) workflow patterns (11 calls: 8 on GPT-4o, 3 on GPT-4o-mini), run `20261029-142754-6b1f` switched **7 of 11 calls to Nemotron 3.5 Lightning**. The chosen model passed 8/8, 7/8, 7/8, 7/7, 7/7 (1 test set aside), 6/6 (2 set aside) and 6/6 (2 set aside) of its tests; 2 calls passed only after Ultra adapted the prompt. Those requests are **97.6% cheaper than on GPT-4o** at list prices (about $699.89 → $16.80 a month at 100k requests per call), and would be 60% cheaper than on GPT-4o-mini. The 4 long-form writing calls stayed on their original model: for 3, no Nemotron model reached the bar; for 1, only Ultra did, and it costs more than GPT-4o-mini. The whole study cost **$0.50** in Token Factory credits (791 calls, 8 minutes). A second run with Tavily inputs (`20261029-150732-a954`) also switched 7 of 11 (5 Lightning, 1 Super, 1 Ultra; 93.4% cheaper than GPT-4o, $0.76). On a Next.js app (OpenAI SDK + Vercel AI SDK), 4 of 4 calls switched, 94.4% cheaper, for $0.10.

<!-- TODO(run): GPT-4o head-to-head on the same tests (needs an OpenAI key): "the original model passed X/Y on the switched calls". -->
<!-- TODO(run): Held-out repo: X of Y switched (repo, run id, cost). -->

**Known limits.** One run per repo is a sample: tests are generated fresh, and two runs on the same repo without Tavily disagreed on 5 of 11 calls. When a test's input has no year, the date check accepts any valid date. Lightning, Super and Ultra are one model family, so the "disputed" tests they all fail the same way (set aside, never counted as passes) are judged within that family; the recorded runs had no OpenAI baseline. Nebius deploy is a preview (see above).

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
  Minimal repro (Python, `openai` SDK; the schema and prompt are ai-cookbook's `parse_event_details`, cut down to two fields):
  ```python
  import json, os
  from openai import OpenAI
  tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])
  schema = {"type": "object", "additionalProperties": False, "required": ["name", "date"], "properties": {
      "name": {"type": "string", "description": "Name of the event"},
      "date": {"type": "string", "description": "Date and time of the event. Use ISO 8601 to format this value."}}}
  system = "Today is Thursday, October 29, 2026. Extract detailed event information."
  user = "Let's schedule a 1h team meeting next Tuesday at 2pm with Alice and Bob."
  for extra in ("", "\nReply with JSON matching this schema (follow each field's description):\n" + json.dumps(schema)):
      r = tf.chat.completions.create(model="nvidia/nemotron-3-super-120b-a12b", temperature=0,
          messages=[{"role": "system", "content": system + extra}, {"role": "user", "content": user}],
          response_format={"type": "json_schema", "json_schema": {"name": "EventDetails", "strict": True, "schema": schema}})
      print(repr(extra[:20]), r.choices[0].message.content)
  ```
  The first request sends the descriptions only inside `response_format`; the second also puts the schema in the system prompt. What we saw on 2026-10-29 with the full schema: without the prompt copy, Super named the event `"create_calendar_event"` and Lightning (`nvidia/Nemotron-3_5-Lightning`) returned a date without a time; with it, both returned `"Team Meeting"` and a full ISO 8601 timestamp. We haven't re-run this cut-down script, and single samples vary, so run it a few times and on both models.
- **Latency doesn't follow size.** Median per-call latency on our test sets was Lightning 6.8–9.3s, Super 4.6–5.2s, Ultra 5.7–6.8s. The smallest model was the slowest, which surprised us and matters for latency-sensitive routes. A published latency/throughput figure per model would help people choose.
- **Long structured outputs can run away.** With thinking off, Ultra hit our 4,096-token cap on 2 of 8 plan-generation cases, producing unfinished JSON. Guidance on recommended `max_tokens`, or stop behaviour for `json_schema` mode, would help.
- **A per-model capability table in the docs** (tools, `json_schema`, reasoning switch, Responses API) would save everyone a probe script like ours.
- **Fine-tuning:** Nemotron models aren't in the fine-tuning list. For call sites where no stock model passes, fine-tuning a small Nemotron on the test set Switchyard already writes would be the natural next step.
- **Smaller models need format hints.** Lightning often added chatter around JSON. Ultra's one-line prompt hint fixed many cases, which is exactly what Switchyard's repair step automates.

**Nebius AI Cloud CLI and deploy experience** (setting up `switchyard deploy`, 2026-10-29/30). We didn't get to a live Serverless endpoint, for the first reason below.
- **A new tenant and all its projects stay SUSPENDED until billing is activated.** CLI reads (`profile`, `iam`, listing) succeed, so nothing looks wrong until the first create call fails. A clear "activate billing" message on login, or on the first failing create, would make the cause obvious.
- **Creating an extra project prompts for a $25 top-up**, which isn't mentioned before you try.
- **`nebius ai endpoint create` defaults to a GPU platform (`gpu-h100-sxm`).** A CPU web app has to pass `--platform cpu-d3` explicitly; a CPU default, or a prompt, would avoid an accidental GPU bill. Switchyard passes `--platform cpu-d3` by default.
- **The installer puts the binary in `~/.nebius/bin`**, which isn't on `PATH` until the shell is reloaded (`exec -l $SHELL`); the installer's last line could say so.
- **`nebius profile delete` refuses to delete the active default profile.** Our workaround was to create a new profile under another name.
- **`nebius iam tenant …` and `nebius iam project …` print deprecation warnings** pointing to `iam v2`.
- **Logging in a second account** opens a browser login that silently reuses whatever Google account the default browser is signed into. A private window works; a `--no-browser`/device-code flow or an account picker would be clearer.

**Token Factory model catalog gaps.** There's no Nemotron embedding model (the only embedding model listed was Qwen3-Embedding-8B), so an app that also embeds can't run fully on Nemotron. Nemotron 3.5 Super VL isn't on Token Factory either, so image-input calls have no Nemotron target.

**NVIDIA open source we built on:** garak's probe/detector design shaped our injection audit. NeMo Guardrails-style input rules are the fixes we re-test. SkillSpector scans our agent skills in CI and passes.
