---
name: switchyard
description: Move a Python or JavaScript/TypeScript app's OpenAI, Anthropic, LiteLLM or LangChain calls to NVIDIA Nemotron on Nebius Token Factory, one call site at a time, keeping only the switches that pass generated tests. Use when the user asks to cut LLM costs, migrate to Nemotron or Nebius Token Factory, or find which LLM calls a cheaper model can handle.
---

# Switchyard: migrate LLM calls to Nemotron, with tests

Switchyard is a command-line tool. It finds every LLM call in a Python or JavaScript/TypeScript codebase, writes tests for
each one, runs them on three Nemotron models, and patches only the calls where a cheaper model
matches the quality bar. Calls that don't pass stay on their original model.

You drive it with shell commands. Spending happens only in step 3, and only after the user agrees.

## Rules

- Never read, print, copy or ask the user to paste an API key into the chat. Keys are entered by
  the user in their own terminal with `switchyard setup`, or live in their `.env`.
- Never start a paid run (`switchyard run --yes`) until the user has seen the cost estimate from
  step 2 and said yes.
- Never commit, push or open a pull request unless the user asks for it.
- Run commands from the app's folder (the folder with its Python code), inside its git checkout.

## 1. Make sure Switchyard is installed

```bash
switchyard --version
```

If the command is missing, ask the user before installing anything, then install the pinned release:

```bash
pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.2.0"
```

(`uv tool install "git+https://github.com/ayushagarwalhere/switchyard@v0.2.0"` works too.)

## 2. Free look: what would be tested, and what it costs

```bash
switchyard run --dry-run
```

No key and no spending. It lists each LLM call (file, line, function, current model) and prints an
estimated Token Factory cost with the hard budget cap. Show the user the list and the estimate.

If it finds no calls, tell the user which SDKs are supported (Python: OpenAI, Anthropic, LiteLLM,
LangChain `ChatOpenAI`/`ChatAnthropic`/`init_chat_model`; JavaScript/TypeScript: OpenAI SDK, Anthropic
SDK, Vercel AI SDK `generateText`/`streamText`/`generateObject`) and stop.

Useful options: `--include "app/**"` / `--exclude "tests/**"` to narrow the scan, `--tests 4` for a
cheaper first pass, `--budget 1` to lower the cap (USD).

## 3. Run it (after the user says yes)

If the user hasn't set up a key yet, ask them to run this in their own terminal (it hides input
and checks the key):

```bash
switchyard setup
```

Then start the run with the options the user agreed to:

```bash
switchyard run --yes
```

It takes a few minutes and prints progress per call. Spending never exceeds `--budget`.

## 4. Report the results

```bash
switchyard show
```

Tell the user, per call: switched to which Nemotron model, or kept on the original and why. Also
give the estimated saving per request, how many switched functions were verified by running the
patched code, and the run's cost. `switchyard show --json` gives the same data as JSON. Calls
listed as "manual" passed the tests but need a hand edit; pass on the reason shown.

## 5. Apply the patch (if the user wants it)

```bash
switchyard apply
```

This creates a new branch `switchyard/nemotron-<run>` with the patch, `.env.switchyard.example`
(the env vars the patched code reads), the saved tests under `.switchyard/` and a CI workflow that
re-runs them. Nothing is committed. Show the user `git diff --stat` and offer to walk through the
diff. The patched code reads `NEBIUS_API_KEY`, and each call's model sits behind an env var, so
rolling one back is a one-line change.

If the user asks for a pull request instead: `switchyard pr` (needs the GitHub CLI, `gh`).

## Later: check the switches still hold

```bash
switchyard check
```

Re-runs the saved tests on the switched calls (small cost) and fails if any call dropped below the
bar, for example after a prompt edit.

## When something fails

- `no NEBIUS_API_KEY`: ask the user to run `switchyard setup` in their terminal.
- `uncommitted changes`: ask the user to commit or stash their work, then re-run `switchyard apply`.
- `budget`: the run stopped at the cap. Offer a smaller run (`--tests 4`, `--max-sites 5`) or a
  higher `--budget`, and let the user choose.
- Anything else: show the user the error text as-is.
