"""Thin Token Factory client: retries, JSON extraction, and a running cost ledger."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from openai import OpenAI

from . import config


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    by_role: dict = field(default_factory=dict)

    def add(self, role: str, model: config.Model, tin: int, tout: int) -> float:
        c = model.cost(tin, tout)
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        self.cost += c
        r = self.by_role.setdefault(role, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "model": model.key})
        r["calls"] += 1
        r["tokens_in"] += tin
        r["tokens_out"] += tout
        r["cost"] += c
        return c

    def to_dict(self) -> dict:
        return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cost": round(self.cost, 6), "by_role": self.by_role}


@dataclass
class Completion:
    text: str
    reasoning: str | None
    tokens_in: int
    tokens_out: int
    latency: float
    cost: float
    model: str
    error: str | None = None
    tool_calls: list | None = None


THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)


class TokenFactory:
    def __init__(self, budget_usd: float | None = None, tracer=None):
        key = config.api_key()
        if not key:
            raise RuntimeError("Set NEBIUS_API_KEY in .env (get one at tokenfactory.nebius.com)")
        self.client = OpenAI(base_url=config.DEFAULT_BASE_URL, api_key=key, max_retries=3, timeout=180)
        if tracer is not None:
            self.client = tracer(self.client)
        self.usage = Usage()
        self.budget = budget_usd
        self._lock = threading.Lock()
        # cap parallel requests so a big repo doesn't trip rate limits
        self._slots = threading.BoundedSemaphore(int(os.environ.get("SWITCHYARD_CONCURRENCY", "8")))
        # flipped off if the endpoint rejects Nemotron's thinking switch
        self.thinking_toggle = os.environ.get("SWITCHYARD_THINKING_TOGGLE", "1") == "1"
        self.fallbacks: list[str] = []
        # the app's original model, run on the same tests for a head-to-head (OpenAI key; billed by OpenAI)
        bkey = config.baseline_key()
        self.baseline = OpenAI(api_key=bkey, max_retries=3, timeout=120) if bkey else None
        self.baseline_usage = Usage()

    def complete(self, model_key: str, messages: list[dict], *, role: str = "candidate", temperature: float | None = None,
                 max_tokens: int | None = None, json_mode: bool = False, reasoning: bool | None = None,
                 extra: dict | None = None, response_format: dict | None = None, tools: list[dict] | None = None) -> Completion:
        model = config.model(model_key)
        source = model_key.startswith(config.SOURCE_PREFIX)
        if source and self.baseline is None:
            return Completion("", None, 0, 0, 0.0, 0.0, model_key, error="no OPENAI_API_KEY for the baseline")
        client = self.baseline if source else self.client
        usage = self.baseline_usage if source else self.usage
        if source:
            reasoning = None  # Nemotron's thinking switch means nothing to the original provider
        with self._lock:
            if self.budget is not None and self.usage.cost >= self.budget:
                raise BudgetExceeded(f"budget of ${self.budget:.2f} reached")
        kwargs: dict = {"model": model.id, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            # OpenAI's reasoning models (o-series, gpt-5) reject max_tokens; max_completion_tokens works on all
            kwargs["max_completion_tokens" if source else "max_tokens"] = max_tokens
        if response_format:
            kwargs["response_format"] = response_format
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
        extra_body = dict(extra or {})
        if reasoning is not None and self.thinking_toggle:
            # Nemotron chat templates expose a thinking switch.
            extra_body.setdefault("chat_template_kwargs", {})["enable_thinking"] = reasoning
        if extra_body:
            kwargs["extra_body"] = extra_body
        t0 = time.perf_counter()
        try:
            with self._slots:
                resp = client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - surface any API error as a failed sample
            msg_e = str(e)
            if "chat_template_kwargs" in kwargs.get("extra_body", {}) and ("chat_template_kwargs" in msg_e or "enable_thinking" in msg_e or "extra" in msg_e.lower()):
                self.thinking_toggle = False
                self._note("endpoint rejected chat_template_kwargs.enable_thinking; continuing without the thinking switch")
                return self.complete(model_key, messages, role=role, temperature=temperature, max_tokens=max_tokens,
                                     json_mode=json_mode, reasoning=None, extra=extra, response_format=response_format,
                                     tools=tools)
            if (json_mode or response_format) and "response_format" in msg_e:
                self._note(f"{model.key}: endpoint rejected {'json_schema' if response_format else 'json_object'} response_format; falling back")
                # endpoint rejected structured output: fall back one level (schema -> json_object -> plain)
                return self.complete(model_key, messages, role=role, temperature=temperature, max_tokens=max_tokens,
                                     json_mode=bool(response_format), reasoning=reasoning, extra=extra)
            return Completion("", None, 0, 0, time.perf_counter() - t0, 0.0, model.key, error=f"{type(e).__name__}: {e}"[:500])
        latency = time.perf_counter() - t0
        msg = resp.choices[0].message
        text = msg.content or ""
        reasoning_text = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        m = THINK_RE.search(text)
        if m:
            reasoning_text = (reasoning_text or "") + m.group(1)
            text = THINK_RE.sub("", text)
        elif "</think>" in text:  # template that only emits the closing tag
            head, text = text.split("</think>", 1)
            reasoning_text = (reasoning_text or "") + head
        tin = getattr(resp.usage, "prompt_tokens", 0) or 0
        tout = getattr(resp.usage, "completion_tokens", 0) or 0
        with self._lock:
            cost = usage.add(role, model, tin, tout)
        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments} for tc in msg.tool_calls]
        return Completion(text.strip(), reasoning_text, tin, tout, latency, cost, model.key, tool_calls=tool_calls)

    def _note(self, text: str) -> None:
        with self._lock:
            if text not in self.fallbacks:
                self.fallbacks.append(text)

    def json(self, model_key: str, messages: list[dict], *, role: str, temperature: float = 0.2,
             max_tokens: int | None = None, reasoning: bool | None = None, retries: int = 2) -> tuple[dict | list, Completion]:
        last = None
        for attempt in range(retries + 1):
            c = self.complete(model_key, messages, role=role, temperature=temperature, max_tokens=max_tokens,
                              json_mode=True, reasoning=reasoning)
            last = c
            if c.error:
                continue
            data = extract_json(c.text)
            if data is not None:
                return data, c
            messages = messages + [{"role": "assistant", "content": c.text[:2000]},
                                   {"role": "user", "content": "That was not valid JSON. Reply with only the JSON."}]
        raise ValueError(f"model did not return JSON: {(last.error or last.text)[:300] if last else ''}")


def extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None
