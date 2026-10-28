# Switchyard

**Move every LLM call in your app to NVIDIA Nemotron on Nebius Token Factory, one call site at a time, with tests that prove each switch.**

Most production apps send every request to one frontier model. Many of those calls are routing, classification, extraction or short rewrites that a smaller open model handles just as well, at a fraction of the price. Nobody switches because nobody knows *which* calls are safe to move.

Switchyard answers that per call site. Point it at a repository and it:

1. **Scans** the code (static analysis, nothing is executed) for every OpenAI / Anthropic SDK call, including the prompt template, parameters and Pydantic output schema.
2. **Understands** each call: what task it does, what inputs look like, and how the app parses the answer.
3. **Writes tests**: typical and tricky cases with ideal reference answers. Real-world inputs come from **Tavily** web search where the task needs them.
4. **Evaluates** every Nemotron tier on every case: hard checks in code (valid JSON, schema match, correct labels and fields) plus a Nemotron grader.
5. **Repairs**: if a cheaper model *nearly* passes, Nemotron Ultra adapts the prompt and the cheaper model is re-tested.
6. **Patches** the code: each call moves to the cheapest model that clears the quality bar, calls that fail stay on the original model, every changed file is re-parsed and compiled, and you get a reviewable diff plus a cost report.

> Results from a recorded run are in [Results](#results).

## How Switchyard uses NVIDIA Nemotron and Nebius Token Factory

Switchyard runs entirely on Nemotron models served by Token Factory (OpenAI-compatible API). It also practises what it preaches: each internal job goes to the model that fits it.

| Job | Model | Why |
|---|---|---|
| Understand a call site | **Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`) | fast, strong at reading code, structured JSON out |
| Write tests and reference answers | **Nemotron 3 Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`), reasoning on | reference quality decides everything downstream |
| Grade candidate outputs | **Nemotron 3 Super** | many short calls, needs consistency more than depth |
| Adapt prompts for cheaper models | **Nemotron 3 Ultra**, reasoning on | rare, high-leverage |
| Candidates being tested | **Lightning** (`nvidia/Nemotron-3_5-Lightning`), **Super**, **Ultra** | the migration targets, cheapest first |

The "Run cost" tab shows exactly what each job spent.

The migrated app talks to Token Factory through its existing SDK: Switchyard only changes the client's `base_url` / `api_key` and the `model` of each call (behind an env var, so rollback is one line). If some calls stay on the original provider, their client is untouched and migrated calls get a dedicated Token Factory client.

## Other tools

- **Tavily**: finds real-world inputs (articles, tickets, product pages) so tests aren't only synthetic.
- **LangSmith** (optional): set `LANGSMITH_API_KEY` and every model call Switchyard makes is traced.

## Quick start

```bash
git clone https://github.com/ayushagarwalhere/switchyard
cd switchyard
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,tracing]"
cp .env.example .env                               # add NEBIUS_API_KEY (and TAVILY_API_KEY)

switchyard models                                  # check access and model ids
switchyard scan path/to/repo                       # list LLM call sites, no API calls
switchyard run https://github.com/owner/repo       # full migration run (default budget $2)
switchyard serve                                   # web app on http://127.0.0.1:8000
switchyard publish <run-id>                        # feature a finished run in the web app + fill the Results below
```

GitHub folder links work too, e.g. `https://github.com/daveebbelaar/ai-cookbook/tree/main/patterns/workflows/2-workflow-patterns`.

Each run is saved in `runs/<id>/`: `run.json` (every test, output, score and decision), `migration.patch`, `migration.env`, `PR.md` (the pull-request description) and the migrated copy of the code.

## Use it on a real codebase

```bash
# 1. Test every call site and write the patch (filters are optional)
switchyard run . --include "app/**" --exclude "tests/**" --budget 2

# 2a. Apply it on a new branch in your checkout (nothing is committed)
switchyard apply <run-id> .

# 2b. ...or let Switchyard commit, push and open the PR with its report (needs the gh CLI)
switchyard pr <run-id> .

# 3. Keep it honest: re-run the saved tests in CI and fail on drift
switchyard check .switchyard/run.json --repo .
```

`apply` also drops `.switchyard/run.json` (the test set) and `.env.switchyard.example` into the repo. [examples/switchyard-check.yml](examples/switchyard-check.yml) is a ready GitHub Actions workflow that runs the check nightly and on every PR that touches Python. `check --repo .` re-scans the code, so if someone edits a prompt the edited prompt is what gets tested.

### Use your real traffic as the reference

Synthetic tests are a good start; your original model's real answers are better. Give Switchyard recorded calls and it matches each one to the call site that produced it (by prompt template), recovers the variables, and uses the recorded answer as the reference:

```bash
switchyard traces --langsmith my-project --out traces.jsonl   # export from LangSmith
switchyard run . --traces traces.jsonl
```

Any JSONL works: `{"messages": [...], "output": "...", "model": "gpt-4o"}`, raw OpenAI request/response logs, or LangSmith LLM runs. Production cases fill the test set first; Nemotron Ultra writes the rest.

### Check the grader against people

```bash
switchyard review <run-id> --out review.tsv          # balanced sample of graded answers
# upload to Toloka (or any rating tool); raters answer OUTPUT:verdict yes/no
switchyard review <run-id> --import results.tsv      # -> "grader agrees with humans on 92% of 40 items"
```

The export uses the `INPUT:` / `GOLDEN:` column style that crowd platforms accept, and samples passes and fails equally, so agreement is measured on both.

## How decisions are made

- A model **passes a case** when the hard checks pass *and* the grader scores it 7/10 or more.
- A model **clears the bar** for a call site when its pass rate is at least the target (default 85%) *and* its mean score is within 0.05 of the best model tested, so a cheap model never wins by squeaking past a low bar.
- The **cheapest** model that clears the bar wins. If a cheaper model came close, Ultra adapts the prompt once and that variant is re-tested.
- If nothing clears the bar, the call **stays on its original model**. Switchyard says so; it doesn't force a migration.

## Results

<!-- results:start -->
_Filled in from a recorded run by `switchyard publish <run-id>`._
<!-- results:end -->

## Limits

- Python only. OpenAI SDK calls are rewritten automatically. Plain Anthropic `messages.create()` calls are rewritten too: they go through a small shim that returns Anthropic-shaped responses from Token Factory. Async, streaming and tool-use Anthropic calls are tested and get a target model in the report, but you port them by hand.
- Prompts built far from the call site are tested as a single user turn.
- Quality is measured against reference answers written by Nemotron Ultra with reasoning on, not against the original provider's outputs. If you have recorded production outputs, those make a stronger reference.

## Project layout

```
switchyard/
  scanner.py    find call sites, prompt templates, Pydantic schemas (AST only)
  agents.py     Nemotron steps: analyze, write tests, grade, repair prompts
  evaluate.py   run candidates, hard checks + grader, per-model summaries
  pipeline.py   orchestration, decisions, savings, Tavily, LangSmith
  rewrite.py    AST-positioned code edits, compile checks, diff
  server.py     FastAPI + server-sent events for the web app
web/            the web app (vanilla JS + Three.js hero)
tests/          offline tests (a scripted stand-in for Token Factory)
```

## License

MIT
