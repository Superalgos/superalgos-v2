"""Streaming, pure-Python indicators and the bank that feeds DSL evaluation.

Every indicator appends exactly one value per bar (``None`` while warming up), so a value ``n`` bars ago
is ``history[-1 - n]``. Pure Python on purpose: results are identical in backtest and live, and the
DSL can be tested without Nautilus.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Final, Protocol

from sa_engine.dsl import SERIES_NAMES, TIMEFRAME_SECONDS, IndicatorSpec

HISTORY: Final = 1100  # >= dsl.MAX_SHIFT + 1 + slack


@dataclass(frozen=True, slots=True)
class OHLCV:
    ts: int  # bar close time, ms since epoch (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


class Indicator(Protocol):
    def update(self, bar: OHLCV) -> float | None: ...


def _src(bar: OHLCV, name: str) -> float:
    return float(getattr(bar, name))


class _Sma:
    def __init__(self, src: str, period: int) -> None:
        self.src, self.n = src, period
        self.win: deque[float] = deque(maxlen=period)

    def update(self, bar: OHLCV) -> float | None:
        x = _src(bar, self.src)
        self.win.append(x)
        if len(self.win) < self.n:
            return None
        return math.fsum(self.win) / self.n


class _Ema:
    def __init__(self, src: str, period: int) -> None:
        self.src, self.n = src, period
        self.alpha = 2.0 / (period + 1)
        self.seed: list[float] = []
        self.value: float | None = None

    def update(self, bar: OHLCV) -> float | None:
        x = _src(bar, self.src)
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self.value = math.fsum(self.seed) / self.n
                self.seed = []
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


class _Rsi:
    """Wilder's RSI. First value after ``period + 1`` prices."""

    def __init__(self, src: str, period: int) -> None:
        self.src, self.n = src, period
        self.prev: float | None = None
        self.gains: list[float] = []
        self.losses: list[float] = []
        self.avg_gain = 0.0
        self.avg_loss = 0.0
        self.ready = False

    def update(self, bar: OHLCV) -> float | None:
        x = _src(bar, self.src)
        if self.prev is None:
            self.prev = x
            return None
        change = x - self.prev
        self.prev = x
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if not self.ready:
            self.gains.append(gain)
            self.losses.append(loss)
            if len(self.gains) < self.n:
                return None
            self.avg_gain = math.fsum(self.gains) / self.n
            self.avg_loss = math.fsum(self.losses) / self.n
            self.ready = True
        else:
            self.avg_gain = (self.avg_gain * (self.n - 1) + gain) / self.n
            self.avg_loss = (self.avg_loss * (self.n - 1) + loss) / self.n
        if self.avg_loss == 0.0:
            return 50.0 if self.avg_gain == 0.0 else 100.0
        rs = self.avg_gain / self.avg_loss
        return 100.0 - 100.0 / (1.0 + rs)


class _Bollinger:
    """Bollinger bands with population standard deviation. ``part`` is upper / middle / lower."""

    def __init__(self, src: str, period: int, k: float, part: str) -> None:
        self.src, self.n, self.k, self.part = src, period, k, part
        self.win: deque[float] = deque(maxlen=period)

    def update(self, bar: OHLCV) -> float | None:
        self.win.append(_src(bar, self.src))
        if len(self.win) < self.n:
            return None
        mean = math.fsum(self.win) / self.n
        if self.part == "middle":
            return mean
        var = math.fsum((v - mean) ** 2 for v in self.win) / self.n
        std = math.sqrt(var)
        return mean + self.k * std if self.part == "upper" else mean - self.k * std


class _Atr:
    """Wilder's ATR seeded with the simple mean of the first ``period`` true ranges."""

    def __init__(self, period: int) -> None:
        self.n = period
        self.prev_close: float | None = None
        self.seed: list[float] = []
        self.value: float | None = None

    def update(self, bar: OHLCV) -> float | None:
        if self.prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - self.prev_close), abs(bar.low - self.prev_close))
        self.prev_close = bar.close
        if self.value is None:
            self.seed.append(tr)
            if len(self.seed) == self.n:
                self.value = math.fsum(self.seed) / self.n
                self.seed = []
            return self.value
        self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


class _Donchian:
    """Highest high / lowest low over the last ``period`` bars including the current bar."""

    def __init__(self, period: int, part: str) -> None:
        self.n, self.part = period, part
        self.highs: deque[float] = deque(maxlen=period)
        self.lows: deque[float] = deque(maxlen=period)

    def update(self, bar: OHLCV) -> float | None:
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        if len(self.highs) < self.n:
            return None
        hi, lo = max(self.highs), min(self.lows)
        return hi if self.part == "upper" else lo if self.part == "lower" else (hi + lo) / 2.0


def make_indicator(spec: IndicatorSpec) -> Indicator:
    kind = spec.kind
    if kind == "ema":
        return _Ema(spec.src, spec.period)
    if kind == "sma":
        return _Sma(spec.src, spec.period)
    if kind == "rsi":
        return _Rsi(spec.src, spec.period)
    if kind.startswith("bb_"):
        return _Bollinger(spec.src, spec.period, spec.k, kind.removeprefix("bb_"))
    if kind == "atr":
        return _Atr(spec.period)
    if kind.startswith("donchian_"):
        return _Donchian(spec.period, kind.removeprefix("donchian_"))
    raise ValueError(f"unknown indicator kind {kind!r}; this is a bug in sa_engine.dsl")


