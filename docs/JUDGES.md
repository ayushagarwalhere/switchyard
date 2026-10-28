# Judging Switchyard in 5 minutes

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
3. Open the **Patch** tab: this is the actual diff Switchyard produced, with compile checks and per-call env overrides.

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
| Token Factory client, cost ledger, budget cap | n/a | [`llm.py`](../switchyard/llm.py) |

The **Run cost** tab in the app shows what each of these jobs spent.

## What is deterministic (not left to a model)

- Finding call sites, prompt templates and Pydantic output schemas: Python `ast`, no code executed ([`scanner.py`](../switchyard/scanner.py)).
- Format checks: JSON validity, schema match, allowed labels, exact fields ([`evaluate.hard_checks`](../switchyard/evaluate.py)).
- The decision rule: cheapest model with pass rate ≥ the bar **and** mean score within 0.05 of the best ([`pipeline.decide`](../switchyard/pipeline.py)).
- The code edit: exact AST positions, then every touched file is re-parsed and byte-compiled ([`rewrite.py`](../switchyard/rewrite.py)).
- Runtime verification: the patched module is loaded (definitions only), the switched function is called, and every HTTP request is recorded. "Verified" means it returned normally **and** its request went to Token Factory with the chosen model ([`runcheck.py`](../switchyard/runcheck.py)).

## "Isn't Nemotron grading Nemotron?"

Partly, and Switchyard is built to limit how much that matters:
- Format checks (JSON, schema, labels, exact fields) run in code, not by a model.
- With an `OPENAI_API_KEY`, the app's **original model answers the same tests** and is graded the same way. It also raises the bar: a switch must score within 0.05 of the original.
- With recorded production traffic (`--traces`), the original model's real answers become the references.
- `switchyard review` exports a balanced sample for human raters (Toloka-ready) and reports how often the grader agrees with people.

## Run it yourself (about 3 minutes, under $1 of credits)

```bash
git clone https://github.com/ayushagarwalhere/switchyard && cd switchyard
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
echo "NEBIUS_API_KEY=..." > .env
switchyard run https://github.com/daveebbelaar/ai-cookbook/tree/main/patterns/workflows/2-workflow-patterns --budget 1
switchyard serve    # then open http://127.0.0.1:8000
```

The offline test suite (`pytest`) runs the full pipeline against a scripted stand-in for Token Factory, so it needs no key.
