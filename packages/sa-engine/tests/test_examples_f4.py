"""The two F4 examples run end to end on the offline fixture catalog (5 days of 1m bars)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from tests.f4_helpers import backtest_on

ROOT = Path(__file__).resolve().parents[1]


def example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ROOT / "examples" / name).read_text())
    return data


def quick_trend() -> dict[str, Any]:
    """trend_phases with periods small enough for five days of data; the structure is untouched."""
    text = (ROOT / "examples" / "trend_phases.json").read_text()
    text = text.replace("ema(close, 20)", "ema(close, 5)").replace("ema(close, 50)", "ema(close, 13)")
    text = text.replace('ema(close, 50, \\"4h\\")', 'ema(close, 3, \\"1h\\")')
    doc: dict[str, Any] = json.loads(text)
    doc["stages"]["open"]["when"]["any"][0]["all"][1] = 'close > ema(close, 3, "1h")'
    stop = doc["stages"]["manage"]["stop"]["phases"]
    stop[0]["next"]["any"][0]["all"] = ["close > entry_price + 0.1 * atr(14)"]  # reachable on the fixture's noise
    stop[1]["next"]["any"][0]["all"] = ["close > entry_price + 0.3 * atr(14)"]
    return doc


def test_trend_phases_walks_its_phases_and_exits_on_levels(
    fixture_catalog: ParquetDataCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = backtest_on(quick_trend(), fixture_catalog, monkeypatch)
    events = [f.event for f in run.brain.log]
    assert run.report.trade_count > 3 and run.report.rejections == []
    assert any(e.startswith("stop.phase:") for e in events)  # the stop moved on to a later phase
    assert any(e.endswith(".hit") for e in events)  # and positions were closed by a level
    sells = [f for f in run.report.fills if f.side == "sell"]
    buys = [f for f in run.report.fills if f.side == "buy"]
    assert sells and sum(f.qty for f in buys) - sum(f.qty for f in sells) == pytest.approx(
        run.report.open_qty_at_end, abs=1e-8
    )


def test_ladder_example_runs_clean(fixture_catalog: ParquetDataCatalog, monkeypatch: pytest.MonkeyPatch) -> None:
    run = backtest_on(example("ladder_rearm.json"), fixture_catalog, monkeypatch)
    assert run.report.trade_count > 10 and run.report.rejections == []
    assert run.report.fills == sorted(run.report.fills, key=lambda f: f.t)


def test_old_examples_still_run_after_the_migration(
    fixture_catalog: ParquetDataCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ema_crossover.json", "rsi_mean_reversion.json", "grid_10_level.json"):
        run = backtest_on(example(name), fixture_catalog, monkeypatch)
        assert run.report.rejections == [], name
