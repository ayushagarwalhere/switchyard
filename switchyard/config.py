"""Models, endpoints and prices. Everything here can be overridden with env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass

from pathlib import Path

from dotenv import dotenv_values, find_dotenv

# Where Switchyard keeps your key (from `switchyard setup`) and, once installed, its runs.
HOME = Path(os.environ.get("SWITCHYARD_HOME") or Path.home() / ".switchyard").expanduser()
USER_ENV = HOME / ".env"
# A source checkout of Switchyard keeps runs next to the code (the web app's demo runs live there).
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """The project's .env (searched from the current folder up) wins over the saved user key."""
    # .env.local first (Next.js / Vite convention), then .env, from the current folder up
    files = [Path(find_dotenv(".env.local", usecwd=True) or ""), Path(find_dotenv(usecwd=True) or "")]
    if (SOURCE_ROOT / "pyproject.toml").exists():
        files += [SOURCE_ROOT / ".env.local", SOURCE_ROOT / ".env"]
    files.append(USER_ENV)
    for f in files:
        if not f.is_file():
            continue
        for k, v in dotenv_values(f).items():
            # first non-empty value wins; a blank `NEBIUS_API_KEY=` copied from .env.example doesn't hide the saved key
            if v and not os.environ.get(k):
                os.environ[k] = v


load_env()


def runs_dir() -> Path:
    if os.environ.get("SWITCHYARD_RUNS"):
        return Path(os.environ["SWITCHYARD_RUNS"]).expanduser()
    if (SOURCE_ROOT / "pyproject.toml").exists() and (SOURCE_ROOT / "runs").is_dir():
        return SOURCE_ROOT / "runs"
    return HOME / "runs"


def save_user_env(values: dict[str, str]) -> Path:
    """Write keys to ~/.switchyard/.env (merged with what's there), readable only by you."""
    HOME.mkdir(parents=True, exist_ok=True)
    lines, seen = [], set()
    if USER_ENV.exists():
        for line in USER_ENV.read_text(encoding="utf-8").splitlines():
            k = line.split("=", 1)[0].strip()
            if k in values:
                seen.add(k)
                line = f"{k}={values[k]}"
            lines.append(line)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    USER_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        USER_ENV.chmod(0o600)
    except OSError:
        pass
    for k, v in values.items():
        os.environ[k] = v
    return USER_ENV

# Token Factory is OpenAI-compatible. Ultra is served from us-central1.
DEFAULT_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/")


@dataclass(frozen=True)
class Model:
    key: str            # short name used in the UI
    id: str             # Token Factory model id
    tier: int           # 1 = cheapest
    price_in: float     # USD per 1M input tokens
    price_out: float    # USD per 1M output tokens
    label: str

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return tokens_in / 1e6 * self.price_in + tokens_out / 1e6 * self.price_out


# Candidate models, cheapest first. Prices are list prices from the Token Factory
# catalog; override with SWITCHYARD_PRICES="key:in:out,key:in:out" if they change.
CANDIDATES: list[Model] = [
    Model("lightning", os.environ.get("MODEL_LIGHTNING", "nvidia/Nemotron-3_5-Lightning"), 1, 0.06, 0.24, "Nemotron 3.5 Lightning"),
    Model("super", os.environ.get("MODEL_SUPER", "nvidia/nemotron-3-super-120b-a12b"), 2, 0.30, 0.90, "Nemotron 3 Super"),
    Model("ultra", os.environ.get("MODEL_ULTRA", "nvidia/Nemotron-3-Ultra-550b-a55b"), 3, 1.00, 3.00, "Nemotron 3 Ultra"),
]


def _apply_price_overrides() -> None:
    raw = os.environ.get("SWITCHYARD_PRICES", "")
    for part in filter(None, (p.strip() for p in raw.split(","))):
        key, pin, pout = part.split(":")
        for i, m in enumerate(CANDIDATES):
            if m.key == key:
                CANDIDATES[i] = Model(m.key, m.id, m.tier, float(pin), float(pout), m.label)


_apply_price_overrides()
MODELS = {m.key: m for m in CANDIDATES}

# Roles inside Switchyard's own pipeline (the "Nano for cheap calls, Ultra for hard
# reasoning" split the hackathon recommends).
ROLE_ANALYZE = os.environ.get("ROLE_ANALYZE", "super")    # understand each call site
ROLE_TESTGEN = os.environ.get("ROLE_TESTGEN", "ultra")    # write tests + reference answers
ROLE_JUDGE = os.environ.get("ROLE_JUDGE", "super")        # grade candidate outputs
ROLE_REPAIR = os.environ.get("ROLE_REPAIR", "ultra")      # adapt prompts for smaller models

# Prices of the models apps are migrating *from* (USD per 1M tokens, in/out).
# Used only to estimate savings. Unknown models fall back to "default".
SOURCE_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o1": (15.00, 60.00),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "default": (2.50, 10.00),
}


def source_price(model_name: str | None) -> tuple[float, float, str]:
    """Best match for a source model name, longest prefix wins."""
    name = (model_name or "").lower()
    best = ""
    for key in SOURCE_PRICES:
        if key != "default" and name.startswith(key) and len(key) > len(best):
            best = key
    if best:
        return (*SOURCE_PRICES[best], best)
    return (*SOURCE_PRICES["default"], "default (gpt-4o pricing)")


SOURCE_PREFIX = "source:"


def model(key: str) -> Model:
    """A candidate by key, or the app's original model as `source:<model id>` (the head-to-head baseline)."""
    if key.startswith(SOURCE_PREFIX):
        mid = key[len(SOURCE_PREFIX):]
        pin, pout, _ = source_price(mid)
        return Model(key, mid, 0, pin, pout, f"{mid} (original)")
    return MODELS[key]


def baseline_key() -> str | None:
    """OpenAI key for running the original model on the same tests. Off with SWITCHYARD_BASELINE=0."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if os.environ.get("SWITCHYARD_BASELINE", "1") != "1" or not key.startswith("sk-") or "placeholder" in key:
        return None
    return key


KEY_NAMES = ("NEBIUS_API_KEY", "TOKEN_FACTORY_API_KEY", "NEBIUS_TOKEN_FACTORY_KEY", "NEBIUS_TOKEN_FACTORY_API_KEY")


def api_key() -> str | None:
    key = next((os.environ[k] for k in KEY_NAMES if os.environ.get(k)), None)
    if key and not os.environ.get("NEBIUS_API_KEY"):
        os.environ["NEBIUS_API_KEY"] = key     # the patched code and the runtime check read this name
    return key


def tavily_key() -> str | None:
    return os.environ.get("TAVILY_API_KEY")
