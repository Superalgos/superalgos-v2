"""Read-only market data for the chart (F7): Binance spot symbols and candles, and indicator series.

- Candles come from the local Nautilus catalog (``<data_dir>/catalog``, 1m bars aggregated to the timeframe) when every
  month the window touches is in it and the data-dir lock is free; otherwise from Binance's public klines (no key).
- Indicators are computed with ``sa_engine.indicators.make_indicator``, the very constructor ``IndicatorBank`` uses for
  strategies, from a spec parsed by ``sa_engine.dsl``: there is no second implementation.

Everything is bounded: symbol results, candles per request, spec length and count, cache sizes, requests per IP.
"""

from __future__ import annotations

import gc
import logging
import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from sa_api import models as m
from sa_api.config import ApiConfig
from sa_engine import data, dsl
from sa_engine.indicators import OHLCV, make_indicator
from sa_engine.intent import to_nautilus_id

log = logging.getLogger(__name__)

MINUTE_MS: Final = 60_000
TF_MS: Final[dict[str, int]] = {
    "1m": MINUTE_MS,
    "5m": 5 * MINUTE_MS,
    "15m": 15 * MINUTE_MS,
    "1h": 60 * MINUTE_MS,
    "4h": 240 * MINUTE_MS,
    "1d": 1440 * MINUTE_MS,
}
MAX_LIMIT: Final = 1000
DEFAULT_LIMIT: Final = 500
DEFAULT_SYMBOLS: Final = 50
MAX_SYMBOLS: Final = 100
MAX_QUERY_LEN: Final = 20
MAX_SPEC_LEN: Final = 200
MAX_SPEC_ITEMS: Final = 8
LOCAL_MAX_MINUTES: Final = 300_000  # ~208 days of 1m bars: more is cheaper to ask Binance for
HISTORY_TTL_S: Final = 600.0
CACHE_ENTRIES: Final = 256
MAX_RATE_KEYS: Final = 10_000

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_QUERY_RE = re.compile(r"^[A-Za-z0-9/_:\-]*$")
_NUM_RE = re.compile(r"^\d+(\.\d+)?$")


