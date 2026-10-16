"""Replayed history at a configurable speed (demos and tests).

A replay maps historical market time onto the wall clock: ``wall = wall0 + (hist - hist0) / speed``.
Signals are created at wall-clock times (the relay rejects a ``created_ms`` far from its own clock), so
everything that compares signal times with candles (followers' fills, the track record) uses candles
mapped through the same :class:`ReplayClock`.

Known distortion, by design: time spans that are real seconds in a protocol (anchor latency of at least
15 s, a signal's ``timeframe`` window for limit and stop fills) are ``speed`` times longer in market time
than in live operation. A replayed record shows that the machinery works, not what a live record would be.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sa_core.trackrecord import Candle

from sa_engine.data import (
    DataError,
    Fetch,
    default_data_dir,
    download_month,
    http_fetch,
    month_range,
    read_month,
    utc_ms,
)
from sa_engine.intent import OrderIntent, normalize_instrument
from sa_engine.schema import StrategyDoc

MINUTE_MS = 60_000
MAX_SPEED = 30_000.0  # above this two 1m candles would land on the same wall millisecond


class ReplayError(Exception):
    """A replay specification or its data is unusable. The message says what to change."""


class ReplayClock(BaseModel):
    """Historical time (ms since epoch) <-> wall time (ms since epoch)."""

    model_config = ConfigDict(frozen=True)

    hist0_ms: int
    wall0_ms: int
    speed: float = Field(gt=0, le=MAX_SPEED)

    def to_wall(self, hist_ms: int) -> int:
        return self.wall0_ms + round((hist_ms - self.hist0_ms) / self.speed)

    def to_hist(self, wall_ms: int) -> int:
        return self.hist0_ms + round((wall_ms - self.wall0_ms) * self.speed)


class ReplaySpec(BaseModel):
    """What was replayed and when it started on the wall clock; written next to a provider's state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = 1
    instrument: str
    start: str  # YYYY-MM-DD, inclusive
    end: str  # YYYY-MM-DD, exclusive
    speed: float = Field(gt=0, le=MAX_SPEED)
    wall0_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _order(self) -> ReplaySpec:
        if date.fromisoformat(self.end) <= date.fromisoformat(self.start):
            raise ValueError(f"replay end {self.end} must be after start {self.start}")
        return self

    @property
    def clock(self) -> ReplayClock:
        return ReplayClock(hist0_ms=utc_ms(date.fromisoformat(self.start)), wall0_ms=self.wall0_ms, speed=self.speed)

    @property
    def symbol(self) -> str:
        return normalize_instrument(self.instrument).partition(":")[2]

    def tail_days(self, tolerance_s: int = 60) -> int:
        """Days of candles to load after ``end``: a signal may be anchored up to ``tolerance_s`` (plus a minute of
        stamping) of wall time late, which is ``speed`` times as long in market time, and its fill needs a price."""
        return math.ceil((tolerance_s + 60) * self.speed / 86_400) + 1

    def wall_duration_s(self) -> float:
        return (utc_ms(date.fromisoformat(self.end)) - utc_ms(date.fromisoformat(self.start))) / 1000 / self.speed

    def save(self, path: Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ReplaySpec:
        try:
            return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReplayError(
                f"replay spec {path} not found; `sa provider run --replay` writes it into its state directory"
            ) from exc
        except ValueError as exc:
            raise ReplayError(f"{path} is not a replay spec: {exc}") from exc


def load_candles(
    symbol: str,
    start: date,
    end: date,
    *,
    data_dir: Path | None = None,
    fetch: Fetch = http_fetch,
) -> list[Candle]:
    """Real 1m klines in [start, end) from the verified monthly archives (downloaded on first use)."""
    data_dir = data_dir or default_data_dir()
    lo, hi = utc_ms(start), utc_ms(end)
    out: list[Candle] = []
    for month in month_range(start, end - timedelta(days=1)):
        frame = read_month(download_month(symbol, month, data_dir, fetch))
        index = pd.DatetimeIndex(frame.index)
        close_ms = (index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)
        opens = [int(v) - MINUTE_MS for v in close_ms]
        cols = {k: frame[k].tolist() for k in ("open", "high", "low", "close", "volume")}
        for i, open_ms in enumerate(opens):
            if lo <= open_ms < hi:
                out.append(
                    Candle(
                        open_time_ms=open_ms,
                        open=Decimal(repr(cols["open"][i])),
                        high=Decimal(repr(cols["high"][i])),
                        low=Decimal(repr(cols["low"][i])),
                        close=Decimal(repr(cols["close"][i])),
                        volume=Decimal(repr(cols["volume"][i])),
                    )
                )
    if not out:
        raise ReplayError(f"no {symbol} 1m candles between {start} and {end} in the archive; check the dates")
    return out


def wall_candles(candles: Sequence[Candle], clock: ReplayClock) -> list[Candle]:
    """The same candles on the wall-clock axis (strictly ascending as long as speed <= MAX_SPEED)."""
    return [c.model_copy(update={"open_time_ms": clock.to_wall(c.open_time_ms)}) for c in candles]


class PriceFeed:
    """Candle prices addressed by wall time. Used by simulated followers (no network, deterministic)."""

    def __init__(self, candles_wall: Sequence[Candle]) -> None:
        if not candles_wall:
            raise ReplayError("a price feed needs at least one candle")
        self._c = list(candles_wall)
        self._t = [c.open_time_ms for c in self._c]

    def _index(self, wall_ms: int) -> int:
        return max(0, min(len(self._c) - 1, bisect.bisect_right(self._t, wall_ms) - 1))

    def price_at(self, wall_ms: int) -> float:
        """Open of the candle that is current at ``wall_ms`` (first candle before the replay starts)."""
        return float(self._c[self._index(wall_ms)].open)

    def range_between(self, from_ms: int, to_ms: int) -> tuple[float, float]:
        """(low, high) over the candles current between the two instants."""
        lo, hi = self._index(from_ms), self._index(max(from_ms, to_ms))
        window = self._c[lo : hi + 1]
        return float(min(c.low for c in window)), float(max(c.high for c in window))


def collect_intents(
    strategy: StrategyDoc | Mapping[str, Any] | str,
    instrument: str,
    start: date | str,
    end: date | str,
    *,
    capital: float = 10_000.0,
    data_dir: Path | None = None,
    fetch: Fetch = http_fetch,
    catalog: Any = None,
) -> list[OrderIntent]:
    """Run the strategy over [start, end) in a Nautilus backtest and return its order intents in order.

    Every order the strategy places goes through the intent hook (SEP-3.5), so this list is exactly the
    set of signals a live provider would have published. Intents are a pure function of the bars up to
    their timestamp, so computing them first and publishing them on a schedule is equivalent to
    publishing as the replay goes.
    """
    from sa_engine.backtest import run_backtest  # Nautilus is imported only when a replay really runs

    intents: list[OrderIntent] = []
    report = run_backtest(
        strategy,
        instrument,
        start,
        end,
        starting_capital=capital,
        data_dir=data_dir,
        fetch=fetch,
        catalog=catalog,
        intent_hook=intents.append,
    )
    if report.rejections:
        raise ReplayError(f"the strategy produced {len(report.rejections)} rejected intent(s): {report.rejections[:3]}")
    return intents


def fetch_rest_candles(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    client: httpx.Client | None = None,
    base_url: str = "https://api.binance.com",
) -> list[Candle]:
    """1m klines in [start_ms, end_ms) from Binance's public REST endpoint (no key), for live-mode records
    whose month is not in the archive yet. Pages of 1000 candles."""
    http = client or httpx.Client(timeout=20.0)
    out: list[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            resp = http.get(
                f"{base_url}/api/v3/klines",
                params={"symbol": symbol, "interval": "1m", "startTime": cursor, "endTime": end_ms - 1, "limit": 1000},
            )
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ReplayError(
                f"cannot fetch {symbol} klines from {base_url}: {exc}; "
                "check the network, or pass --replay-spec for a replayed record"
            ) from exc
        if not rows:
            break
        out.extend(Candle.from_kline(r) for r in rows)
        cursor = int(rows[-1][0]) + MINUTE_MS
    if not out:
        raise ReplayError(f"Binance returned no {symbol} candles between {start_ms} and {end_ms} (ms)")
    return out


def load_replay_candles(spec: ReplaySpec, *, data_dir: Path | None = None, tolerance_s: int = 60) -> list[Candle]:
    """The replay's candles on the wall-clock axis, with the tail late-anchored fills need (when the archive has it)."""
    start, end = date.fromisoformat(spec.start), date.fromisoformat(spec.end)
    try:
        raw = load_candles(spec.symbol, start, end + timedelta(days=spec.tail_days(tolerance_s)), data_dir=data_dir)
    except (ReplayError, DataError):
        raw = load_candles(spec.symbol, start, end, data_dir=data_dir)
    return wall_candles(raw, spec.clock)
