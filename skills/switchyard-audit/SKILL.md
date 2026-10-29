---
name: switchyard-audit
description: Audit a Python or JavaScript/TypeScript app's LLM calls (OpenAI, Anthropic, LiteLLM, LangChain) for security and reliability problems such as hard-coded API keys, model output reaching eval or a shell, prompt injection, system-prompt leaks, missing timeouts and token caps, and fix them. Use when the user asks to check an AI app before shipping, or asks whether their LLM code is safe.
---

# Switchyard audit: is this LLM app safe to ship?

`switchyard audit` checks every LLM call in a Python or JavaScript/TypeScript codebase. The static part is free, needs no
key and never runs the app's code. The optional probes attack each prompt with injected
instructions on NVIDIA Nemotron (a few cents) and verify a fix.

## Rules

- Never print, copy or move an API key you find. Refer to it by file and line only, and tell the
  user to rotate it.
- Don't run `--probes` until the user agrees; it calls Token Factory and costs a little.
- Fix only what the user asks you to fix, and show the diff.

## 1. Install check

```bash
switchyard --version
```

If missing, ask the user first, then: `pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.0"`.

## 2. Static audit (free)

Run it from the app's folder:

```bash
switchyard audit --fail-on never
```

Each finding has a severity, a rule, `file:line`, what's wrong and a fix:

| Rule | Meaning |
|---|---|
| AU1 (critical) | API key in source. The user must rotate it; then read it from an env var. |
| AU2 (critical/high) | Model output reaches `eval`/`exec`, a shell or SQL. Replace with a fixed set of allowed actions. |
| AU3 (medium) | A variable inside the system prompt. Move user text to a user message. |
| AU4 (low) | No `max_tokens`. |
| AU5 (low) | Client has no timeout. |
| AU6 (medium) | `json.loads` on model output without try/except. |

Summarize for the user: the count per severity, then the critical and high ones in plain words.

## 3. Probes (optional, after the user agrees)

The user needs a Token Factory key; if none is set, ask them to run `switchyard setup` in their
own terminal. Then:

```bash
switchyard audit --probes --yes --fail-on never
```

PR1 (prompt injection), PR2 (system prompt leak) and PR3 (exfiltration link) findings name the
variable the attack went through. When `fix_verified` is true, the fix text includes a system-prompt
rule that was re-tested and stopped the attack. Offer to add it to that call's system message.

## 4. Fix

For each finding the user wants fixed, edit the code at `file:line` following the finding's fix
text, then run `switchyard audit --fail-on never` again and show that the finding is gone.

For AU1 (a key in source), don't open or edit that line yourself: ask the user to rotate the key
and replace it with `os.environ["..."]`, then re-run the audit to confirm.

## CI

To keep it checked on every pull request, offer to run `switchyard ci` in the app's folder. It
adds a workflow that audits each pull request and puts findings in the repository's Security tab.
The user commits it.
