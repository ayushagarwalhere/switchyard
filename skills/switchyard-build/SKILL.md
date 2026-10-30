---
name: switchyard-build
description: Create a new web app from a one-sentence idea, or make a change to an existing app, with NVIDIA Nemotron on Nebius Token Factory writing and testing the code. Use when the user wants to build an app ("make me an app that..."), prototype an idea quickly, or have a change implemented and tested in their app.
---

# Switchyard build: from an idea to a tested app

`switchyard new` turns one sentence into a working FastAPI + HTML/JS app with AI features on Nemotron, a
Dockerfile, pinned requirements and smoke tests. It tests the result and fixes failures before handing it over.
`switchyard code` makes a change to an existing app: it reads the code, writes files inside the app only, runs
the tests and fixes failures. Both cost about a cent or two of Token Factory credit.

## Rules

- Never read, print or ask the user to paste an API key into the chat. If no key is set, ask them to run
  `switchyard setup` in their own terminal.
- `switchyard code` never commits: show the user `git diff` and let them decide.
- Don't deploy anything as part of building; that's the `switchyard-ship` skill and it bills.

## 1. Make sure Switchyard is installed

```bash
switchyard --version
```

If missing, ask the user first, then: `pipx install "git+https://github.com/ayushagarwalhere/switchyard@v0.4.1"`.

## 2. A new app

Agree on the idea in one or two sentences (what the user does, what the AI does), then run it in the folder
where the app should be created:

```bash
switchyard new "a habit tracker that suggests one tiny next step each morning"
```

It prints the plan (features, API, AI features), writes the app into a new folder, runs the tests, and ends with
the next steps. Tell the user where it is and how to run it:

```bash
cd <folder> && pip install -r requirements.txt && uvicorn app.main:app --reload
```

## 3. A change to an existing app

From the app's folder:

```bash
switchyard code "add a dark mode toggle that remembers the choice"
```

Show the summary and `git diff`. If the tests failed, read the output it printed and either run
`switchyard code` again with a more specific request or fix it yourself.

## 4. Next

Suggest `switchyard secure` before sharing the app, and the `switchyard-ship` skill to put it online.
