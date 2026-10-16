"""Backtest/paper reporting: FIFO trade matching, drawdown, win rate. Pure Python, no Nautilus."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Trade(BaseModel):
    """One closed lot: entry fill matched (FIFO) to the exit fill that closed it. ``pnl`` is net of fees."""

    model_config = ConfigDict(frozen=True)

    opened_ms: int
    closed_ms: int
    qty: float
    entry_price: float
    exit_price: float
    fees: float
    pnl: float


class ReportFill(BaseModel):
    """One executed order as the engine filled it (every fill, not only matched lots): the chart's markers."""

    model_config = ConfigDict(frozen=True)

    t: int  # ms since epoch (UTC): bar close for entries on a bar, fill time for exits
    side: str  # buy | sell
    price: float
    qty: float
    fee: float = 0.0


class BacktestReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy: str
    instrument: str
    timeframe: str
    start: datetime
    end: datetime
    starting_capital: float
    final_equity: float
    pnl: float
    pnl_pct: float
    trade_count: int
    wins: int
    win_rate: float  # 0..1; 0 when there are no trades
    max_drawdown: float  # absolute, quote currency
    max_drawdown_pct: float  # percent of the running peak
    fees: float
    open_qty_at_end: float
    bars_processed: int
    intents: int
    rejections: list[str]
    trades: list[Trade]
    fills: list[ReportFill] = []  # absent in reports written before F7: re-run the backtest to chart its fills
    equity_curve: list[tuple[int, float]]  # (ts ms, equity) at each strategy-timeframe bar close

    def fingerprint(self) -> str:
        """sha256 of the canonical JSON: equal fingerprints mean bit-identical results."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def summary(self) -> str:
        return (
            f"{self.strategy} {self.instrument} {self.start:%Y-%m-%d}..{self.end:%Y-%m-%d}: "
            f"pnl {self.pnl:,.2f} ({self.pnl_pct:.2f}%), trades {self.trade_count}, win rate {self.win_rate:.1%}, "
            f"max drawdown {self.max_drawdown_pct:.2f}%, fees {self.fees:,.2f}"
        )


@dataclass(frozen=True, slots=True)
class Fill:
    ts: int
    side: str  # buy | sell
    qty: float
    price: float
    commission: float


def match_trades(fills: Sequence[Fill]) -> tuple[list[Trade], float]:
    """FIFO-match sells to earlier buys (long-only). Returns (trades, quantity still open)."""
    lots: deque[list[float]] = deque()  # [ts, qty_remaining, price, fee_per_unit]
    trades: list[Trade] = []
    for f in fills:
        if f.qty <= 0:
            continue
        if f.side == "buy":
            lots.append([float(f.ts), f.qty, f.price, f.commission / f.qty])
            continue
        remaining = f.qty
        exit_fee_per_unit = f.commission / f.qty
        while remaining > 1e-12 and lots:
            lot = lots[0]
            take = min(remaining, lot[1])
            fees = take * (lot[3] + exit_fee_per_unit)
            trades.append(
                Trade(
                    opened_ms=int(lot[0]),
                    closed_ms=f.ts,
                    qty=take,
                    entry_price=lot[2],
                    exit_price=f.price,
                    fees=fees,
                    pnl=take * (f.price - lot[2]) - fees,
                )
            )
            lot[1] -= take
            remaining -= take
            if lot[1] <= 1e-12:
                lots.popleft()
    return trades, sum(lot[1] for lot in lots)


def max_drawdown(curve: Sequence[tuple[int, float]]) -> tuple[float, float]:
    """(absolute, percent of peak) maximum peak-to-trough decline of the equity curve."""
    peak = float("-inf")
    worst_abs = worst_pct = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        dd = peak - equity
        if dd > worst_abs:
            worst_abs = dd
        if peak > 0:
            worst_pct = max(worst_pct, dd / peak * 100.0)
    return worst_abs, worst_pct