class MarketError(Exception):
    """A market request that cannot be served. ``status`` is the HTTP code; the message says what to change or retry."""

    def __init__(self, status: int, message: str, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


class CatalogUnreadable(Exception):
    """The local catalog could not be read; the message has the underlying error."""


@dataclass(frozen=True, slots=True)
class Candle:
    t: int  # open time, epoch ms
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


@dataclass(frozen=True, slots=True)
class Symbol:
    symbol: str
    base: str
    quote: str


# ---------------------------------------------------------------- small utilities


class RateLimiter:
    """At most ``per_min`` calls per client key in any 60 s (sliding window); thread-safe and bounded."""

    def __init__(self, per_min: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_min = per_min
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._hits) >= MAX_RATE_KEYS and client not in self._hits:
                for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - 60]:
                    del self._hits[k]
                if len(self._hits) >= MAX_RATE_KEYS:
                    raise MarketError(
                        429,
                        "too many clients are using the market endpoints; retry in a minute",
                        60,
                    )
            q = self._hits.setdefault(client, deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.per_min:
                wait = max(1, int(q[0] + 60 - now) + 1)
                raise MarketError(
                    429,
                    f"{client or 'this client'} made more than {self.per_min} market requests in a minute; "
                    f"wait {wait} s and retry",
                    wait,
                )
            q.append(now)


class TtlCache[V]:
    """Small thread-safe cache with per-entry expiry and a hard size bound (oldest entry out first)."""

    def __init__(self, size: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.size = size
        self._clock = clock
        self._items: OrderedDict[Any, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> V | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            if hit[0] <= self._clock():
                del self._items[key]
                return None
            return hit[1]

    def put(self, key: Any, value: V, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = (self._clock() + ttl_s, value)
            while len(self._items) > self.size:
                self._items.popitem(last=False)


# ---------------------------------------------------------------- parameters


def parse_instrument(raw: str | None) -> str:
    """``BTCUSDT`` or ``BINANCE:BTCUSDT`` to the Binance symbol."""
    if not raw:
        raise MarketError(
            400,
            "instrument is required, e.g. instrument=BINANCE:BTCUSDT; search /api/market/symbols?q=btc",
        )
    sym = raw.strip().upper().removeprefix("BINANCE:")
    if not _SYMBOL_RE.match(sym):
        raise MarketError(
            400,
            f"instrument {raw[:40]!r} is not a Binance spot symbol such as BTCUSDT or BINANCE:BTCUSDT; "
            "search /api/market/symbols?q=btc",
        )
    return sym


def parse_tf(raw: str | None) -> str:
    if raw not in TF_MS:
        raise MarketError(
            400, f"tf {str(raw)[:20]!r} is not one of {', '.join(TF_MS)}; pass e.g. tf=1h"
        )
    return str(raw)


def parse_limit(raw: str | None, default: int, maximum: int, name: str = "limit") -> int:
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except ValueError:
        raise MarketError(
            400, f"{name} {raw[:20]!r} is not an integer; pass 1..{maximum}"
        ) from None
    if not 1 <= n <= maximum:
        raise MarketError(400, f"{name}={n} is out of range; pass 1..{maximum}")
    return n


def parse_end_ms(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        raise MarketError(
            400, f"end_ms {raw[:20]!r} is not an integer; pass epoch milliseconds"
        ) from None
    if n < 0:
        raise MarketError(400, f"end_ms={n} is negative; pass epoch milliseconds")
    return n


# ---------------------------------------------------------------- aggregation (local catalog)


def aggregate(minutes: list[Candle], tf_ms: int) -> list[Candle]:
    """1m candles to ``tf_ms`` candles aligned to UTC (the grid Binance uses). Volume is summed exactly (fsum) and
    rounded to Binance's 8 decimals."""
    out: list[Candle] = []
    bucket: list[Candle] = []
    key = -1

    def flush() -> None:
        if bucket:
            out.append(
                Candle(
                    t=key,
                    o=bucket[0].o,
                    h=max(c.h for c in bucket),
                    l=min(c.l for c in bucket),
                    c=bucket[-1].c,
                    v=round(math.fsum(c.v for c in bucket), 8),
                )
            )

    for c in minutes:
        k = c.t // tf_ms * tf_ms
        if k != key:
            flush()
            bucket, key = [], k
        bucket.append(c)
    flush()
    return out


def window(end_ms: int, tf_ms: int, limit: int) -> tuple[int, int]:
    """(open time of the first candle, open time of the last candle) for the ``limit`` candles opening at or before ``end_ms``."""
    last = end_ms // tf_ms * tf_ms
    return max(0, last - (limit - 1) * tf_ms), last


# ---------------------------------------------------------------- indicator specs

_SINGLE = {"ema": "ema", "sma": "sma", "rsi": "rsi"}


@dataclass(frozen=True, slots=True)
class SpecItem:
    key: str  # the item as the client wrote it, without spaces: the response key
    shape: str  # "value" | "band"
    parts: tuple[tuple[str, dsl.IndicatorSpec], ...]  # (field name, engine indicator)


def split_spec(spec: str) -> list[str]:
    """Split at commas outside parentheses: ``ema(20),bbands(20,2)`` has two items."""
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in spec:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise MarketError(
                    400, f"spec {spec[:60]!r} has an unmatched ')'; check the parentheses"
                )
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if depth != 0:
        raise MarketError(400, f"spec {spec[:60]!r} has an unmatched '('; check the parentheses")
    items.append("".join(cur))
    return items


def _engine_spec(text: str, item: str) -> dsl.IndicatorSpec:
    """Parse one indicator call with the engine's DSL parser (which also range-checks the arguments)."""
    try:
        node = dsl.parse(text)
    except dsl.DslError as exc:
        raise MarketError(400, f"spec item {item!r}: {exc}") from exc
    spec = dsl.indicator_spec(node) if isinstance(node, dsl.Call) else None
    if spec is None:  # pragma: no cover - the names are fixed above
        raise MarketError(400, f"spec item {item!r} is not an indicator")
    return spec


def parse_spec(spec: str | None) -> list[SpecItem]:
    """``ema(20),rsi(14),bbands(20,2)`` to engine indicator specs. Also: ``sma(n)``, ``atr(n)``, ``donchian(n)``; a
    price source may lead ``ema``/``sma``/``rsi``/``bbands`` (``ema(close,20)``, default close)."""
    usage = "e.g. spec=ema(20),sma(50),rsi(14),bbands(20,2),atr(14),donchian(20)"
    if not spec or not spec.strip():
        raise MarketError(400, f"spec is required, {usage}")
    if len(spec) > MAX_SPEC_LEN:
        raise MarketError(
            400, f"spec is {len(spec)} characters; at most {MAX_SPEC_LEN} are allowed"
        )
    out: list[SpecItem] = []
    seen: set[str] = set()
    for raw in split_spec(spec):
        item = "".join(raw.split())
        if item in seen:
            continue
        seen.add(item)
        match = re.fullmatch(r"([a-z]+)\(([^()]*)\)", item)
        if match is None:
            raise MarketError(400, f"spec item {item[:40]!r} is not name(args); {usage}")
        name, args = match.group(1), [a for a in match.group(2).split(",") if a != ""]
        src = "close"
        if name in ("ema", "sma", "rsi", "bbands") and args and args[0] in dsl.SERIES_NAMES:
            src, args = args[0], args[1:]
        for a in args:
            if not _NUM_RE.match(a):
                raise MarketError(
                    400, f"spec item {item!r}: argument {a[:20]!r} is not a number; {usage}"
                )
        if name in _SINGLE and len(args) == 1:
            ind = _engine_spec(f"{_SINGLE[name]}({src}, {args[0]})", item)
            out.append(SpecItem(item, "value", (("value", ind),)))
        elif name == "bbands" and len(args) in (1, 2):
            k = args[1] if len(args) == 2 else "2"
            parts = tuple(
                (field, _engine_spec(f"bb_{kind}({src}, {args[0]}, {k})", item))
                for field, kind in (("upper", "upper"), ("mid", "middle"), ("lower", "lower"))
            )
            out.append(SpecItem(item, "band", parts))
        elif name == "atr" and len(args) == 1:
            out.append(SpecItem(item, "value", (("value", _engine_spec(f"atr({args[0]})", item)),)))
        elif name == "donchian" and len(args) == 1:
            parts = tuple(
                (field, _engine_spec(f"donchian_{kind}({args[0]})", item))
                for field, kind in (("upper", "upper"), ("mid", "mid"), ("lower", "lower"))
            )
            out.append(SpecItem(item, "band", parts))
        else:
            raise MarketError(
                400,
                f"spec item {item!r} is not a known indicator with the right arguments "
                "(ema/sma/rsi/atr/donchian take a period, bbands a period and optional width); "
                + usage,
            )
        if len(out) > MAX_SPEC_ITEMS:
            raise MarketError(400, f"spec has more than {MAX_SPEC_ITEMS} indicators; ask for fewer")
    return out


def series(indicator: dsl.IndicatorSpec, candles: list[Candle], tf_ms: int) -> list[float | None]:
    """The engine's streaming indicator over ``candles``: one value per candle, ``None`` while warming up.
    ``make_indicator`` is what ``IndicatorBank`` runs inside backtests and paper trading."""
    ind = make_indicator(indicator)
    return [
        ind.update(OHLCV(c.t + tf_ms, c.o, c.h, c.l, c.c, c.v)) for c in candles
    ]  # ts is the bar close, as in the engine


# ---------------------------------------------------------------- the service


class MarketService:
    def __init__(
        self,
        cfg: ApiConfig,
        *,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self._http = http or httpx.Client(timeout=httpx.Timeout(10.0))
        self._clock = clock
        self._symbols: tuple[float, list[Symbol]] | None = None
        self._symbols_lock = threading.Lock()
        self._candles = TtlCache[list[Candle]](CACHE_ENTRIES)
        self._reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalog-reader")
        self.limiter = RateLimiter(cfg.market_rate_per_min)

    # ------------------------------------------------------------ Binance

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = self.cfg.binance_base_url.rstrip("/") + path
        try:
            resp = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise MarketError(
                502, f"cannot reach Binance at {url}: {exc}; check the network and retry"
            ) from exc
        if resp.status_code in (418, 429):
            raise MarketError(
                503,
                f"Binance is rate-limiting this server (HTTP {resp.status_code}); retry in a minute",
                60,
            )
        if resp.status_code == 400:
            raise MarketError(
                404,
                f"Binance rejected {params.get('symbol', 'the request')!r}: {_binance_msg(resp)}",
            )
        if resp.status_code >= 400:
            raise MarketError(
                502, f"Binance answered HTTP {resp.status_code} for {path}; retry shortly"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise MarketError(
                502, f"Binance answered something that is not JSON for {path}; retry shortly"
            ) from exc

    # ------------------------------------------------------------ symbols

    def _load_symbols(self) -> list[Symbol]:
        now = self._clock()
        cached = self._symbols
        if cached is not None and now - cached[0] < self.cfg.market_symbols_ttl_s:
            return cached[1]
        with self._symbols_lock:
            cached = self._symbols
            if cached is not None and self._clock() - cached[0] < self.cfg.market_symbols_ttl_s:
                return cached[1]
            try:
                info = self._get("/api/v3/exchangeInfo", {"permissions": "SPOT"})
                rows = [
                    Symbol(s["symbol"], s["baseAsset"], s["quoteAsset"])
                    for s in info["symbols"]
                    if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)
                ]
            except MarketError:
                if cached is not None:  # a stale list beats no list
                    log.warning(
                        "exchangeInfo refresh failed; serving the list from %.0f s ago",
                        now - cached[0],
                    )
                    return cached[1]
                raise
            except (KeyError, TypeError) as exc:
                raise MarketError(
                    502, f"Binance exchangeInfo has an unexpected shape ({exc!r}); retry later"
                ) from exc
            self._symbols = (self._clock(), rows)
            return rows

    def symbols(self, q: str | None, limit: str | None = None) -> list[m.MarketSymbol]:
        needle = (q or "").strip()
        if len(needle) > MAX_QUERY_LEN or not _QUERY_RE.match(needle):
            raise MarketError(
                400,
                f"q must be at most {MAX_QUERY_LEN} letters, digits, '/', '-', '_' or ':'; got {needle[:30]!r}",
            )
        n = parse_limit(limit, DEFAULT_SYMBOLS, MAX_SYMBOLS)
        key = re.sub(r"[/_\-]", "", needle.upper().removeprefix("BINANCE:"))
        ranked: list[tuple[int, int, str, Symbol]] = []
        for s in self._load_symbols():
            if not key:
                rank = 3
            elif s.symbol == key:
                rank = 0
            elif s.symbol.startswith(key):
                rank = 1
            elif s.base.startswith(key):
                rank = 2
            elif key in s.symbol:
                rank = 3
            else:
                continue
            ranked.append((rank, 0 if s.quote == "USDT" else 1, s.symbol, s))
        ranked.sort(key=lambda r: r[:3])
        return [
            m.MarketSymbol(instrument=s.symbol, base=s.base, quote=s.quote) for *_, s in ranked[:n]
        ]

    # ------------------------------------------------------------ candles

    def _read_minutes(
        self, catalog_dir: Path, symbol: str, t0: datetime, t1: datetime
    ) -> list[Candle]:
        """1m candles of [t0, t1) from the catalog, under the data-dir lock (waits at most ``market_lock_wait_s``).
        Runs on the reader thread, where Nautilus' query session must also die: a failure is re-raised as a plain
        ``CatalogUnreadable`` whose traceback holds no session."""
        problem = ""
        try:
            return self._load_minutes(catalog_dir, symbol, t0, t1)
        except data.LockTimeout:
            raise
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"
        finally:
            gc.collect()  # the query session (and the frames of a failure) are released here, on this thread
        raise CatalogUnreadable(problem)

    def _load_minutes(
        self, catalog_dir: Path, symbol: str, t0: datetime, t1: datetime
    ) -> list[Candle]:
        from nautilus_trader.persistence.catalog import (  # noqa: PLC0415 - heavy import, local path only
            ParquetDataCatalog,
        )

        from sa_engine import catalog as cat  # noqa: PLC0415

        with data.file_lock(catalog_dir / ".lock", timeout_s=self.cfg.market_lock_wait_s):
            bars = cat.load_bars(
                ParquetDataCatalog(catalog_dir), to_nautilus_id(f"BINANCE:{symbol}"), t0, t1
            )
            return [
                Candle(
                    t=b.ts_event // 1_000_000 - MINUTE_MS,
                    o=float(b.open.as_decimal()),
                    h=float(b.high.as_decimal()),
                    l=float(b.low.as_decimal()),
                    c=float(b.close.as_decimal()),
                    v=float(b.volume.as_decimal()),
                )
                for b in bars
            ]

    def _local(self, symbol: str, tf: str, first: int, last: int) -> list[Candle] | None:
        """Aggregate 1m bars of the local catalog for [first, last + tf). ``None``: the catalog cannot answer this window
        (not there, months missing, too large, locked by a download/conversion, unreadable): ask Binance."""
        tf_ms = TF_MS[tf]
        end = last + tf_ms
        if end > int(self._clock() * 1000) or (end - first) // MINUTE_MS > LOCAL_MAX_MINUTES:
            return None
        catalog_dir = (self.cfg.data_dir or data.default_data_dir()) / "catalog"
        marks = catalog_dir / ".converted"
        if not marks.is_dir():
            return None
        t0, t1 = (datetime.fromtimestamp(x / 1000, UTC) for x in (first, end))
        months = data.month_range(t0.date(), datetime.fromtimestamp((end - 1) / 1000, UTC).date())
        if not all((marks / f"{symbol}-{mo}").exists() for mo in months):
            return None
        try:
            # Nautilus' query session may only be dropped on the thread that made it: one dedicated reader thread
            minutes = self._reader.submit(self._read_minutes, catalog_dir, symbol, t0, t1).result()
        except data.LockTimeout as exc:
            log.info("catalog busy, using Binance for %s: %s", symbol, exc)
            return None
        except CatalogUnreadable as exc:  # a broken catalog must not take the chart down
            log.warning("cannot read the local catalog for %s (%s); using Binance", symbol, exc)
            return None
        if not minutes:
            return None
        return aggregate(minutes, tf_ms)[-(1 + (last - first) // tf_ms) :]

    def _remote(self, symbol: str, tf: str, end_ms: int | None, n: int) -> list[Candle]:
        """The ``n`` newest candles opening at or before ``end_ms`` (now if None), paging back 1000 at a time."""
        out: list[Candle] = []
        cursor = end_ms
        while len(out) < n:
            want = min(MAX_LIMIT, n - len(out))
            params: dict[str, Any] = {"symbol": symbol, "interval": tf, "limit": want}
            if cursor is not None:
                params["endTime"] = cursor
            rows = self._get("/api/v3/klines", params)
            try:
                page = [
                    Candle(
                        int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
                    )
                    for r in rows
                ]
            except (IndexError, TypeError, ValueError) as exc:
                raise MarketError(
                    502,
                    f"Binance klines for {symbol} have an unexpected shape ({exc!r}); retry later",
                ) from exc
            out = page + out
            if len(page) < want:
                break  # the listing starts here
            cursor = page[0].t - 1
        return out

    def _candles_of(
        self, symbol: str, tf: str, end: int | None, n: int
    ) -> tuple[list[Candle], float]:
        """(the ``n`` newest candles opening at or before ``end``, seconds the answer may be cached)."""
        tf_ms = TF_MS[tf]
        now_ms = int(self._clock() * 1000)
        closed = end is not None and (end // tf_ms + 1) * tf_ms <= now_ms
        ttl = HISTORY_TTL_S if closed else self.cfg.market_candle_ttl_s
        key = (symbol, tf, end, n)
        hit = self._candles.get(key)
        if hit is not None:
            return hit, ttl
        candles: list[Candle] | None = None
        if end is not None:
            first, last = window(end, tf_ms, n)
            candles = self._local(symbol, tf, first, last)
        if candles is None:
            candles = self._remote(symbol, tf, end, n)
        self._candles.put(key, candles, ttl)
        return candles, ttl

    def candles(
        self, instrument: str | None, tf: str | None, end_ms: str | None, limit: str | None
    ) -> tuple[list[m.MarketCandle], float]:
        symbol, tfk = parse_instrument(instrument), parse_tf(tf)
        end, n = parse_end_ms(end_ms), parse_limit(limit, DEFAULT_LIMIT, MAX_LIMIT)
        found, ttl = self._candles_of(symbol, tfk, end, n)
        return [m.MarketCandle(t=c.t, o=c.o, h=c.h, l=c.l, c=c.c, v=c.v) for c in found], ttl

    # ------------------------------------------------------------ indicators

    def indicators(
        self,
        instrument: str | None,
        tf: str | None,
        end_ms: str | None,
        limit: str | None,
        spec: str | None,
    ) -> tuple[dict[str, list[m.ValuePoint | m.BandPoint]], float]:
        """One point per window candle once the indicator has a value. The engine's indicators run over the window
        preceded by ``warmup`` candles, so the first window point already has one; ``warmup`` candles are not returned."""
        items = parse_spec(spec)  # validate the cheap things first
        symbol, tfk = parse_instrument(instrument), parse_tf(tf)
        end, n = parse_end_ms(end_ms), parse_limit(limit, DEFAULT_LIMIT, MAX_LIMIT)
        warm = warmup(items)
        found, ttl = self._candles_of(symbol, tfk, end, n + warm)
        tf_ms = TF_MS[tfk]
        first = max(0, len(found) - n)  # index of the first window candle
        out: dict[str, list[m.ValuePoint | m.BandPoint]] = {}
        for item in items:
            cols = {field: series(ind, found, tf_ms) for field, ind in item.parts}
            pts: list[m.ValuePoint | m.BandPoint] = []
            for i in range(first, len(found)):
                vals = {f: col[i] for f, col in cols.items()}
                if any(v is None for v in vals.values()):
                    continue  # still warming up (not enough history before the window)
                t = found[i].t
                if item.shape == "value":
                    pts.append(m.ValuePoint(t=t, value=_f(vals["value"])))
                else:
                    pts.append(
                        m.BandPoint(
                            t=t,
                            upper=_f(vals["upper"]),
                            mid=_f(vals["mid"]),
                            lower=_f(vals["lower"]),
                        )
                    )
            out[item.key] = pts
        return out, ttl


def warmup(items: list[SpecItem]) -> int:
    """Candles to compute before the window: enough for every indicator to have a value on the first window candle
    (RSI needs period + 1 prices), and three periods where affordable so the Wilder/EMA smoothing has settled."""
    p = max(ind.period for it in items for _, ind in it.parts)
    return max(p + 1, min(3 * p, MAX_LIMIT))


def _f(v: float | None) -> float:
    if v is None:  # pragma: no cover - callers skip warm-up rows
        raise ValueError("indicator value is None; this is a bug in sa_api.market")
    return v


def _binance_msg(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("msg", resp.text))[:120]
    except (ValueError, AttributeError):
        return resp.text[:120]