class HigherTimeframeFeed:
    """Builds closed bars of a higher timeframe out of the strategy's own bars.

    A strategy bar covers ``(ts - base, ts]`` (``ts`` is its close, ms). A higher-timeframe window is
    ``(k * tf, (k + 1) * tf]`` and *closes* with the strategy bar whose ``ts`` is ``(k + 1) * tf``; only then
    :meth:`push` returns the aggregated bar. A window that did not start at its boundary (the series began mid-window)
    or that never saw its last bar (a data gap) is never completed from partial data: the former is dropped, the
    latter is released when the first bar of the next window arrives, which is later, never earlier, than its close.
    """

    def __init__(self, tf_ms: int, base_ms: int) -> None:
        if tf_ms < base_ms or tf_ms % base_ms:
            raise ValueError(
                f"higher timeframe of {tf_ms // 1000}s must be a whole multiple of the strategy timeframe of "
                f"{base_ms // 1000}s"
            )
        self.tf_ms, self.base_ms = tf_ms, base_ms
        self._window: int | None = None
        self._acc: OHLCV | None = None
        self._complete_start = False

    def push(self, bar: OHLCV) -> OHLCV | None:
        """Feed one strategy bar; returns a higher-timeframe bar when one has just closed."""
        window = (bar.ts - 1) // self.tf_ms
        out: OHLCV | None = None
        if self._window is not None and window != self._window:
            out = self._flush()
        if self._acc is None:
            self._window = window
            self._complete_start = bar.ts - self.base_ms == window * self.tf_ms
            self._acc = bar
        else:
            a = self._acc
            self._acc = OHLCV(
                bar.ts, a.open, max(a.high, bar.high), min(a.low, bar.low), bar.close, a.volume + bar.volume
            )
        if bar.ts % self.tf_ms == 0:
            out = self._flush() or out
        return out

    def _flush(self) -> OHLCV | None:
        acc, ok = self._acc, self._complete_start
        self._acc, self._window = None, None
        return acc if ok else None


def _ms(timeframe: str) -> int:
    return TIMEFRAME_SECONDS[timeframe] * 1000


class IndicatorBank:
    """Holds the price history and every indicator an expression set needs; implements ``EvalContext`` data.

    Everything is kept on the strategy's clock: ``value(spec, n)`` is what the expression saw ``n`` strategy bars ago.
    Higher-timeframe indicators (``spec.tf``) and prices (``close("4h")``) change only when a higher-timeframe bar
    closes and are carried forward in between, so they never include an unclosed bar.
    """

    def __init__(
        self,
        specs: set[IndicatorSpec] | frozenset[IndicatorSpec] = frozenset(),
        base_seconds: int | None = None,
        series_tfs: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        tfs = {s.tf for s in specs if s.tf} | {tf for _, tf in series_tfs}
        if tfs and base_seconds is None:
            raise ValueError(f"timeframes {sorted(tfs)} need base_seconds, the strategy timeframe in seconds")
        self._ind: dict[IndicatorSpec, Indicator] = {s: make_indicator(s) for s in sorted(specs, key=repr)}
        self._hist: dict[IndicatorSpec, deque[float | None]] = {s: deque(maxlen=HISTORY) for s in self._ind}
        self._prices: dict[str, deque[float]] = {n: deque(maxlen=HISTORY) for n in SERIES_NAMES}
        self._feeds: dict[str, HigherTimeframeFeed] = {
            tf: HigherTimeframeFeed(_ms(tf), (base_seconds or 0) * 1000) for tf in sorted(tfs)
        }
        self._htf_prices: dict[tuple[str, str], deque[float | None]] = {
            key: deque(maxlen=HISTORY) for key in sorted(series_tfs)
        }
        self._htf_last: dict[str, OHLCV | None] = dict.fromkeys(self._feeds)
        self._latest: dict[IndicatorSpec, float | None] = {}
        self.bars = 0

    def update(self, bar: OHLCV) -> None:
        for name, dq in self._prices.items():
            dq.append(_src(bar, name))
        closed: dict[str, OHLCV | None] = {tf: feed.push(bar) for tf, feed in self._feeds.items()}
        for tf, htf_bar in closed.items():
            if htf_bar is not None:
                self._htf_last[tf] = htf_bar
        for spec, ind in self._ind.items():
            if spec.tf is None:
                self._hist[spec].append(ind.update(bar))
            else:
                htf_bar = closed[spec.tf]
                if htf_bar is not None:
                    self._latest[spec] = ind.update(htf_bar)
                self._hist[spec].append(self._latest.get(spec))
        for (name, tf), hq in self._htf_prices.items():
            last = self._htf_last[tf]
            hq.append(None if last is None else _src(last, name))
        self.bars += 1

    def price(self, name: str, shift: int, tf: str | None = None) -> float | None:
        if tf is not None:
            hq = self._htf_prices.get((name, tf))
            if hq is None:
                raise KeyError(f"{name}({tf!r}) was not registered; collect it with dsl.collect_series_tfs first")
            return hq[-1 - shift] if 0 <= shift < len(hq) else None
        dq = self._prices[name]
        return dq[-1 - shift] if 0 <= shift < len(dq) else None

    def value(self, spec: IndicatorSpec, shift: int) -> float | None:
        dq = self._hist.get(spec)
        if dq is None:
            raise KeyError(f"indicator {spec} was not registered; collect it with dsl.collect_indicators first")
        return dq[-1 - shift] if 0 <= shift < len(dq) else None
