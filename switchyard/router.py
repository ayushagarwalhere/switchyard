"""Runtime router: an OpenAI-compatible gateway that makes Switchyard's routing live.

An app points its `base_url` at the router and names the feature behind each call, either with the
`X-Switchyard-Feature: <name>` header or with `model="switchyard/<name>"`. The router sends the call to
that feature's model on Nebius Token Factory, falls back to the next bigger Nemotron tier when the
upstream fails or answers with nothing, adds the repaired-prompt hint when the route has one, and meters
every request (tokens, cost, latency, fallbacks) next to what the same traffic would have cost on the
original model.

Routes come from three places, later wins: the Nemotron tiers themselves (`switchyard/lightning`, ...),
~/.switchyard/routes.json, and a finished `switchyard run` (`routes_from_run`), so the decisions a migration
proved with tests become the live routing table.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import config
from .llm import Usage

log = logging.getLogger("switchyard.router")

FEATURE_HEADER = "x-switchyard-feature"
ALIAS_PREFIXES = ("switchyard/", "switchyard:")
KEEP = "keep"              # route target meaning "send the model the app asked for"
DIRECT = "direct"          # feature name for requests that name a plain model id
FLUSH_EVERY = 5.0          # seconds between writes of router_stats.json
LATENCY_SAMPLES = 1000     # per feature, for p50/p95
DEFAULT_PORT = 8787


# ---------------------------------------------------------------- routes

def _tier(model_id: str) -> config.Model | None:
    """The Nemotron candidate with this id or key, if it is one."""
    if model_id in config.MODELS:
        return config.MODELS[model_id]
    return next((m for m in config.CANDIDATES if m.id.lower() == (model_id or "").lower()), None)


def _resolve(model: str) -> str:
    """Tier keys ("lightning", "super+hint") become full model ids; anything else is kept as is."""
    if not model or model == KEEP:
        return model
    t = _tier(model.split("+")[0])
    return t.id if t else model


def _bigger(model_id: str) -> list[str]:
    """The default fallback chain after a model: every bigger Nemotron tier, cheapest first."""
    t = _tier(model_id)
    if not t:
        return []
    return [m.id for m in sorted(config.CANDIDATES, key=lambda m: m.tier) if m.tier > t.tier]


def _normalize(route) -> dict:
    """A route is {"model", "fallback", "hint", ...}; a bare string is shorthand for {"model": <it>}."""
    r = {"model": route} if isinstance(route, str) else dict(route or {})
    r["model"] = _resolve(str(r.get("model") or KEEP))
    if r.get("fallback") is None:
        r["fallback"] = [] if r["model"] == KEEP else _bigger(r["model"])
    r["fallback"] = [_resolve(m) for m in r["fallback"] if m and _resolve(m) != r["model"]]
    r["hint"] = r.get("hint") or ""
    return r


def default_routes() -> dict:
    """One route per Nemotron tier, so `switchyard/lightning` works with no setup."""
    return {m.key: _normalize({"model": m.id, "source": "default"}) for m in config.CANDIDATES}


def _routes_file() -> Path:
    return config.HOME / "routes.json"


def load_routes() -> dict:
    """Defaults merged with ~/.switchyard/routes.json (the file wins)."""
    routes = default_routes()
    f = _routes_file()
    if f.is_file():
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("ignoring %s: %s", f, e)
            saved = {}
        routes.update({k: _normalize(v) for k, v in (saved.get("routes", saved) or {}).items()})
    return routes


def save_routes(routes: dict) -> Path:
    """Write the routes that differ from the defaults to ~/.switchyard/routes.json."""
    def core(r):
        return r and (r["model"], r["fallback"], r["hint"])

    defaults = default_routes()
    keep = {k: _normalize(v) for k, v in routes.items()}
    keep = {k: v for k, v in keep.items() if core(v) != core(defaults.get(k))}
    f = _routes_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"routes": keep}, indent=1), encoding="utf-8")
    return f


def _run_path(path) -> Path:
    """A run.json, a run folder, or a bare run id under the runs folder."""
    p = Path(path).expanduser()
    if p.is_dir():
        p = p / "run.json"
    if not p.exists() and not Path(path).parent.parts:
        hit = next(config.runs_dir().rglob(f"{path}/run.json"), None)
        if hit:
            p = hit
    if not p.is_file():
        raise FileNotFoundError(f"no run.json at {path}")
    return p


def routes_from_run(path) -> dict:
    """Turn a finished migration's decisions into routes, one per call site (named after its function).

    Switched sites route to the model that passed their tests, with the bigger Nemotron tiers as fallback
    and the repaired-prompt hint if there was one. Kept sites route to "keep": the app's own model.
    """
    p = _run_path(path)
    state = json.loads(p.read_text(encoding="utf-8"))
    routes: dict = {}
    for s in state.get("sites", []):
        site, dec = s.get("site") or {}, s.get("decision") or {}
        if dec.get("action") not in ("migrate", "keep"):
            continue
        name = site.get("function") or f"{Path(site.get('file', 'call')).stem}_{site.get('line', 0)}"
        base, n = name, 2
        while name in routes:            # two call sites in functions with the same name
            name, n = f"{base}_{n}", n + 1
        model = (dec.get("model") or dec.get("choice") or "") if dec["action"] == "migrate" else KEEP
        route = _normalize({"model": model or KEEP, "hint": dec.get("hint", "") if dec["action"] == "migrate" else ""})
        route.update({"source": f"run:{state.get('id', p.parent.name)}", "file": site.get("file"),
                      "line": site.get("line"), "source_model": site.get("model")})
        if dec.get("pass_rate") is not None:
            route["pass_rate"] = dec["pass_rate"]
        routes[name] = route
    return routes


def _find(routes: dict, name: str) -> tuple[str, dict | None]:
    """Exact name first, then case-insensitive (apps use LLM_MODEL_<FEATURE> in capitals)."""
    if name in routes:
        return name, routes[name]
    for k, v in routes.items():
        if k.lower() == name.lower():
            return k, v
    return name, None


def _alias(model: str) -> str | None:
    for pre in ALIAS_PREFIXES:
        if model.lower().startswith(pre):
            return model[len(pre):].strip() or None
    return None


# ---------------------------------------------------------------- keys

def router_key() -> str:
    """The key clients send: SWITCHYARD_ROUTER_KEY, or one generated once into ~/.switchyard/router.key."""
    env = os.environ.get("SWITCHYARD_ROUTER_KEY", "").strip()
    if env:
        return env
    path = config.HOME / "router.key"
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    config.HOME.mkdir(parents=True, exist_ok=True)
    key = "sy-" + secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


# ---------------------------------------------------------------- metering

def _price_model(model_id: str) -> config.Model:
    """Nemotron tiers at Token Factory prices; anything else at its list price (as in savings estimates)."""
    return _tier(model_id) or config.model(config.SOURCE_PREFIX + (model_id or "unknown"))


def _baseline(route: dict | None) -> config.Model:
    """What the traffic would have cost without routing: the original model if known, else the biggest tier."""
    if route and route.get("source_model"):
        return config.model(config.SOURCE_PREFIX + route["source_model"])
    return max(config.CANDIDATES, key=lambda m: (m.price_out, m.price_in))


def _pct(values, q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 1)


class Meter:
    """Rolling per-feature stats in memory, with a compact summary flushed to router_stats.json."""

    def __init__(self):
        self.lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self.usage = Usage()            # tokens and cost per feature (llm.py's ledger, feature = role)
        self.features: dict[str, dict] = {}
        self.recent: deque = deque(maxlen=50)
        self.started = time.time()
        self._last_flush = 0.0

    def record(self, *, feature: str, model: str, tokens_in: int, tokens_out: int, latency_ms: float,
               fallbacks: list, status: int, stream: bool, baseline: config.Model) -> None:
        ok = 200 <= status < 300
        with self.lock:
            f = self.features.setdefault(feature, {
                "calls": 0, "errors": 0, "fallbacks": 0, "streams": 0, "baseline_cost": 0.0,
                "baseline_model": baseline.id, "models": {}, "latency": deque(maxlen=LATENCY_SAMPLES)})
            f["calls"] += 1
            f["errors"] += 0 if ok else 1
            f["fallbacks"] += len(fallbacks)
            f["streams"] += 1 if stream else 0
            f["latency"].append(latency_ms)
            f["models"][model] = f["models"].get(model, 0) + 1
            cost = 0.0
            if ok:
                cost = self.usage.add(feature, _price_model(model), tokens_in, tokens_out)
                f["baseline_cost"] += baseline.cost(tokens_in, tokens_out)
                f["baseline_model"] = baseline.id
            self.recent.append({"t": round(time.time(), 3), "feature": feature, "model": model, "status": status,
                                "tokens_in": tokens_in, "tokens_out": tokens_out, "cost": round(cost, 8),
                                "latency_ms": round(latency_ms, 1), "fallbacks": fallbacks, "stream": stream})
        log.info("%s -> %s %s %.0fms in=%d out=%d fallbacks=%d", feature, model, status, latency_ms,
                 tokens_in, tokens_out, len(fallbacks))
        if time.monotonic() - self._last_flush >= FLUSH_EVERY:
            self.flush()

    def summary(self, recent: bool = True) -> dict:
        with self.lock:
            feats = {}
            tot = {"calls": 0, "errors": 0, "fallbacks": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
                   "baseline_cost": 0.0}
            for name, f in self.features.items():
                u = self.usage.by_role.get(name, {})
                row = {"calls": f["calls"], "errors": f["errors"], "fallbacks": f["fallbacks"], "streams": f["streams"],
                       "latency_p50_ms": _pct(f["latency"], 0.5), "latency_p95_ms": _pct(f["latency"], 0.95),
                       "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0),
                       "cost": round(u.get("cost", 0.0), 8), "baseline_model": f["baseline_model"],
                       "baseline_cost": round(f["baseline_cost"], 8), "models": dict(f["models"])}
                row["saved"] = round(row["baseline_cost"] - row["cost"], 8)
                row["saving_pct"] = round(100 * row["saved"] / row["baseline_cost"], 1) if row["baseline_cost"] else None
                feats[name] = row
                for k in tot:
                    tot[k] += row[k]
            tot["cost"], tot["baseline_cost"] = round(tot["cost"], 8), round(tot["baseline_cost"], 8)
            tot["saved"] = round(tot["baseline_cost"] - tot["cost"], 8)
            tot["saving_pct"] = round(100 * tot["saved"] / tot["baseline_cost"], 1) if tot["baseline_cost"] else None
            out = {"started": self.started, "updated": time.time(), "totals": tot, "features": feats}
            if recent:
                out["recent"] = list(self.recent)[-20:]
            return out

    def flush(self) -> None:
        """Write the compact summary (no per-request log) atomically."""
        data = self.summary(recent=False)
        path = config.HOME / "router_stats.json"
        with self._flush_lock:
            self._last_flush = time.monotonic()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as e:
                log.warning("could not write %s: %s", path, e)


_meter: Meter | None = None   # the meter of the most recently created app (what stats_summary reports)


def stats_summary() -> dict:
    """Live stats from the router in this process, else the last summary it wrote to disk."""
    if _meter is not None:
        return _meter.summary()
    path = config.HOME / "router_stats.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"totals": {}, "features": {}}


# ---------------------------------------------------------------- the gateway

def _error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    """Errors in OpenAI's shape so SDKs surface the message."""
    return JSONResponse({"error": {"message": message, "type": kind, "code": status}}, status_code=status)


