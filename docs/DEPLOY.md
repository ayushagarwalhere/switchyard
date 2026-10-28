# Deploying the public demo

The public demo serves recorded runs to everyone and allows live runs only with an access code, so a public URL can't drain your Token Factory credits.

| Env var | Purpose |
|---|---|
| `NEBIUS_API_KEY` | needed for live runs |
| `SWITCHYARD_PUBLIC=1` | public mode: recorded runs open, live runs need the code, GitHub URLs only |
| `SWITCHYARD_RUN_CODE` | access code for live runs (share it with judges in the submission) |
| `SWITCHYARD_RUN_BUDGET` | max USD per live run in public mode (default 0.50) |
| `TAVILY_API_KEY`, `LANGSMITH_API_KEY` | optional |

Recorded runs live in `runs/demo/<id>/run.json` and ship with the image. To promote a run:

```bash
switchyard publish <run-id>     # copies it to runs/demo/<id>/ (local paths stripped) and fills the README Results section
git add runs/demo README.md && git commit -m "Feature recorded run"
```

Simulated (offline) runs and unfinished runs are refused, so the public demo only ever shows real Token Factory runs.

## Hugging Face Spaces (Docker)

1. Create a Space, choose the **Docker** SDK, and pick the blank template.
2. Push this repo to the Space, or link it. The Space's README needs this front matter at the top:
   ```yaml
   ---
   title: Switchyard
   emoji: 🚦
   colorFrom: green
   colorTo: purple
   sdk: docker
   app_port: 7860
   ---
   ```
3. In the Space settings, add the env vars above as **secrets**.
4. The provided `Dockerfile` listens on port 7860 with `SWITCHYARD_PUBLIC=1`.

## Render / Railway / Fly

Use the same `Dockerfile`. These platforms set `PORT` and the container respects it.

## Local

```bash
switchyard serve --host 0.0.0.0 --port 8000
```
