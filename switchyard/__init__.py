"""Switchyard: move LLM calls to NVIDIA Nemotron on Nebius Token Factory, with per-call proof.

    import switchyard
    calls = switchyard.scan(".")                   # static, no API calls
    result = switchyard.migrate(".", budget=1.0)   # test every call on Nemotron, write the patch
    result.apply()                                 # new branch in your checkout
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__version__ = "0.3.0"
__all__ = ["scan", "migrate", "Result", "__version__"]


def scan(path: str = ".") -> list[dict]:
    """Every OpenAI / Anthropic SDK call under `path`: file, line, function, model, prompt template."""
    from .scanner import scan_repo
    return scan_repo(path)["calls"]


@dataclass
class Result:
    """A finished (or failed) run. `state` is the full record that run.json holds."""
    state: dict
    dir: Path

    @property
    def id(self) -> str:
        return self.state["id"]

    @property
    def ok(self) -> bool:
        return self.state.get("status") == "done"

    @property
    def summary(self) -> dict:
        return self.state.get("summary") or {}

    @property
    def decisions(self) -> list[dict]:
        """One row per call site: where it is, what it was, what it moves to (or why it stays)."""
        rows = []
        for s in self.state.get("sites", []):
            d, site = s.get("decision") or {}, s["site"]
            rows.append({"file": site["file"], "line": site["line"], "function": site.get("function"),
                         "from": site.get("model") or site.get("model_expr"), "action": d.get("action", "error"),
                         "to": d.get("choice") if d.get("action") == "migrate" else None,
                         "reason": d.get("reason") or s.get("error")})
        return rows

    @property
    def patch_path(self) -> Path | None:
        p = self.dir / "migration.patch"
        return p if p.exists() and p.stat().st_size else None

    @property
    def cost(self) -> float:
        return (self.state.get("usage") or {}).get("cost", 0.0)

    def apply(self, repo: str | None = None, **kw) -> dict:
        """Apply the patch on a new branch (defaults to the checkout that was scanned)."""
        from .gitops import apply
        return apply(str(self.dir), repo, **kw)

    def open_pr(self, repo: str | None = None, **kw) -> str:
        from .gitops import open_pr
        return open_pr(str(self.dir), repo, **kw)


def migrate(path: str = ".", *, tests: int = 8, target: float = 0.85, budget: float = 2.0,
            tavily: bool = True, execute: bool | None = None, on_event=None, **kw) -> Result:
    """Run the full pipeline on a folder or git URL. Spend never exceeds `budget` (USD)."""
    from .pipeline import Run
    run = Run(path, tests=tests, target=target, budget=budget, use_tavily=tavily,
              execute=execute, emit=on_event, **kw)
    return Result(run.run(), run.dir)

