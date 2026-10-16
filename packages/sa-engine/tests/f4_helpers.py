"""Builders shared by the F4 tests: bars, brains, one-minute catalogs and a hook to reach the compiled strategy."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine.backtest import run_backtest
from sa_engine.brain import Command, StrategyBrain, Submit
from sa_engine.catalog import write_minute_bars
from sa_engine.compile import CompiledStrategy, compile_strategy
from sa_engine.indicators import OHLCV
from sa_engine.report import BacktestReport
from sa_engine.schema import validate_strategy
from tests.strategies import ev

MINUTE = 60_000
T0 = 1_735_689_600_000  # 2025-01-01T00:00:00Z
INSTRUMENT = "BINANCE:BTCUSDT"


def when(*conditions: str) -> dict[str, object]:
    """An event with one situation: all ``conditions`` (AND)."""
    return ev(("s", list(conditions)))


def bar(i: int, close: float, *, high: float | None = None, low: float | None = None, step_ms: int = MINUTE) -> OHLCV:
    """Bar number ``i`` (closes at ``i * step_ms``)."""
    return OHLCV(i * step_ms, close, close if high is None else high, close if low is None else low, close, 1.0)


def make_doc(stages: dict[str, Any], timeframe: str = "1m") -> dict[str, Any]:
    return {"schema_version": 2, "name": "t", "timeframe": timeframe, "stages": stages}


def make_brain(stages: dict[str, Any], timeframe: str = "1m", tick: float | None = 0.01) -> StrategyBrain:
    return StrategyBrain(validate_strategy(make_doc(stages, timeframe)), INSTRUMENT, tick=tick)


def submits(cmds: Sequence[Command]) -> list[Submit]:
    return [c for c in cmds if isinstance(c, Submit)]


def frame_from_rows(rows: Sequence[tuple[float, float, float, float]], start: str = "2025-01-01") -> pd.DataFrame:
    """1m OHLC rows (open, high, low, close) as a close-time-indexed frame."""
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC") + pd.Timedelta(minutes=1)
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": 10.0,
        },
        index=pd.DatetimeIndex(idx, name="timestamp"),
    )


def catalog_of(path: Path, frame: pd.DataFrame) -> ParquetDataCatalog:
    catalog = ParquetDataCatalog(path)
    write_minute_bars(catalog, "BTCUSDT", frame)
    return catalog


def flat(price: float, n: int) -> list[tuple[float, float, float, float]]:
    return [(price, price, price, price)] * n


class Run:
    """A backtest plus the compiled strategy that ran (to read its brain's log and slots)."""

    def __init__(self, report: BacktestReport, strategy: CompiledStrategy) -> None:
        self.report = report
        self.strategy = strategy

    @property
    def brain(self) -> StrategyBrain:
        assert self.strategy._brain is not None
        return self.strategy._brain


def backtest(
    doc: dict[str, Any], frame: pd.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> Run:
    end = (frame.index[-1] + pd.Timedelta(seconds=1)).isoformat()
    return backtest_on(doc, catalog_of(tmp_path / "cat", frame), monkeypatch, "2025-01-01", end, **kw)


def backtest_on(
    doc: dict[str, Any],
    catalog: ParquetDataCatalog,
    monkeypatch: pytest.MonkeyPatch,
    start: str = "2025-01-01",
    end: str = "2025-01-06",
    **kw: Any,
) -> Run:
    seen: list[CompiledStrategy] = []

    def capture(*a: Any, **k: Any) -> CompiledStrategy:
        strategy = compile_strategy(*a, **k)
        seen.append(strategy)
        return strategy

    monkeypatch.setattr("sa_engine.backtest.compile_strategy", capture)
    report = run_backtest(doc, INSTRUMENT, start, end, catalog=catalog, **kw)
    return Run(report, seen[0])
