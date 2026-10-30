# Feedback on Nebius Token Factory and the Nebius CLI

From building [Switchyard](README.md), which moves an app's LLM calls to Nemotron on Token Factory after testing them. We ported three real projects (ai-cookbook, microsoft/generative-ai-for-beginners, WhyChain) and deployed with the Nebius CLI.

**The API numbers below were measured live on 2026-10-30 03:37 UTC** by `switchyard doctor --feedback` (54 requests, about $0.03). Rerun it with your own key to check them: results vary between runs, so each figure is over 5 or more samples. The CLI findings can't be measured with an API key; they are what we observed, with the date and version.

| # | Issue | Impact |
|---|---|---|
| 1 | json_schema field descriptions never reach the model | Wrong values in valid JSON after migrating from OpenAI `.parse()` |
| 2 | Model IDs use four different naming styles | Ports from NVIDIA NIM / build.nvidia.com and config typos fail with 404 |
| 3 | The smallest Nemotron is the slowest | Teams choose Lightning for speed and get the slowest replies |
| 4 | Structured answers sometimes burn the whole token budget unseen | Unfinished JSON, a crash in the caller, and paid tokens nobody sees |
| 5 | No Nemotron embedding model; the only one is slow | RAG apps can't run on Nemotron alone |
| 6 | Nebius CLI: confusing auth failures | Deploys blocked with errors that don't say what to do |

## 1. `json_schema` field descriptions never reach the model

**What happened.** Token Factory enforces the *shape* of a `json_schema` response, but the model doesn't see the `description` of each field. OpenAI's `client.beta.chat.completions.parse()` users put their instructions there ("use ISO 8601"), so after a port the JSON stays valid but the values are wrong. Copying the schema into the system prompt fixes it, which points at the descriptions as the missing piece. Super is hit hardest; in this measurement Lightning and Ultra guessed right without them, but a port can't rely on guessing.

**Repro** (ai-cookbook's `parse_event_details`, cut to two fields):
```python
import json, os; from openai import OpenAI
tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])
s = {"type": "object", "additionalProperties": False, "required": ["name", "date"], "properties": {
     "name": {"type": "string", "description": "Name of the event"},
     "date": {"type": "string", "description": "Date and time of the event. Use ISO 8601 to format this value."}}}
sys = "Today is Thursday, October 29, 2026. Extract detailed event information."
for extra in ("", "\nReply with JSON matching this schema:\n" + json.dumps(s)):
    r = tf.chat.completions.create(model="nvidia/nemotron-3-super-120b-a12b", temperature=0, messages=[{"role": "system", "content": sys + extra},
        {"role": "user", "content": "Schedule a 1h team meeting next Tuesday at 2pm with Alice and Bob."}],
        response_format={"type": "json_schema", "json_schema": {"name": "EventDetails", "strict": True, "schema": s}})
    print(bool(extra), r.choices[0].message.content)
```

**Measured.** A response counts as correct when the name is a readable event name (not a label like `create_calendar_event`) and the date carries a time (`T14:00`).

| Model | Schema only in `response_format` | Schema also in the prompt | Example of a miss |
|---|---|---|---|
| `nvidia/Nemotron-3_5-Lightning` | 5/5 correct | 5/5 correct | `{"name": "Team Meeting", "date": "2026-11-03T14:00:00-05:00 (Tuesday, November 3, 2026 at ` |
| `nvidia/nemotron-3-super-120b-a12b` | 0/5 correct | 5/5 correct | `{"name": "create_calendar_event", "date": "Tuesday, November 3, 2026 at 2:00 PM for 1 hour` |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | 5/5 correct | 5/5 correct | `{"name": "Team Meeting", "date": "2026-11-03T14:00:00-05:00"}` |

**Impact.** Every team porting `.parse()` code to Super hits this silently: no error, just wrong values that pass schema validation and break the code downstream (a calendar API rejects a date without a time).

**Suggested fix.** Pass field descriptions into the prompt the way OpenAI does (the chat template can render the schema), or document that they're ignored and recommend copying the schema into the system prompt. Switchyard does the copy in every migrated `.parse()` call as a workaround.

## 2. Model IDs use four different naming styles

**What happened.** The four Nemotron models in `/v1/models` are named four ways: lower-case, mixed case, an underscore for the version dot, and a repeated `NVIDIA-` vendor prefix. IDs are case-sensitive, so a guessed or ported ID fails with 404.

**Repro**
```bash
curl -s https://api.tokenfactory.us-central1.nebius.com/v1/models -H "Authorization: Bearer $NEBIUS_API_KEY" \
  | python -c "import json,sys; print('\n'.join(m['id'] for m in json.load(sys.stdin)['data'] if 'emotron' in m['id']))"
```

**Measured** (18 models listed):

| ID | Case | `NVIDIA-` prefix in the name | Version written as |
|---|---|---|---|
| `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | mixed | yes | digits only |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | mixed | no | digits only |
| `nvidia/Nemotron-3_5-Lightning` | mixed | no | `3_5` (underscore) |
| `nvidia/nemotron-3-super-120b-a12b` | lower | no | digits only |

**Impact.** Code ported from NVIDIA's own catalog (build.nvidia.com / NIM uses lower-case hyphenated IDs such as `nvidia/llama-3.3-nemotron-super-49b-v1`) or written from the model's marketing name ("Nemotron 3.5 Lightning") doesn't resolve. We had to copy each ID by hand from the listing.

**Suggested fix.** One scheme (lower-case, hyphens, `3.5` or `3-5`, no repeated vendor), with case-insensitive matching and the old IDs kept as aliases. A 404 that suggests the closest ID would also help.

## 3. The smallest Nemotron is the slowest

**What happened.** Lightning is the cheapest and smallest model, so it's the natural choice for latency-sensitive routes. It was the slowest of the three both in our migration runs and in a short repeated request.

**Recorded** in our 12 migration runs on 2026-10-29/30 (every test call, real app prompts):

| Model | Calls | Median | p90 |
|---|---|---|---|
| `nvidia/Nemotron-3_5-Lightning` | 650 | 5.7 s | 22.52 s |
| `nvidia/nemotron-3-super-120b-a12b` | 653 | 4.42 s | 19.79 s |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | 653 | 4.74 s | 20.62 s |

**Repro**
```python
import os, time; from openai import OpenAI
tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])
for mid in ("nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"):
    t = []
    for _ in range(5):
        t0 = time.perf_counter()
        tf.chat.completions.create(model=mid, max_tokens=200, temperature=0, extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                                   messages=[{"role": "user", "content": "In two sentences, explain what a database index is."}])
        t.append(time.perf_counter() - t0)
    print(mid, sorted(t)[2])
