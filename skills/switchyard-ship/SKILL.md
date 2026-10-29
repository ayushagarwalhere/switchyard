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
switchyard --version          # if missing: pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.0"
docker --version              # Docker Desktop or Docker Engine
nebius profile list           # the Nebius CLI, logged in (on Windows: inside WSL Ubuntu)
```

If the Nebius CLI is missing, the user installs it and logs in once (it opens a browser):

```bash
curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
exec -l $SHELL && nebius profile create
```

## 2. Secure (free)

```bash
switchyard secure
```

Explain CRITICAL and HIGH findings first; they block the deploy. Fix the ones the user asks you to fix and run it again.

## 3. Deploy

```bash
switchyard deploy
```

It prints each step and ends with `Live: https://...`. The app gets the user's Token Factory key as an environment
variable for its AI features, plus any `LLM_MODEL_*` overrides from `.env`. Use `--private` for an endpoint that
needs a bearer token.

## 4. Manage

```bash
switchyard deploy status        # state and URL
switchyard deploy logs          # container logs
switchyard deploy delete        # stop billing
```

Redeploying the same app replaces its old endpoint once the new one is healthy.