def _empty(data) -> bool:
    """No usable answer: no text and no tool calls in the first choice."""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return True
    return not (str(msg.get("content") or "").strip() or msg.get("tool_calls") or msg.get("function_call"))


def _usage(data) -> tuple[int, int]:
    u = (data.get("usage") or {}) if isinstance(data, dict) else {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _stream_usage(tail: bytes) -> tuple[int, int]:
    """Tokens from the final SSE chunk, if the upstream put usage there (stream_options.include_usage)."""
    for line in reversed(tail.decode("utf-8", "ignore").splitlines()):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            return _usage(json.loads(payload))
        except ValueError:
            return 0, 0    # cut-off chunk: count nothing rather than guess
    return 0, 0


def create_app(routes: dict | None = None, upstream: str | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """The router app. `routes` defaults to load_routes(); `transport` lets tests fake Token Factory."""
    global _meter
    table = default_routes()
    table.update({k: _normalize(v) for k, v in (load_routes() if routes is None else routes).items()})
    base = (upstream or config.DEFAULT_BASE_URL).rstrip("/") + "/"
    key = router_key()
    meter = _meter = Meter()
    timeout = httpx.Timeout(float(os.environ.get("SWITCHYARD_ROUTER_TIMEOUT", "120")), connect=10.0)
    holder: dict = {}

    def http() -> httpx.AsyncClient:
        if "client" not in holder:
            holder["client"] = httpx.AsyncClient(transport=transport, timeout=timeout)
        return holder["client"]

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if "client" in holder:
            await holder.pop("client").aclose()
        meter.flush()

    app = FastAPI(title="Switchyard router", lifespan=lifespan)
    app.state.routes, app.state.meter = table, meter

    def denied(request: Request) -> JSONResponse | None:
        auth = request.headers.get("authorization", "")
        sent = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if sent and hmac.compare_digest(sent.encode(), key.encode()):
            return None
        return _error(401, "missing or wrong router key (Authorization: Bearer <SWITCHYARD_ROUTER_KEY>)",
                      "authentication_error")

    def upstream_headers() -> dict | None:
        tf = config.api_key()
        if not tf:
            return None
        return {"Authorization": f"Bearer {tf}", "Content-Type": "application/json", "Accept-Encoding": "identity"}

    def plan(body: dict, header: str | None):
        """(feature, route, model chain) for a request, or an error response."""
        model = str(body.get("model") or "").strip()
        alias = _alias(model)
        name = (header or "").strip() or alias
        if not name:
            if not model:
                return _error(400, "request has no model")
            return DIRECT, None, [model] + _bigger(model)
        name, route = _find(table, name)
        if route is None and (alias or not model):
            return _error(404, f"no route for feature '{name}'; known: {', '.join(sorted(table))}", "not_found_error")
        if route is None or route["model"] == KEEP:
            if alias or not model:
                return _error(400, f"route '{name}' keeps the app's own model: send that model in `model` and name "
                                   f"the feature with the X-Switchyard-Feature header")
            return name, route, [model] + _bigger(model)
        chain = [route["model"]] + [m for m in route["fallback"] if m != route["model"]]
        return name, route, chain

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "routes": len(table), "upstream_key": bool(config.api_key())}

    @app.get("/routes")
    async def get_routes(request: Request):
        return denied(request) or {"routes": table}

    @app.get("/stats")
    async def get_stats(request: Request):
        return denied(request) or meter.summary()

    @app.get("/v1/models")
    @app.get("/models")
    async def models(request: Request):
        if (bad := denied(request)) is not None:
            return bad
        data, headers = [], upstream_headers()
        if headers:
            try:
                r = await http().get(base + "models", headers=headers)
                if r.status_code == 200:
                    data = list(r.json().get("data") or [])
            except (httpx.TransportError, ValueError) as e:
                log.warning("upstream model list failed: %s", type(e).__name__)
        data += [{"id": f"switchyard/{name}", "object": "model", "owned_by": "switchyard",
                  "target": r["model"], "fallback": r["fallback"]} for name, r in table.items()]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat(request: Request):
        if (bad := denied(request)) is not None:
            return bad
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "body is not JSON")
        if not isinstance(body, dict):
            return _error(400, "body must be a JSON object")
        planned = plan(body, request.headers.get(FEATURE_HEADER))
        if isinstance(planned, Response):
            return planned
        feature, route, chain = planned
        headers = upstream_headers()
        if headers is None:
            return _error(503, "the router has no Token Factory key (run `switchyard setup`)", "server_error")
        hint = (route or {}).get("hint")
        msgs = list(body.get("messages") or [])
        if hint and not any(isinstance(m, dict) and m.get("content") == hint for m in msgs):
            # the repaired prompt, added the same way the migration patches it in: a leading system message
            body = {**body, "messages": [{"role": "system", "content": hint}] + msgs}
        baseline = _baseline(route)
        stream = bool(body.get("stream"))
        url = base + "chat/completions"
        t0 = time.perf_counter()
        fallbacks: list[dict] = []

        def ms() -> float:
            return (time.perf_counter() - t0) * 1000

        def done(model: str, status: int, tin: int = 0, tout: int = 0, latency: float | None = None) -> dict:
            meter.record(feature=feature, model=model, tokens_in=tin, tokens_out=tout,
                         latency_ms=ms() if latency is None else latency, fallbacks=fallbacks, status=status,
                         stream=stream, baseline=baseline)
            return {"x-switchyard-feature": feature, "x-switchyard-model": model,
                    "x-switchyard-fallbacks": str(len(fallbacks))}

        for i, model in enumerate(chain):
            last = i == len(chain) - 1
            payload = {**body, "model": model}

            def next_model(reason: str) -> None:
                fallbacks.append({"from": model, "to": chain[i + 1], "reason": reason})
                log.info("%s: %s failed (%s), falling back to %s", feature, model, reason, chain[i + 1])

            if not stream:
                try:
                    r = await http().post(url, json=payload, headers=headers)
                except httpx.TransportError as e:
                    if not last:
                        next_model(type(e).__name__)
                        continue
                    timed_out = isinstance(e, httpx.TimeoutException)
                    status = 504 if timed_out else 502
                    h = done(model, status)
                    return JSONResponse({"error": {"message": f"upstream {type(e).__name__}", "type": "upstream_error",
                                                   "code": status}}, status_code=status, headers=h)
                if (r.status_code == 429 or r.status_code >= 500) and not last:
                    next_model(f"HTTP {r.status_code}")
                    continue
                data = None
                if r.status_code < 300:
                    try:
                        data = r.json()
                    except ValueError:
                        data = None
                    if _empty(data) and not last:
                        next_model("empty answer")
                        continue
                tin, tout = _usage(data)
                h = done(model, r.status_code, tin, tout)
                return Response(r.content, status_code=r.status_code, headers=h,
                                media_type=r.headers.get("content-type", "application/json"))

            # streaming: commit to a model once its first bytes arrive, then pass the SSE through unchanged
            try:
                r = await http().send(http().build_request("POST", url, json=payload, headers=headers), stream=True)
            except httpx.TransportError as e:
                if not last:
                    next_model(type(e).__name__)
                    continue
                status = 504 if isinstance(e, httpx.TimeoutException) else 502
                h = done(model, status)
                return JSONResponse({"error": {"message": f"upstream {type(e).__name__}", "type": "upstream_error",
                                               "code": status}}, status_code=status, headers=h)
            if r.status_code >= 300:
                content = await r.aread()
                await r.aclose()
                if (r.status_code == 429 or r.status_code >= 500) and not last:
                    next_model(f"HTTP {r.status_code}")
                    continue
                h = done(model, r.status_code)
                return Response(content, status_code=r.status_code, headers=h,
                                media_type=r.headers.get("content-type", "application/json"))
            chunks = r.aiter_bytes()
            try:
                first = await chunks.__anext__()
            except StopAsyncIteration:
                first = b""
            except httpx.TransportError as e:
                await r.aclose()
                if not last:
                    next_model(type(e).__name__)
                    continue
                done(model, 502)
                return _error(502, f"upstream {type(e).__name__}", "upstream_error")
            if not first and not last:
                await r.aclose()
                next_model("empty answer")
                continue
            ttfb = ms()
            h = {"x-switchyard-feature": feature, "x-switchyard-model": model,
                 "x-switchyard-fallbacks": str(len(fallbacks)), "cache-control": "no-cache"}

            async def relay(r=r, chunks=chunks, first=first, model=model, ttfb=ttfb):
                tail, status = first[-16384:], 200
                try:
                    if first:
                        yield first
                    async for chunk in chunks:
                        tail = (tail + chunk)[-16384:]
                        yield chunk
                except httpx.TransportError:
                    status = 502    # the stream broke midway; the client already has part of it
                finally:
                    await r.aclose()
                    tin, tout = _stream_usage(tail)
                    done(model, status, tin, tout, latency=ttfb)

            return StreamingResponse(relay(), status_code=200, headers=h,
                                     media_type=r.headers.get("content-type", "text/event-stream"))
        return _error(502, "no model to try", "upstream_error")   # unreachable: chains are never empty

    return app


# ---------------------------------------------------------------- running it

def snippet(port: int = DEFAULT_PORT) -> str:
    """The code change an app needs: point the OpenAI SDK at the router and name the feature."""
    return (f'client = OpenAI(base_url="http://127.0.0.1:{port}/v1",              # was Token Factory / OpenAI\n'
            f'                api_key=os.environ["SWITCHYARD_ROUTER_KEY"])        # the router key (~/.switchyard/router.key)\n'
            f'client.chat.completions.create(model="switchyard/<feature>", messages=messages)  # route per feature')


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, run=None) -> None:
    """Run the router: saved routes, plus the decisions of a finished run if one is given."""
    import uvicorn

    routes = load_routes()
    if run:
        routes.update(routes_from_run(run))
    if not config.api_key():
        log.warning("no Token Factory key: requests will fail until `switchyard setup` is run")
    router_key()   # make sure the key exists before the first client shows up
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    where = "SWITCHYARD_ROUTER_KEY" if os.environ.get("SWITCHYARD_ROUTER_KEY") else str(config.HOME / "router.key")
    log.info("Switchyard router on http://%s:%d/v1 with %d routes (client key: %s)", host, port, len(routes), where)
    uvicorn.run(create_app(routes), host=host, port=port, log_level="warning")