```

**Measured** (same short request, thinking off):

| Model | Median | p90 | Output tokens (median) | Tokens/s | Samples |
|---|---|---|---|---|---|
| `nvidia/Nemotron-3_5-Lightning` | 1.12 s | 2.24 s | 77 | 59.3 | 5 |
| `nvidia/nemotron-3-super-120b-a12b` | 0.62 s | 0.69 s | 54 | 86.2 | 5 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | 0.59 s | 0.61 s | 65 | 109.6 | 5 |

In this run Lightning's median was 1.12 s against Super's 0.62 s (Lightning slower).

**Impact.** Choosing a model by size gives the wrong answer for latency. Switchyard's router records p50/p95 latency per route so teams see it, but nothing tells them before they switch.

**Suggested fix.** Publish median latency and throughput per model (or show them on the model page), and check Lightning's serving capacity.

## 4. Structured answers sometimes burn the whole token budget on output nobody sees (intermittent)

**What happened.** In our recorded runs, 47 of 1,782 `json_schema` answers used the whole output budget (4,096 or 8,192 tokens, all billed) but contained little or no visible text: unfinished JSON, or nothing at all. 4,096 tokens of text is roughly 15,000 characters. So most of the budget went on output the caller never receives, most likely whitespace inside the JSON, which a schema grammar allows without limit. The caller's `json.loads` then fails.

| Model | Structured answers recorded | Used the whole budget, JSON unfinished | Visible characters in those | Runs |
|---|---|---|---|---|
| `nvidia/Nemotron-3_5-Lightning` | 594 | 20 | 0–3,308 | `20261029-1401`, `20261029-1507` |
| `nvidia/nemotron-3-super-120b-a12b` | 594 | 2 | 0–135 | `20261029-1401` |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | 594 | 25 | 115–15,345 | `20261029-1309`, `20261029-1352`, `20261029-1401`, `20261029-1414` |

Every cut-off case (run, function, case, tokens billed, visible characters, last characters) is in [docs/feedback.json](docs/feedback.json).

**Rechecked on 2026-10-30.** We replayed four of the recorded failing `get_plan` inputs on Ultra twice each, with and without the schema in the prompt: 0 of 16 were cut off (every answer finished in 537–1,520 tokens). The generic repro below also finished every time (lightning 0/3 cut off, super 0/3 cut off, ultra 0/3 cut off). So it was intermittent or has been fixed on the serving side, with no notice either way.

**Repro** (it no longer triggers for us; `finish_reason == "length"` with short content is the symptom):
```python
import os; from openai import OpenAI
tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])
schema = {"type": "object", "properties": {"title": {"type": "string"}, "sections": {"type": "array", "items": {"type": "object",
          "properties": {"heading": {"type": "string"}, "key_points": {"type": "array", "items": {"type": "string"}}}}}}}
