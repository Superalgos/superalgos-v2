"""Deterministic track record from a bundle and public 1-minute candles (SEP-3.7, SEP-3.16, SEP-3.28, SEP-3.14, SEP-3.12).

    compute(bundle, candles, ...) -> TrackRecord      # .to_json() is byte-identical for equal input

Rules (every exclusion and penalty is a count in the report, under `signals`):

- A chain item (public signal, or commitment with its reveal) is *on time* if some proof verifies and the
  anchored time is <= created_ms/1000 + tolerance (SEP-3.16, SEP-3.28). For a committed signal the anchor is the
  commitment's, so a reveal is on time only if its commitment predates the outcome.
- A published signal that is not on time (unanchored, anchored later than the tolerance, or anchored after its
  device delegation expired) is NOT dropped: withholding or delaying an anchor must never improve a record.
  It is replayed like any other signal, but the round trip (flat to flat) it belongs to is a *miss*: it counts as
  a loss in `win_rate` whatever it earned and contributes at most 0 to PnL (a gain is forfeited, a loss stays).
  Counts: `signals.unanchored`, `signals.late_anchor`, `signals.delegation_expired`, `signals.penalized`.
  This is the same treatment as an unrevealed commitment (SEP-3.12), which is a loss that contributes 0.
- `anchor_coverage` counts only on-time anchors.
- Fill: at the open of the first 1m candle at or after created_ms, and not before the strategy's previous fill.
  The anchored time does not move the fill, so withholding an anchor cannot change any price. Market orders fill
  there. A limit order fills at its limit price (or the open if better) in the first candle that touches it
  within `timeframe` seconds, else it is `unfilled`. A stop order (stop market) triggers in the first candle
  whose high (buy) or low (sell) reaches `stop_price` within `timeframe` and fills at the stop or the open if
  worse, else `unfilled`. No fees, no slippage, no funding.
- Exposure (M4): an `open`/`adjust` that would take the gross notional of all open positions above 1.0 of capital
  is rejected (`signals.excluded.exposure_cap`); `max_gross_exposure` is the peak and `pnl_per_exposure` is
  PnL divided by it, so stacking notional cannot inflate a percentage return.
- Unrevealed commitments are misses (SEP-3.12): they enter the denominator of win_rate as losses,
  contribute 0 to PnL (their outcome is unknowable), and are listed by event id.

Accounting is in fractions of capital with exact `Decimal` arithmetic. `size_fraction` of an
`open`/`adjust` signal is the notional added (buy +, sell -) as a fraction of capital; of a `close`
it is the fraction of the current position closed (1.0 = all). The position is netted per
(strategy, instrument): an opposite `open` reduces it and flips it if larger. A trade is a round
trip from flat to flat; it wins if its realised PnL > 0.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from pydantic import BaseModel, ConfigDict

from ._canon import canonical_json, sha256_hex
from .anchors import Anchor, anchored_time
from .bundle import BundleCheck, ChainItem, bundle_to_json, check_bundle
from .delegation import DelegationError, ensure_not_revoked
from .models import Bundle, Revocation, Signal

CANDLE_MS = 60_000
REPORT_VERSION = 1
_PLACES = Decimal("1e-12")
MAX_GROSS_EXPOSURE = Decimal(1)  # of capital; notional above this is rejected
_EXPOSURE_EPS = Decimal("1e-20")


class TrackRecordError(Exception):
    """Input to `compute` is unusable (for example unsorted candles). Says what to fix."""


class Candle(BaseModel):
    """One 1m kline. Prices are `Decimal` so the arithmetic is exact and portable."""

    model_config = ConfigDict(frozen=True)

    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)

    @classmethod
    def from_kline(cls, row: Sequence[Any]) -> Candle:
        """From a Binance kline array `[openTime, o, h, l, c, v, ...]`."""
        return cls(
            open_time_ms=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
        )


def _fmt(d: Decimal) -> str:
    q = d.quantize(_PLACES, rounding=ROUND_HALF_EVEN)
    return format(abs(q) if q == 0 else q, "f")


class Trade(BaseModel):
    """One round trip (flat to flat) on the replay of the fills; times are fill times in ms."""

    model_config = ConfigDict(frozen=True)

    open_ms: int
    close_ms: int
    side: str  # side of the entry: "buy" is long, "sell" is short
    entry: Decimal  # average entry price
    exit: Decimal  # average exit price
    pnl_pct: Decimal  # price return in the trade's direction, percent (fees and slippage: none)


class Series(BaseModel):
    """Detail behind the report for charts: not part of `to_json`, so the report bytes do not depend on it.

    `equity` holds (time_ms, equity as a fraction of capital, 1 = flat) at every candle close and fill.
    """

    model_config = ConfigDict(frozen=True)

    equity: tuple[tuple[int, Decimal], ...] = ()
    trades: tuple[Trade, ...] = ()


class TrackRecord(BaseModel):
    """The report. `report` is plain JSON-able data; `to_json` is its canonical encoding."""

    model_config = ConfigDict(frozen=True)

    report: dict[str, Any]
    series: Series = Series()

    def to_json(self) -> str:
        return canonical_json(self.report) + "\n"


# ---------------------------------------------------------------- internals


@dataclass
class _Position:
    units: Decimal = Decimal(0)  # signed: + long, - short
    avg: Decimal = Decimal(0)
    trade_pnl: Decimal = Decimal(0)
    open_trade: bool = False
    tainted: bool = False  # the open round trip contains a signal that was not on time
    # trade bookkeeping for `Series.trades`
    opened_ms: int = 0
    side: str = "buy"
    entry_qty: Decimal = Decimal(0)
    entry_notional: Decimal = Decimal(0)
    exit_qty: Decimal = Decimal(0)
    exit_notional: Decimal = Decimal(0)


def _track_trade(
    pos: _Position, before: Decimal, after: Decimal, price: Decimal, t_ms: int
) -> Trade | None:
    """Update the open trade of `pos` after a fill moved its units from `before` to `after`.

    Returns the finished `Trade` when the position went flat or flipped.
    """

    def start(units: Decimal) -> None:
        pos.opened_ms, pos.side = t_ms, "buy" if units > 0 else "sell"
        pos.entry_qty, pos.entry_notional = abs(units), abs(units) * price
        pos.exit_qty = pos.exit_notional = Decimal(0)

    finished: Trade | None = None
    if before == 0:
        start(after)
    elif (before > 0) == (after > 0) and after != 0:
        if abs(after) > abs(before):  # added to the position
            pos.entry_qty += abs(after) - abs(before)
            pos.entry_notional += (abs(after) - abs(before)) * price
        else:  # partial close
            pos.exit_qty += abs(before) - abs(after)
            pos.exit_notional += (abs(before) - abs(after)) * price
    else:  # flat or flipped: the old trade ends here
        pos.exit_qty += abs(before)
        pos.exit_notional += abs(before) * price
        entry = pos.entry_notional / pos.entry_qty
        exit_ = pos.exit_notional / pos.exit_qty
        sign = Decimal(1) if pos.side == "buy" else Decimal(-1)
        finished = Trade(
            open_ms=pos.opened_ms, close_ms=t_ms, side=pos.side,
            entry=entry.quantize(_PLACES, rounding=ROUND_HALF_EVEN),
            exit=exit_.quantize(_PLACES, rounding=ROUND_HALF_EVEN),
            pnl_pct=(sign * (exit_ / entry - 1) * 100).quantize(_PLACES, rounding=ROUND_HALF_EVEN),
        )  # fmt: skip
        if after != 0:
            start(after)
    return finished


@dataclass(frozen=True)
class _Fill:
    time_ms: int
    strategy_id: str
    seq: int
    instrument: str
    signal: Signal
    price: Decimal
    tainted: bool = False


def _check_candles(candles: Mapping[str, Sequence[Candle]]) -> dict[str, list[int]]:
    times: dict[str, list[int]] = {}
    for inst, series in candles.items():
        ts = [c.open_time_ms for c in series]
        if any(b <= a for a, b in itertools.pairwise(ts)):
            raise TrackRecordError(
                f"candles for {inst} must be strictly ascending by open_time_ms; sort and de-duplicate"
            )
        times[inst] = ts
    return times


def _fill_price(
    sig: Signal, series: Sequence[Candle], times: list[int], fill_ms: int
) -> tuple[int, Decimal] | None:
    i = bisect.bisect_left(times, fill_ms)
    if i >= len(series):
        return None
    horizon = fill_ms + sig.timeframe * 1000
    if sig.order_type == "stop" and sig.stop_price is not None:
        stop = Decimal(str(sig.stop_price))
        while i < len(series) and series[i].open_time_ms < horizon:
            c = series[i]
            if sig.side == "buy" and c.high >= stop:
                return c.open_time_ms, max(c.open, stop)
            if sig.side == "sell" and c.low <= stop:
                return c.open_time_ms, min(c.open, stop)
            i += 1
        return None
    if sig.order_type == "market" or sig.limit_price is None:
        return series[i].open_time_ms, series[i].open
    limit = Decimal(str(sig.limit_price))
    while i < len(series) and series[i].open_time_ms < horizon:
        c = series[i]
        if sig.side == "buy" and c.low <= limit:
            return c.open_time_ms, min(c.open, limit)
        if sig.side == "sell" and c.high >= limit:
            return c.open_time_ms, max(c.open, limit)
        i += 1
    return None


def _apply(pos: _Position, sig: Signal, price: Decimal) -> tuple[bool, Decimal, bool]:
    """Apply a fill. Returns (ok, realised pnl, trade_closed)."""
    f = Decimal(str(sig.size_fraction))
    sign = Decimal(1) if sig.side == "buy" else Decimal(-1)
    realised = Decimal(0)
    closed = False
    if sig.action == "close":
        if pos.units == 0 or (pos.units > 0) == (sign > 0):
            return False, realised, False
        qty = abs(pos.units) if f == 1 else abs(pos.units) * f
        realised = qty * (price - pos.avg) * (1 if pos.units > 0 else -1)
        pos.units = Decimal(0) if f == 1 else pos.units - qty * (1 if pos.units > 0 else -1)
    else:
        delta = sign * f / price
        if pos.units == 0 or (pos.units > 0) == (delta > 0):
            new_units = pos.units + delta
            pos.avg = (abs(pos.units) * pos.avg + abs(delta) * price) / abs(new_units)
            pos.units = new_units
        else:
            qty = min(abs(delta), abs(pos.units))
            realised = qty * (price - pos.avg) * (1 if pos.units > 0 else -1)
            remainder = pos.units + delta
            if remainder == 0 or (remainder > 0) == (pos.units > 0):
                pos.units = remainder
            else:  # flipped: the rest opens a new position at this price
                pos.units, pos.avg = remainder, price
                pos.trade_pnl += realised
                closed = True
                return True, realised, closed
    pos.trade_pnl += realised
    if pos.units == 0:
        closed = True
    return True, realised, closed


def _timeline(
    fills: list[_Fill], candles: Mapping[str, Sequence[Candle]]
) -> Iterator[tuple[int, int, str, int, Decimal, _Fill | None]]:
    """Merged stream of (time_ms, order, instrument, tiebreak, price, fill). Marks sort first."""
    streams: list[Iterable[tuple[int, int, str, int, Decimal, _Fill | None]]] = []
    first = min((f.time_ms for f in fills), default=0)
    for inst in sorted({f.instrument for f in fills}):
        series = candles[inst]
        start = bisect.bisect_left([c.open_time_ms for c in series], first)
        streams.append(
            (c.open_time_ms + CANDLE_MS, 0, inst, 0, c.close, None) for c in series[start:]
        )
    streams.append((f.time_ms, 1, f.instrument, n, f.price, f) for n, f in enumerate(fills))
    return heapq.merge(*streams, key=lambda e: (e[0], e[1], e[2], e[3]))


# ---------------------------------------------------------------- compute


def compute(
    bundle: Bundle,
    candles: Mapping[str, Sequence[Candle]],
    *,
    verifiers: Mapping[str, Anchor] | None = None,
    anchored: Mapping[str, int | None] | None = None,
    tolerance_s: int = 60,
    expected_heads: Mapping[str, tuple[int, str]] | None = None,
    revocations: Iterable[Revocation] = (),
) -> TrackRecord:
    """Recompute a provider's record. `anchored` (event id -> anchored time) overrides proofs."""
    if tolerance_s < 0:
        raise TrackRecordError(f"tolerance_s={tolerance_s} must be >= 0")
    with localcontext() as ctx:
        ctx.prec = 40
        return _compute(
            bundle,
            candles,
            verifiers or {},
            anchored,
            tolerance_s,
            expected_heads,
            list(revocations),
        )


