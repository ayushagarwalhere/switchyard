# Judging Switchyard in 5 minutes

## What to look at, in order

| Time | What | Where |
|---|---|---|
| 3 min | The video: problem, cause, solution, results, every feature | Devpost page ([source](../video/switchyard.html), every number from the recorded run) |
| 3 min | The judge tour of a real migration | the web app, **Judge tour** |
| 1 min | The two recorded runs side by side: 7/11 both times; the second used Tavily inputs for every call | the web app, **Runs** |
| 2 min | Where Nemotron and Token Factory are used, and what is deterministic | below |

No live link at hand? Browse the same recorded runs locally, with no key and no spend:

```bash
pipx install git+https://github.com/ayushagarwalhere/switchyard
switchyard serve          # downloads the published runs once, then open http://127.0.0.1:8000/#tour
```

Direct links once it's running: `#20261029-142754-6b1f:patch` (the diff), `#20261029-142754-6b1f:savings` (the bill at your volume), `#20261029-150732-a954:sites` (the Tavily run: open any call's tests to see the web sources).

## Fastest path (no setup)

1. Open the live demo and press **Judge tour**. It walks through one complete, recorded migration using the run's real data:
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

## Run it yourself (about 3 minutes, under $1 of credits)

```bash
pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.3.1"
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
