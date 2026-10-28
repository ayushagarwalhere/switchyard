"""Models, endpoints and prices. Everything here can be overridden with env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

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


def api_key() -> str | None:
    return os.environ.get("NEBIUS_API_KEY") or os.environ.get("TOKEN_FACTORY_API_KEY")


def tavily_key() -> str | None:
    return os.environ.get("TAVILY_API_KEY")
