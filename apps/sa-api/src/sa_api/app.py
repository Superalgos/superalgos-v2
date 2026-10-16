"""The FastAPI application: the dashboard contract (GET only, except the three ``/api/auth/*`` sign-in routes, SEP-1.8/SEP-2.1/SEP-2.2,
and the two strategy-designer routes ``POST /api/strategy/validate`` and ``POST /api/backtests/run``) and the built web UI at ``/``."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi import Response as FResponse
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.types import Scope

from sa_api import models as m
from sa_api.config import ApiConfig, ConfigError
from sa_api.jobs import (
    Job,
    JobBusy,
    JobManager,
    RequestInvalid,
    is_loopback_host,
    parse_request,
    request_host_is_loopback,
)
from sa_api.market import MarketError, MarketService
from sa_api.service import NotFoundError, Service
from sa_api.siwe import SESSION_COOKIE, SiweAuth, SiweError
from sa_engine.schema import validate_issues

MAX_STRATEGY_BYTES = 256 * 1024


def _status(job: Job) -> m.JobStatus:
    return m.JobStatus(
        id=job.id, state=job.state, started_ms=job.started_ms, finished_ms=job.finished_ms,
        backtest_id=job.backtest_id, error=job.error,
    )  # fmt: skip


class WebFiles(StaticFiles):
    """Static dashboard with cache headers that survive an update.

    Hashed ``assets/*`` never change under the same name, so they are immutable. Everything else
    (``index.html``, the directory fallback, unhashed files) must revalidate on every load, or a
    browser keeps an old index pointing at an old bundle.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            if path.replace("\\", "/").lstrip("./").startswith("assets/"):
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                resp.headers["Cache-Control"] = "no-cache"
        return resp


_BUILD_META = re.compile(
    r"""<meta\s+name=["']sa-build["']\s+content=["']([A-Za-z0-9_.-]+)["']""", re.IGNORECASE
)


def web_build_id(dist: Path) -> str | None:
    """The build id the dashboard bundle in ``dist`` was stamped with (its ``sa-build`` meta tag), if any."""
    try:
        html = (dist / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None
    found = _BUILD_META.search(html)
    return found.group(1) if found else None


def create_app(
    cfg: ApiConfig,
    service: Service | None = None,
    auth: SiweAuth | None = None,
    market: MarketService | None = None,
    bind_host: str | None = None,
    jobs: JobManager | None = None,
) -> FastAPI:
    """Build the app. ``service`` and ``auth`` are injectable for tests. Fails early if ``web_dist`` is set but missing.

    ``bind_host`` is the address the server listens on. ``POST /api/backtests/run`` starts a CPU-heavy job, so it is
    allowed only when this is a loopback address (``None``, unknown, counts as not loopback).
    """
    svc = service or Service(cfg)
    mkt = market or MarketService(cfg)
    runner = jobs or JobManager(cfg)
    siwe = auth or SiweAuth(
        domain=cfg.siwe_domain, uri=cfg.siwe_uri, chain_id=cfg.siwe_chain_id,
        nonce_ttl_s=cfg.siwe_nonce_ttl_s, session_ttl_s=cfg.session_ttl_s,
        nonce_rate_per_min=cfg.nonce_rate_per_min,
    )  # fmt: skip
    app = FastAPI(
        title="Superalgos v2 dashboard API",
        version="0.1.0",
        description="Read-only: every number is recomputed from signed history. The only POST routes are /api/auth/* (Sign-In with Ethereum), POST /api/strategy/validate (pure) and POST /api/backtests/run (loopback only); none holds a key or publishes anything.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=404)

    def missing(what: str, key: str) -> NotFoundError:
        return NotFoundError(f"{what} not found: {key}; list them at the collection endpoint")

    @app.get("/api/providers", response_model=list[m.Provider])
    async def providers() -> list[m.Provider]:
        return await svc.providers()

    @app.get("/api/providers/{pubkey}", response_model=m.Provider)
    async def provider(pubkey: str) -> m.Provider:
        p = await svc.provider(pubkey)
        if p is None:
            raise missing("provider", pubkey)
        return p

    @app.get("/api/providers/{pubkey}/record", response_model=m.TrackRecord)
    async def record(pubkey: str) -> m.TrackRecord:
        r = await svc.record(pubkey)
        if r is None:
            raise missing("provider", pubkey)
        return r

    @app.get("/api/providers/{pubkey}/signals", response_model=list[m.Signal])
    async def signals(
        pubkey: str, limit: Annotated[int, Query(ge=1, le=1000)] = 50
    ) -> list[m.Signal]:
        s = await svc.signals(pubkey, limit)
        if s is None:
            raise missing("provider", pubkey)
        return s

    if cfg.dev_views:
        # Registered only with dev views on: with them off the path is just an unknown route (404).
        @app.get("/api/dev/providers/{pubkey}/releases", response_model=list[m.DevSignal])
        async def dev_releases(
            pubkey: str, limit: Annotated[int, Query(ge=1, le=1000)] = 50
        ) -> list[m.DevSignal]:
            s = await svc.dev_signals(pubkey, limit)
            if s is None:
                raise missing("provider", pubkey)
            return s

    @app.get("/api/backtests", response_model=list[m.Backtest])
    async def backtests() -> list[m.Backtest]:
        return svc.backtests()

    @app.get("/api/backtests/{bid}", response_model=m.Backtest)
    async def backtest(bid: str) -> m.Backtest:
        b = svc.backtest(bid)
        if b is None:
            raise missing("backtest", bid)
        return b

    @app.get("/api/providers/{pubkey}/export")
    async def export(pubkey: str, format: str = "bundle") -> Response:
        if format not in ("bundle", "csv"):
            raise MarketError(
                400,
                f"format {format[:20]!r} is not bundle or csv; pass format=bundle or format=csv",
            )
        out = await svc.export(pubkey, format)
        if out is None:
            raise missing("provider", pubkey)
        body, media_type, name = out
        return Response(
            body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # ---------------------------------------------------------------- market chart (F7), read-only

    @app.exception_handler(MarketError)
    async def _market_error(_: Request, exc: MarketError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s else None
        return JSONResponse({"error": str(exc)}, status_code=exc.status, headers=headers)

    def guard(request: Request) -> None:
        mkt.limiter.check(request.client.host if request.client else "")

    def cached(response: FResponse, ttl_s: float) -> None:
        response.headers["Cache-Control"] = (
            f"public, max-age={int(ttl_s)}" if ttl_s >= 1 else "no-cache"
        )

    @app.get("/api/market/symbols", response_model=list[m.MarketSymbol])
    async def market_symbols(
        request: Request, response: FResponse, q: str = "", limit: str | None = None
    ) -> list[m.MarketSymbol]:
        guard(request)
        out = await asyncio.to_thread(mkt.symbols, q, limit)
        cached(response, 60)
        return out

    @app.get("/api/market/candles", response_model=list[m.MarketCandle])
    async def market_candles(
        request: Request, response: FResponse, instrument: str | None = None, tf: str | None = None,
        end_ms: str | None = None, limit: str | None = None,
    ) -> list[m.MarketCandle]:  # fmt: skip
        guard(request)
        out, ttl = await asyncio.to_thread(mkt.candles, instrument, tf, end_ms, limit)
        cached(response, ttl)
        return out

    @app.get("/api/market/indicators", response_model=dict[str, list[m.ValuePoint | m.BandPoint]])
    async def market_indicators(
        request: Request, response: FResponse, instrument: str | None = None, tf: str | None = None,
        end_ms: str | None = None, limit: str | None = None, spec: str | None = None,
    ) -> dict[str, list[m.ValuePoint | m.BandPoint]]:  # fmt: skip
        guard(request)
        out, ttl = await asyncio.to_thread(mkt.indicators, instrument, tf, end_ms, limit, spec)
        cached(response, ttl)
        return out

    @app.get("/api/me", response_model=m.Me)
    async def me(request: Request) -> m.Me:
        session = siwe.session(request.cookies.get(SESSION_COOKIE))
        return await svc.me(session.address if session else None)

    @app.get("/api/me/signals", response_model=list[m.FollowerSignal])
    async def me_signals(
        request: Request, limit: Annotated[int, Query(ge=1, le=1000)] = 50
    ) -> list[m.FollowerSignal]:
        session = siwe.session(request.cookies.get(SESSION_COOKIE))
        return await svc.my_signals(limit, session.address if session else None)

    @app.get("/api/governance/proposals", response_model=list[m.Proposal])
    async def proposals() -> list[m.Proposal]:
        return await svc.proposals()

    @app.get("/api/network", response_model=m.Network)
    async def network() -> m.Network:
        return await svc.network()

    # ---------------------------------------------------------------- Sign-In with Ethereum (EIP-4361)

    @app.exception_handler(SiweError)
    async def _siwe_error(_: Request, exc: SiweError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def body[T: BaseModel](request: Request, model: type[T]) -> T:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise SiweError(
                415,
                f"content-type {ctype or 'missing'!r} is not application/json; send a JSON body",
            )
        try:
            return model.model_validate_json(await request.body())
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "body"
            raise SiweError(
                400,
                f"bad request at {where}: {first['msg']}; send {list(model.model_fields)} as JSON",
            ) from exc

    @app.post("/api/auth/nonce", response_model=m.NonceResponse)
    async def auth_nonce(request: Request) -> m.NonceResponse:
        req = await body(request, m.NonceRequest)
        nonce, message = siwe.nonce(
            req.address,
            request.headers.get("host", ""),
            request.url.scheme,
            request.client.host if request.client else "",
        )
        return m.NonceResponse(nonce=nonce, message=message)

    @app.post("/api/auth/verify", response_model=m.AddressResponse)
    async def auth_verify(request: Request) -> JSONResponse:
        req = await body(request, m.VerifyRequest)
        # Secure follows the scheme the app sees: behind a TLS-terminating proxy that needs --proxy-headers
        # (X-Forwarded-Proto); a configured https siwe_uri also forces it, whatever the proxy forwards.
        secure_cookie = request.url.scheme == "https" or (cfg.siwe_uri or "").startswith("https://")
        address, token, max_age = siwe.verify(
            req.message, req.signature, request.headers.get("host", "")
        )
        res = JSONResponse({"address": address})
        res.set_cookie(
            SESSION_COOKIE, token, max_age=max_age, path="/", httponly=True, samesite="strict",
            secure=secure_cookie,
        )  # fmt: skip
        return res

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        """Needs a valid session: with none there is nothing to end, and a state-changing route never answers 2xx unauthenticated."""
        token = request.cookies.get(SESSION_COOKIE)
        if not siwe.logout(token):
            res = JSONResponse(
                {"error": "no valid session; sign in first (POST /api/auth/nonce)"}, status_code=401
            )
        else:
            res = JSONResponse({})
        res.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return res

    # ---------------------------------------------------------------- strategy designer (F4)

    async def json_body(request: Request, limit: int) -> bytes:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise MarketError(
                415,
                f"content-type {ctype or 'missing'!r} is not application/json; send a JSON body",
            )
        declared = request.headers.get("content-length", "0")
        if declared.isdigit() and int(declared) > limit:
            raise MarketError(
                413,
                f"body of {declared} bytes is over the {limit}-byte limit; send only the strategy",
            )
        raw = await request.body()
        if len(raw) > limit:
            raise MarketError(
                413,
                f"body of {len(raw)} bytes is over the {limit}-byte limit; send only the strategy",
            )
        return raw

    @app.post("/api/strategy/validate", response_model=m.ValidateResponse)
    async def strategy_validate(request: Request) -> m.ValidateResponse:
        """Pure: parses and checks the posted strategy document and answers; reads and writes nothing."""
        guard(request)
        raw = await json_body(request, MAX_STRATEGY_BYTES)
        parsed, issues = validate_issues(raw)
        return m.ValidateResponse(
            ok=parsed is not None,
            errors=[
                m.ValidationIssue(path=i.path, column=i.column, message=i.message) for i in issues
            ],
        )

    def require_local(request: Request) -> None:
        """Backtests burn CPU and disk: only for a user at this machine. 403 unless the server is bound to loopback,
        no proxy sits in front, the request names a loopback host, and (when sent) its Origin is this very host."""
        host = request.headers.get("host")
        if not is_loopback_host(bind_host):
            why = f"the API is bound to {bind_host or 'an unknown address'}, not loopback; start it with --host 127.0.0.1"
        elif cfg.proxy_headers or any(
            h in request.headers for h in ("x-forwarded-for", "forwarded")
        ):
            why = "requests that come through a proxy are refused; run backtests from the machine itself"
        elif not request_host_is_loopback(host):
            why = f"Host {host!r} is not a loopback name; open the dashboard at http://127.0.0.1 or http://localhost"
        elif (origin := request.headers.get("origin")) is not None and origin.partition("://")[
            2
        ] != host:
            why = f"Origin {origin[:60]!r} is not the page's own host; cross-site requests are refused"
        else:
            return
        raise MarketError(403, f"backtests are allowed only on a loopback-bound API: {why}")

    @app.post("/api/backtests/run", response_model=m.JobStatus, status_code=202)
    async def backtests_run(request: Request) -> m.JobStatus:
        require_local(request)
        guard(request)
        raw = await json_body(request, MAX_STRATEGY_BYTES + 4096)
        try:
            data = m.BacktestRunBody.model_validate_json(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "body"
            raise MarketError(
                400,
                f"bad request at {where}: {first['msg']}; send {{strategy, instrument, timeframe?, start, end}} as a JSON object",
            ) from exc
        try:
            job = runner.submit(parse_request(data.model_dump(exclude_unset=True), cfg))
        except RequestInvalid as exc:
            return JSONResponse(  # type: ignore[return-value]
                {
                    "error": f"invalid backtest request: {exc}",
                    "errors": [i.model_dump() for i in exc.issues],
                },
                status_code=422,
            )
        except JobBusy as exc:
            return JSONResponse({"error": str(exc), "job": exc.job_id}, status_code=409)  # type: ignore[return-value]
        return _status(job)

    @app.get("/api/backtests/jobs/{job_id}", response_model=m.JobStatus)
    async def backtest_job(job_id: str) -> m.JobStatus:
        job = runner.get(job_id)
        if job is None:
            raise missing("backtest job", job_id)
        return _status(job)

    @app.get("/api/{rest:path}", include_in_schema=False)
    async def unknown(rest: str) -> JSONResponse:
        return JSONResponse({"error": f"route not found: /api/{rest}"}, status_code=404)

    if cfg.web_dist is not None:
        dist = Path(cfg.web_dist)
        if not (dist / "index.html").is_file():
            raise ConfigError(
                f"web_dist {dist} has no index.html; build the dashboard first: cd web && bun install && bun run build"
            )
        cache: tuple[int, str | None] = (-1, None)

        @app.middleware("http")
        async def _build_header(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            """Tell open dashboard tabs which build is served now, so a stale one can reload itself."""
            nonlocal cache
            resp = await call_next(request)
            try:
                mtime = (dist / "index.html").stat().st_mtime_ns
            except OSError:
                return resp
            if cache[0] != mtime:
                cache = (mtime, web_build_id(dist))
            if cache[1]:
                resp.headers["X-SA-Build"] = cache[1]
            return resp

        app.mount("/", WebFiles(directory=dist, html=True), name="web")
    return app
