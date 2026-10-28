"""First-run setup: get a Token Factory key, check it works, save it once for every project."""
from __future__ import annotations

import getpass
import os
import sys

from . import config

KEY_URL = "https://tokenfactory.nebius.com"


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("CI") != "true"


def ask(question: str, default: bool = True) -> bool:
    """Y/n prompt; non-interactive sessions take the default."""
    if not interactive():
        return default
    try:
        ans = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return default
    return default if not ans else ans.startswith("y")


def verify_key(key: str) -> list[str]:
    """List the Nemotron models this key can reach; raises with a readable message if it can't."""
    from openai import OpenAI, AuthenticationError, APIConnectionError
    client = OpenAI(base_url=config.DEFAULT_BASE_URL, api_key=key, timeout=30, max_retries=1)
    try:
        ids = [m.id for m in client.models.list().data]
    except AuthenticationError:
        raise RuntimeError("Token Factory rejected this key (401). Copy it again from " + KEY_URL)
    except APIConnectionError as e:
        raise RuntimeError(f"couldn't reach {config.DEFAULT_BASE_URL}: {e}")
    return [i for i in ids if "nemotron" in i.lower()]


def setup(key: str | None = None, tavily: str | None = None, openai_key: str | None = None,
          check: bool = True, out=print) -> dict:
    """Save keys to ~/.switchyard/.env. Prompts for anything missing when run in a terminal."""
    if not key and interactive():
        out(f"Switchyard runs on NVIDIA Nemotron through Nebius Token Factory.\n"
            f"Create an API key at {KEY_URL} (API keys), then paste it here.")
        key = getpass.getpass("NEBIUS_API_KEY (input hidden): ").strip()
    key = key or config.api_key()
    if not key:
        raise RuntimeError(f"no key given. Get one at {KEY_URL}, then: switchyard setup --key <key>")
    found = []
    if check:
        out("Checking the key with Token Factory ...")
        found = verify_key(key)
        missing = [m.id for m in config.CANDIDATES if m.id not in found]
        out(f"  ok: {len(found)} Nemotron model(s) available" + (f"; not visible to this key: {missing}" if missing else ""))
    values = {"NEBIUS_API_KEY": key}
    if interactive() and tavily is None and not config.tavily_key():
        tavily = input("Optional TAVILY_API_KEY for realistic test inputs from the web (Enter to skip): ").strip()
    if interactive() and openai_key is None and not os.environ.get("OPENAI_API_KEY"):
        openai_key = input("Optional OPENAI_API_KEY to score your current model on the same tests (Enter to skip): ").strip()
    if tavily:
        values["TAVILY_API_KEY"] = tavily
    if openai_key:
        values["OPENAI_API_KEY"] = openai_key
    path = config.save_user_env(values)
    out(f"Saved to {path}. Every project on this machine will use it; a project's own .env still wins.")
    return {"path": str(path), "models": found, "saved": sorted(values)}


def ensure_key(out=print) -> bool:
    """True if a key is configured, after offering setup in a terminal."""
    if config.api_key():
        return True
    if not interactive():
        out(f"error: no NEBIUS_API_KEY. Run `switchyard setup` (or set NEBIUS_API_KEY). Keys: {KEY_URL}")
        return False
    out("No Token Factory key yet. Let's add one (takes a minute).\n")
    setup(out=out)
    return bool(config.api_key())
