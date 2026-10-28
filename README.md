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
```

GitHub folder links work too, e.g. `https://github.com/daveebbelaar/ai-cookbook/tree/main/patterns/workflows/2-workflow-patterns`.

Each run is saved in `runs/<id>/`: `run.json` (every test, output, score and decision), `migration.patch`, `migration.env` and the migrated copy of the code.

## How decisions are made

- A model **passes a case** when the hard checks pass *and* the grader scores it 7/10 or more.
- A model **clears the bar** for a call site when its pass rate is at least the target (default 85%) *and* its mean score is within 0.05 of the best model tested, so a cheap model never wins by squeaking past a low bar.
- The **cheapest** model that clears the bar wins. If a cheaper model came close, Ultra adapts the prompt once and that variant is re-tested.
- If nothing clears the bar, the call **stays on its original model**. Switchyard says so; it doesn't force a migration.

## Results

_Filled in from a recorded run._

## Limits

- Python codebases using the OpenAI SDK are rewritten automatically. Anthropic SDK calls are tested and get a target model in the report, but the rewrite is a manual step.
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
