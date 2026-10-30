# Deploying the public demo

**The live instance:** <https://16-178-179-90.sslip.io/> (AWS EC2, Docker behind Caddy for HTTPS; judge tour at [#tour](https://16-178-179-90.sslip.io/#tour)). It serves the recorded runs, the judge tour and the free repo audit, none of which need a key.

This page is about hosting Switchyard's own web app. Deploying *your* app with `switchyard deploy` is covered in the [README](../README.md#also-in-the-box): the Nebius Serverless path is a preview (implemented and tested against the Nebius CLI), and `switchyard deploy --local` runs the same image in local Docker.

The public demo serves recorded runs to everyone and allows live runs only with an access code, so a public URL can't drain your Token Factory credits.

| Env var | Purpose |
|---|---|
| `NEBIUS_API_KEY` | needed for live runs |
| `SWITCHYARD_PUBLIC=1` | public mode: recorded runs open, live runs need the code, GitHub URLs only |
| `SWITCHYARD_RUN_CODE` | access code for live runs (share it with judges in the submission) |
| `SWITCHYARD_RUN_BUDGET` | max USD per live run in public mode (default 0.50) |
| `TAVILY_API_KEY`, `LANGSMITH_API_KEY`, `OPENAI_API_KEY` | optional (OpenAI = original-model baseline) |
| `SWITCHYARD_EXEC` | run patched code during live runs. Off by default in public mode, because live runs clone arbitrary GitHub repos; turn it on only if the container is disposable |

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

## AWS EC2 (Amazon Linux)

This is how the live instance runs. One command from your machine, once the instance's security group allows inbound TCP 22 (your IP), 80 and 443 (anywhere):

```bash
deploy/ec2.sh <public-ip-or-dns> ~/Downloads/switchyard.pem              # replays, judge tour, free audit
deploy/ec2.sh <public-ip-or-dns> ~/Downloads/switchyard.pem --with-key   # + live runs behind an access code
```

It installs Docker, clones the repo, copies any featured runs from `runs/demo`, builds the image and runs it with `--restart unless-stopped`. Caddy sits in front and gets a Let's Encrypt certificate automatically. With a bare IP the site name is `<ip-with-dashes>.sslip.io` (a free DNS name that resolves to that IP; the live instance is `16-178-179-90.sslip.io`), and plain HTTP or the bare IP redirects to it. Set `DOMAIN=your.domain` to use your own name (its DNS A record must point at the instance). With `--with-key` the key goes to a `chmod 600` env file on the server (never into the image), live runs need the printed access code, and each run is capped at $0.50.