r = tf.chat.completions.create(model="nvidia/Nemotron-3-Ultra-550b-a55b", max_tokens=4096, extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    messages=[{"role": "user", "content": "Plan a detailed long-form blog post about migrating a production app from GPT-4o to open models."}],
    response_format={"type": "json_schema", "json_schema": {"name": "Plan", "schema": schema}})
c = r.choices[0].message.content; print(r.choices[0].finish_reason, r.usage.completion_tokens, len(c), len(c.strip()))
```

**Impact.** A cut-off structured answer is an exception in the caller, not a worse answer, so one runaway response fails the request, and the caller still pays for the full budget. During a migration it made models look worse than they are, and a re-run can give a different verdict.

**Suggested fix.** Limit consecutive whitespace in `json_schema` mode (or end generation once the top-level object closes), and post serving changes in a changelog so users know when behaviour like this changes.

## 5. No Nemotron embedding model, and the only embedding model is slow

**What happened.** `/v1/models` lists 1 embedding model: `Qwen/Qwen3-Embedding-8B`. So an app that embeds (any RAG app) can't run fully on Nemotron, and porting WhyChain we found single-text embeddings taking 1–12 s.

**Repro**
```python
import os, time; from openai import OpenAI
tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])
for _ in range(10):
    t0 = time.perf_counter()
    tf.embeddings.create(model="Qwen/Qwen3-Embedding-8B", input="Switchyard moves an app's LLM calls to Nemotron.")
    print(round(time.perf_counter() - t0, 2), "s")
```

**Measured** (one short text per request):

| Model | Min | Median | Max | Dimensions | Samples |
|---|---|---|---|---|---|
| `Qwen/Qwen3-Embedding-8B` | 0.61 s | 1.13 s | 8.51 s | 4096 | 10 |

**Impact.** Retrieval latency adds to every RAG answer, and teams that want a single-vendor Nemotron stack need a second provider.

**Suggested fix.** Serve NVIDIA's own embedding models (the NV-Embed / Llama-Nemotron embedding family on NIM), and look at the tail latency of single-text requests.

## 6. Nebius CLI: auth failures that don't say what to do

**What happened** (observed 2026-10-29/30, Nebius CLI 0.12.298 in WSL Ubuntu, while setting up `switchyard deploy`):

- With a working profile (`nebius iam whoami` succeeds), `nebius registry list` fails with `rpc error: code = PermissionDenied desc = unknown subject`, and `nebius vpc subnet list` is also denied. Rechecked on 2026-10-30 with the same CLI: `registry list` now returns `{}`. So the error came from the account's state at the time, and nothing in the message said which state or how to fix it.
- `nebius ai endpoint list` opens a browser login, even with that profile configured and other commands working.
- A new tenant and its projects stay **SUSPENDED until billing is activated**. Reads succeed, so nothing looks wrong until the first create fails. The `unknown subject` error above is likely this, but it doesn't say so.
- `nebius config set region` fails with `no such config field "region"`.
- `nebius ai endpoint create` defaults to a GPU platform (`gpu-h100-sxm`); a CPU web app needs `--platform cpu-d3`.
- The installer puts the binary in `~/.nebius/bin`, which isn't on `PATH` until the shell reloads.
- `nebius profile delete` refuses to delete the active default profile; a second login silently reuses the browser's Google account.

**Repro**
```bash
curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash && exec -l $SHELL
nebius profile create            # log in to a new tenant, pick its project
nebius iam whoami                # works
nebius registry list             # PermissionDenied: unknown subject
nebius ai endpoint list          # opens a browser login despite the profile
```

**Impact.** We didn't reach a live Serverless endpoint: every error pointed at auth, while the cause was billing. Switchyard's demo runs on EC2 instead.

**Suggested fix.** Say "this tenant is suspended: activate billing at …" in place of `unknown subject`; make every command use the active profile's token; default `endpoint create` to CPU or ask; print the `PATH` step at the end of the installer.

## What worked well

- **One OpenAI-compatible endpoint for the whole Nemotron family.** A port is a `base_url` and `model` change, so patches stay small.
- **Every probed feature works on all three models**: chat, `json_object`, `json_schema`, tools, the thinking switch and the Responses API ([docs/doctor.md](docs/doctor.md); rerun with `switchyard doctor`).
- **Price makes testing before switching practical.** A full study of 11 call sites (8 tests each, 3 models, grading, prompt repair, security probes) cost about $0.55.

---
Generated by `switchyard doctor --feedback --out FEEDBACK.md`. Raw measurements: `docs/feedback.json`.
