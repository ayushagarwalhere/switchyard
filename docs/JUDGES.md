# Judging Switchyard in 5 minutes

**Track:** Coding and Agentic Engineering. **What it is:** find out which LLM calls in an app can move to NVIDIA Nemotron on Nebius Token Factory, proven per call, and get the PR; plus a live router, and tools to build, review, secure, deploy and run the app ("Also in the box" in the [README](../README.md#also-in-the-box), each marked Proven, Working or Preview).

## 1. Open the live demo

**<https://16-178-179-90.sslip.io/>**. No key, no sign-up, no spend. It serves the recorded runs, the judge tour and the free repo audit.

| Time | What | Where |
|---|---|---|
| 3 min | The judge tour of a real migration | [Judge tour](https://16-178-179-90.sslip.io/#tour) (or the **Judge tour** button) |
| 1 min | The bill for the switched calls at your volume | [Savings](https://16-178-179-90.sslip.io/#20261029-142754-6b1f:savings) |
| 1 min | The patch, its runtime check and the security audit | [Patch](https://16-178-179-90.sslip.io/#20261029-142754-6b1f:patch) |
| 1 min | The run with Tavily inputs: open any call's tests to see the web sources they were written from | [Tavily run](https://16-178-179-90.sslip.io/#20261029-150732-a954:sites) |
| 1 min | A free audit of any public GitHub repo's LLM calls (static, no key) | the **Free security audit** box on the home page |
| 3 min | The video: problem, cause, solution, results, every feature | Devpost page ([source](../video/switchyard.html), every number from the recorded run) |
| 2 min | Where Nemotron and Token Factory are used, and what is deterministic | below |

<!-- TODO(access-code): if live runs are enabled on the demo (deploy/ec2.sh --with-key), put the access code in the Devpost testing instructions (not here, this file is public) and say so in one line here. -->
Live runs (a new migration from the browser) need an access code on the public demo, so a public URL can't drain our Token Factory credits. Everything above works without one.

**The two recorded runs.** Both are on the same repo (ai-cookbook, 11 calls) and both switched 7 of 11. `20261029-142754-6b1f` used generated tests only and moved all 7 to Lightning. `20261029-150732-a954` gave every call's test writer Tavily search results and moved 5 to Lightning, 1 to Super and 1 to Ultra. Decisions differ on 3 calls, which is within what we saw between two runs without Tavily (5 of 11 differed); see [How Tavily is used](../README.md#how-tavily-is-used).

**Where the Tavily call happens at runtime:** in a live `switchyard run`, once per call site, in `tavily_sources()` called from `Run.process_site()` in [`pipeline.py`](../switchyard/pipeline.py), cached on disk, at most 12 searches per run. Browsing recorded runs makes no Tavily calls.

## 2. If the demo is down: the same runs locally (no key)

```bash
pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.1"
switchyard serve          # downloads the published runs once, then open http://127.0.0.1:8000/#tour
```

The same direct links work on the local server: `#20261029-142754-6b1f:patch`, `#20261029-142754-6b1f:savings`, `#20261029-150732-a954:sites`.

## What the judge tour shows

1. **Judge tour** walks through one complete, recorded migration using the run's real data:
   - the scan,
   - one call site up close,
   - the tests Nemotron Ultra wrote,
   - the model bake-off,
   - the prompt repair,
   - the patch,
   - running the patched code for real,
   - the bill.
2. Press **Replay run** on the recorded run to watch the whole pipeline replay at speed.
3. Open the **Patch** tab: this is the actual diff Switchyard produced, with compile checks and per-call env overrides, the runtime verification, and the **Security audit**: static findings plus injection probes run against each switched call on its new model.

## Where NVIDIA Nemotron and Nebius Token Factory are used

Everything model-related runs on Nemotron through Token Factory's OpenAI-compatible API:

| Step | Model | Code |
|---|---|---|
| Investigate the codebase with tool calls (search, read file) | Nemotron 3 Super | [`investigate.investigate`](../switchyard/investigate.py) |
| Understand each call site | Nemotron 3 Super | [`agents.analyze`](../switchyard/agents.py) |
| Write tests + reference answers (reasoning on) | Nemotron 3 Ultra | [`agents.generate_tests`](../switchyard/agents.py) |
| Grade outputs | Nemotron 3 Super | [`agents.judge`](../switchyard/agents.py) |
| Adapt prompts for cheaper models (reasoning on) | Nemotron 3 Ultra | [`agents.repair_prompt`](../switchyard/agents.py) |
| Migration candidates | Nemotron 3.5 Lightning, 3 Super, 3 Ultra | [`evaluate.py`](../switchyard/evaluate.py) |
| Write (and fix from tracebacks) calls into the patched code | Nemotron 3 Super | [`runcheck.verify_site`](../switchyard/runcheck.py) |
| The patched app itself, run for real | the chosen Nemotron model per call | [`runcheck.py`](../switchyard/runcheck.py) |
| Attack each switched prompt (injection, leak, exfiltration) and re-test the fix | the chosen Nemotron model per call | [`audit.probes`](../switchyard/audit.py) |
| Token Factory client, cost ledger, budget cap | n/a | [`llm.py`](../switchyard/llm.py) |

The **Run cost** tab in the app shows what each of these jobs spent.

## What is deterministic (not left to a model)

- Finding call sites, prompt templates and Pydantic output schemas: Python `ast`, no code executed ([`scanner.py`](../switchyard/scanner.py)).
- Format checks: JSON validity, schema match, allowed labels, exact fields ([`evaluate.hard_checks`](../switchyard/evaluate.py)).
- The decision rule: cheapest model with pass rate ≥ the bar **and** mean score within 0.05 of the best ([`pipeline.decide`](../switchyard/pipeline.py)).
- The code edit: exact AST positions, then every touched file is re-parsed and byte-compiled ([`rewrite.py`](../switchyard/rewrite.py)).
- Audit rules and probe detectors: AST data flow from the model call to `eval`/shell/SQL sinks, key patterns, canary strings and system-prompt n-gram overlap. A model never decides whether an attack succeeded ([`audit.py`](../switchyard/audit.py)).
- Runtime verification: the patched module is loaded (definitions only), the switched function is called, and every HTTP request is recorded. "Verified" means it returned normally **and** its request went to Token Factory with the chosen model ([`runcheck.py`](../switchyard/runcheck.py)).

## "Isn't Nemotron grading Nemotron?"

Partly, and Switchyard is built to limit how much that matters:
- Format checks (JSON, schema, labels, exact fields) run in code, not by a model.
- With an `OPENAI_API_KEY`, the app's **original model answers the same tests** and is graded the same way. It also raises the bar: a switch must score within 0.05 of the original.
- With recorded production traffic (`--traces`), the original model's real answers become the references.
- `switchyard review` exports a balanced sample for human raters (Toloka-ready) and reports how often the grader agrees with people.

What it doesn't fix, said plainly: the two recorded runs had no OpenAI key, so their quality bar is Nemotron Ultra's reference answers, graded by Nemotron Super. A test is set aside as disputed when Lightning, Super and Ultra all fail it the same way; they're one model family and can share a mistake, so a correct reference can be set aside (set-aside tests are listed per call, never counted as passes). And when a test's input has no year, the date check accepts any valid date. The full list is in the [README's Limits](../README.md#limits).

## Run it yourself (about 3 minutes, under $1 of credits)

```bash
pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.1"
switchyard setup                     # paste a Token Factory key; it's checked and saved
git clone --depth 1 https://github.com/daveebbelaar/ai-cookbook && cd ai-cookbook/patterns/workflows/2-workflow-patterns
switchyard audit                     # free: security findings for every LLM call
switchyard run --budget 1            # shows calls + cost, asks, runs, offers to apply on a branch
switchyard serve                     # browse the run at http://127.0.0.1:8000
```

No app at hand? `switchyard demo` (Python) or `switchyard demo --js` (Next.js with the Vercel AI SDK) creates a sample app in its own git repo; then `cd switchyard-demo && switchyard run`.

Or from a coding agent: `npx skills add ayushagarwalhere/switchyard`, then ask it to "move my LLM calls to Nemotron".

Without installing anything: the web app's **Free security audit** box audits any public GitHub repo's LLM calls (no key).

The offline test suite (`pytest`) runs the full pipeline against a scripted stand-in for Token Factory, so it needs no key.