def _anchored_of(
    item: ChainItem,
    bundle: Bundle,
    verifiers: Mapping[str, Anchor],
    override: Mapping[str, int | None] | None,
) -> int | None:
    if override is not None and item.event.id in override:
        return override[item.event.id]
    return anchored_time(bytes.fromhex(item.event.id), bundle.proofs, verifiers)


def _cost(pos: _Position) -> Decimal:
    """Notional (fraction of capital) tied up in a position at its average entry price."""
    return abs(pos.units) * pos.avg


def _compute(
    bundle: Bundle,
    candles: Mapping[str, Sequence[Candle]],
    verifiers: Mapping[str, Anchor],
    override: Mapping[str, int | None] | None,
    tolerance_s: int,
    expected_heads: Mapping[str, tuple[int, str]] | None,
    revocations: list[Revocation],
) -> TrackRecord:
    check: BundleCheck = check_bundle(
        bundle, expected_heads=expected_heads, revocations=revocations
    )
    ctimes = _check_candles(candles)
    excluded: Counter[str] = Counter()
    penalties: Counter[str] = (
        Counter()
    )  # signals scored as misses: unanchored, late_anchor, delegation_expired
    on_time_n = 0
    fills: list[_Fill] = []
    last_fill: dict[str, int] = {}

    for item in check.items:  # (strategy, seq) order
        t = _anchored_of(item, bundle, verifiers, override)
        created_s = (
            item.signal.created_ms // 1000 if item.signal is not None else item.event.created_at
        )
        late = t is not None and t > created_s + tolerance_s
        if t is not None and not late:
            on_time_n += 1
        if item.signal is None:
            excluded["unrevealed"] += 1  # also counted as a miss below
            continue
        sig = item.signal
        if t is not None and revocations:
            try:
                ensure_not_revoked(item.delegation, max(item.event.created_at, t), revocations)
            except DelegationError:
                excluded["revoked"] += 1
                continue
        reason: str | None = None
        if t is None:
            reason = "unanchored"
        elif late:
            reason = "late_anchor"
        elif t >= item.delegation.expires_at:
            reason = "delegation_expired"
        if sig.instrument not in candles:
            excluded["no_candles"] += 1
            continue
        want = max(sig.created_ms, last_fill.get(sig.strategy_id, 0))
        hit = _fill_price(sig, candles[sig.instrument], ctimes[sig.instrument], want)
        if hit is None:
            excluded["unfilled"] += 1
            continue
        if reason is not None:
            penalties[reason] += 1
        fill_ms, price = hit
        last_fill[sig.strategy_id] = fill_ms
        fills.append(
            _Fill(fill_ms, sig.strategy_id, sig.seq, sig.instrument, sig, price, reason is not None)
        )

    fills.sort(key=lambda f: (f.time_ms, f.strategy_id, f.seq))

    positions: dict[tuple[str, str], _Position] = {}
    marks: dict[str, Decimal] = {}
    realised = Decimal(0)
    forfeited = Decimal(0)  # gains of round trips that were not on time
    unreal: dict[tuple[str, str], Decimal] = {}
    unreal_total = Decimal(0)
    wins = losses = tainted_trades = 0
    max_gross = Decimal(0)
    peak = Decimal(1)
    max_dd = Decimal(0)
    curve: list[tuple[int, Decimal]] = []
    trade_list: list[Trade] = []

    def open_penalty() -> Decimal:
        """Gains (realised so far plus unrealised) of open round trips that are not on time: not credited."""
        return sum(
            (
                max(Decimal(0), p.trade_pnl + unreal.get(k, Decimal(0)))
                for k, p in positions.items()
                if p.tainted and p.units != 0
            ),
            Decimal(0),
        )

    for t_ms, _order, inst, _n, price, fill in _timeline(fills, candles):
        if fill is None:
            marks[inst] = price
        else:
            key = (fill.strategy_id, fill.instrument)
            pos = positions.setdefault(key, _Position())
            if fill.signal.action != "close":
                trial = replace(pos)
                _apply(trial, fill.signal, fill.price)
                others = sum((_cost(p) for k, p in positions.items() if k != key), Decimal(0))
                if (
                    _cost(trial) > _cost(pos)
                    and others + _cost(trial) > MAX_GROSS_EXPOSURE + _EXPOSURE_EPS
                ):
                    excluded["exposure_cap"] += 1
                    continue
            before = pos.units
            ok, pnl, closed = _apply(pos, fill.signal, fill.price)
            if not ok:
                excluded["invalid_close"] += 1
                continue
            done = _track_trade(pos, before, pos.units, fill.price, fill.time_ms)
            if done is not None:
                trade_list.append(done)
            realised += pnl
            if closed:
                late_trip = pos.tainted or fill.tainted
                if late_trip:
                    tainted_trades += 1
                    forfeited += max(Decimal(0), pos.trade_pnl)
                if pos.trade_pnl > 0 and not late_trip:
                    wins += 1
                else:
                    losses += 1
                pos.trade_pnl = Decimal(0)
                pos.tainted = fill.tainted and pos.units != 0  # a flip opens a new round trip
            elif fill.tainted:
                pos.tainted = True
            max_gross = max(max_gross, sum((_cost(p) for p in positions.values()), Decimal(0)))
            marks[inst] = price  # the fill price is the freshest mark until the candle closes
        new_unreal = {
            k: p.units * (marks[k[1]] - p.avg)
            for k, p in positions.items()
            if k[1] == inst and p.units != 0
        }
        for k in [k for k in unreal if k[1] == inst and k not in new_unreal]:
            unreal_total -= unreal.pop(k)
        for k, v in new_unreal.items():
            unreal_total += v - unreal.get(k, Decimal(0))
            unreal[k] = v
        equity = 1 + realised - forfeited + unreal_total - open_penalty()
        curve.append((t_ms, equity))
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    open_positions = sum(1 for p in positions.values() if p.units != 0)
    open_forfeit = open_penalty()
    pnl_total = realised - forfeited + unreal_total - open_forfeit
    misses = len(check.unrevealed)
    trades = wins + losses
    denom = trades + misses
    counted = len(fills) - excluded["invalid_close"] - excluded["exposure_cap"]
    total = len(check.items)
    report: dict[str, Any] = {
        "v": REPORT_VERSION,
        "provider": bundle.provider,
        "bundle_sha256": sha256_hex(bundle_to_json(bundle)),
        "pnl": _fmt(pnl_total),
        "pnl_realized": _fmt(realised - forfeited),
        "pnl_unrealized": _fmt(unreal_total - open_forfeit),
        "pnl_forfeited": _fmt(forfeited + open_forfeit),
        "win_rate": None if denom == 0 else _fmt(Decimal(wins) / Decimal(denom)),
        "max_drawdown": _fmt(max_dd),
        "max_gross_exposure": _fmt(max_gross),
        "pnl_per_exposure": None if max_gross == 0 else _fmt(pnl_total / max_gross),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "misses": misses,
        "open_positions": open_positions,
        "unrevealed_commitments": list(check.unrevealed),
        "signals": {
            "total": total,
            "counted": counted,
            "excluded": dict(sorted(excluded.items())),
            "unanchored": penalties["unanchored"],
            "late_anchor": penalties["late_anchor"],
            "delegation_expired": penalties["delegation_expired"],
            "penalized": sum(penalties.values()),
            "penalized_trades": tainted_trades,
        },
        "completeness": {
            "complete": check.complete,
            "head_checked": check.head_checked,
            "gaps": list(check.gaps),
            "problems": list(check.problems),
        },
        "anchor_coverage": {
            "anchored": on_time_n,
            "total": total,
            "ratio": "n/a" if total == 0 else _fmt(Decimal(on_time_n) / Decimal(total)),
        },
        "assumptions": {
            "tolerance_s": tolerance_s,
            "fees": "none",
            "slippage": "none",
            "stop_price": "stop orders trigger on the first candle that reaches it within timeframe",
            "max_gross_exposure": str(MAX_GROSS_EXPOSURE),
            "not_on_time": "a signal not anchored within tolerance is scored as a miss: a loss in win_rate, gains forfeited",
        },
    }
    return TrackRecord(report=report, series=Series(equity=tuple(curve), trades=tuple(trade_list)))
