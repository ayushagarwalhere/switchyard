---
name: switchyard-ship
description: Check an app for security problems and deploy it to Nebius AI Cloud (container, Nebius Container Registry, Serverless endpoint) with a live URL, then check status, read logs or delete the deployment. Use when the user wants to put an app online, deploy to Nebius, check an app is safe to ship, or take a deployment down.
---

# Switchyard ship: secure it, then put it on Nebius

`switchyard secure` checks the whole app before it ships: secrets anywhere in the repo, `.env` files committed to
git, dependencies with known vulnerabilities (OSV), debug mode, CORS open to every site, containers running as
root, and every LLM call. `switchyard deploy` runs that check, writes a Dockerfile if the app has none, builds the
image, pushes it to Nebius Container Registry and creates a Nebius Serverless endpoint, then prints the live URL.

## Rules

- **A deployed endpoint bills until it is deleted.** Say so before deploying, and only deploy when the user agrees.
- Never print, copy or move a key you find. Refer to it by file and line and tell the user to rotate it.
- Don't pass `--force` to get past blocking security findings unless the user explicitly accepts the risk.

## 1. Requirements

```bash
switchyard --version          # if missing: pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.1"
docker --version              # Docker Desktop or Docker Engine
nebius profile list           # the Nebius CLI, logged in (on Windows: inside WSL Ubuntu)
```

If the Nebius CLI is missing, the user installs it and logs in once (it opens a browser):

```bash
curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
exec -l $SHELL && nebius profile create
```

The installer puts the binary in `~/.nebius/bin`; it's on `PATH` only after the shell is reloaded (`exec -l $SHELL`).
The Nebius tenant needs billing activated: a new tenant and its projects stay suspended until then, and while
`nebius` reads still work, creating the endpoint fails. To try the whole flow for free first, use
`switchyard deploy --local` (same image, env and health check, in local Docker).

## 2. Secure (free)

```bash
switchyard secure
```

Explain CRITICAL and HIGH findings first; they block the deploy. Fix the ones the user asks you to fix and run it again.

## 3. Deploy

```bash
switchyard deploy
```

It prints each step and ends with `Live: https://...`. Apps run on a CPU platform (`cpu-d3`; set
`SWITCHYARD_NEBIUS_PLATFORM` to change it), because `nebius ai endpoint create` otherwise defaults to a GPU platform. The app gets the user's Token Factory key as an environment
variable for its AI features, plus any `LLM_MODEL_*` overrides from `.env`. Use `--private` for an endpoint that
needs a bearer token.

## 4. Manage

```bash
switchyard deploy status        # state and URL
switchyard deploy logs          # container logs
switchyard deploy delete        # stop billing
```

Redeploying the same app replaces its old endpoint once the new one is healthy.

## 5. Keep it running

```bash
switchyard oncall                    # WispOps watches every deployed app (needs Node 18.17+)
switchyard deploy logs --explain     # Nemotron reads the logs: what's failing, why, and the one command that fixes it
```

When an app breaks, the on-call agent finds the change that caused it, proposes one fix (restart, or roll back to the
last healthy image), applies it only when the user approves (typed, or said back with Wispr Flow with `--voice`),
and proves the app recovered. Never approve a fix on the user's behalf.

## 6. Review AI code before it merges

```bash
switchyard pr-review --base origin/main
```

Shows each AI call the branch adds or changes, its cost at volume vs Nemotron, which Nemotron tier is worth
testing, and security issues on the changed lines. In CI: `examples/switchyard-review.yml` posts it as a PR comment.
