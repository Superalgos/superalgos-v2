"""Deterministic backtests of compiled strategies on Binance 1m bars via Nautilus ``BacktestEngine``."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine import catalog as cat
from sa_engine.compile import CompiledStrategy, IntentHook, compile_strategy
from sa_engine.data import DataError, Fetch, default_data_dir, http_fetch
from sa_engine.intent import normalize_instrument, to_nautilus_id
from sa_engine.report import BacktestReport, Fill, ReportFill, match_trades, max_drawdown
from sa_engine.schema import StrategyDoc, validate_strategy


def _as_dt(value: date | datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def strategy_bar_type(instrument_id: str, timeframe: str) -> str:
    """1m strategies use the 1m bars directly; others aggregate the 1m stream (INTERNAL) so fills stay 1m-accurate."""
    base = f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
    if timeframe == "1m":
        return base
    n, unit = int(timeframe[:-1]), {"m": "MINUTE", "h": "HOUR", "d": "DAY"}[timeframe[-1]]
    return f"{instrument_id}-{n}-{unit}-LAST-INTERNAL@1-MINUTE-EXTERNAL"


def run_backtest(
    strategy_doc: StrategyDoc | Mapping[str, Any] | str,
    instrument: str,
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    starting_capital: float = 10_000.0,
    data_dir: Path | None = None,
    offline: bool = False,
    fetch: Fetch = http_fetch,
    catalog: ParquetDataCatalog | None = None,
    intent_hook: IntentHook | None = None,
) -> BacktestReport:
    """Backtest ``strategy_doc`` on ``instrument`` (e.g. ``BINANCE:BTCUSDT``) for bars opening in [start, end).

    Data is downloaded (checksum-verified) into ``data_dir`` (default ``$SA_DATA_DIR`` or ``./.sa-data``) on first use.
    Results are deterministic: the same inputs give an identical ``BacktestReport.fingerprint()``.
    """
    doc = (
        strategy_doc
        if isinstance(strategy_doc, StrategyDoc)
        else validate_strategy(strategy_doc if isinstance(strategy_doc, str) else dict(strategy_doc))
    )
    inst_name = normalize_instrument(instrument)
    venue_name, _, symbol = inst_name.partition(":")
    if venue_name != "BINANCE":
        raise ValueError(f"only BINANCE instruments are supported, got {inst_name!r}")
    t0, t1 = _as_dt(start), _as_dt(end)
    if t1 <= t0:
        raise ValueError(f"end {t1:%Y-%m-%d} must be after start {t0:%Y-%m-%d}")
    if starting_capital <= 0:
        raise ValueError(f"starting_capital must be positive, got {starting_capital}")

    if catalog is None:
        catalog = cat.ensure_catalog(symbol, t0, t1, data_dir or default_data_dir(), fetch, offline=offline)
    instrument_id = to_nautilus_id(inst_name)
    bars = cat.load_bars(catalog, instrument_id, t0, t1)
    if not bars:
        raise DataError(f"no 1m bars for {inst_name} in {t0:%Y-%m-%d}..{t1:%Y-%m-%d} in the catalog")

    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id="BACKTESTER-001",
            logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
        )
    )
    try:
        venue = Venue(venue_name)
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,  # leverage 1, long-only by construction; avoids double-locking OCO exits
            base_currency=None,
            starting_balances=[Money(starting_capital, USDT)],
            default_leverage=Decimal(1),
            bar_execution=True,
        )
        instrument_obj = cat.instrument_for(symbol)
        engine.add_instrument(instrument_obj)
        engine.add_data(bars)
        strategy = compile_strategy(
            doc, instrument_id, strategy_bar_type(instrument_id, doc.timeframe), intent_hook=intent_hook
        )
        engine.add_strategy(strategy)
        engine.run()
        return _build_report(doc, inst_name, t0, t1, starting_capital, len(bars), strategy)
    finally:
        engine.dispose()


def _build_report(
    doc: StrategyDoc,
    instrument: str,
    t0: datetime,
    t1: datetime,
    capital: float,
    bars: int,
    strategy: CompiledStrategy,
) -> BacktestReport:
    fills = [Fill(f.ts, f.side, f.qty, f.price, f.commission) for f in strategy.fills]
    trades, open_qty = match_trades(fills)
    curve = [(ts, round(eq, 8)) for ts, eq in strategy.equity_curve]
    final = curve[-1][1] if curve else capital
    dd_abs, dd_pct = max_drawdown([(0, capital), *curve])
    wins = sum(1 for t in trades if t.pnl > 0)
    return BacktestReport(
        strategy=doc.name,
        instrument=instrument,
        timeframe=doc.timeframe,
        start=t0,
        end=t1,
        starting_capital=capital,
        final_equity=final,
        pnl=final - capital,
        pnl_pct=(final - capital) / capital * 100.0,
        trade_count=len(trades),
        wins=wins,
        win_rate=wins / len(trades) if trades else 0.0,
        max_drawdown=dd_abs,
        max_drawdown_pct=dd_pct,
        fees=sum(f.commission for f in fills),
        open_qty_at_end=open_qty,
        bars_processed=bars,
        intents=len(strategy.intents),
        rejections=[*strategy.rejections, *strategy.hook_errors],
        trades=trades,
        fills=[ReportFill(t=f.ts, side=f.side, price=f.price, qty=f.qty, fee=f.commission) for f in fills],
        equity_curve=curve,
    )
