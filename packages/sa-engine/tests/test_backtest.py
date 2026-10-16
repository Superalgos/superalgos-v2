from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine.backtest import run_backtest, strategy_bar_type
from sa_engine.compile import compile_strategy
from sa_engine.data import DataError
from sa_engine.intent import OrderIntent
from sa_engine.schema import StrategyValidationError
from tests.strategies import ev

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT = "BINANCE:BTCUSDT"


def example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    return data


def fast(doc: dict[str, Any], timeframe: str, when: str | None = None) -> dict[str, Any]:
    """Shrink an example's indicator periods/timeframe so a 5-day fixture produces trades."""
    doc = json.loads(json.dumps(doc))
    doc["timeframe"] = timeframe
    if when:
        doc["stages"]["open"]["when"] = ev(("entry", [when]))
    return doc


def bt(doc: Any, catalog: ParquetDataCatalog, **kw: Any) -> Any:
    return run_backtest(doc, INSTRUMENT, "2025-01-01", "2025-01-06", catalog=catalog, **kw)


def test_ema_crossover_trades_and_reports(fixture_catalog: ParquetDataCatalog) -> None:
    doc = fast(example("ema_crossover"), "15m", "cross_above(ema(close, 5), ema(close, 13))")
    doc["stages"]["close"]["when"] = ev(("exit", ["cross_below(ema(close, 5), ema(close, 13))"]))
    r = bt(doc, fixture_catalog)
    assert r.bars_processed == 5 * 1440
    assert r.trade_count > 3
    assert r.fees > 0 and r.intents >= r.trade_count
    assert 0 <= r.win_rate <= 1 and r.wins == sum(1 for t in r.trades if t.pnl > 0)
    assert r.max_drawdown >= 0 and 0 <= r.max_drawdown_pct < 100
    assert r.final_equity == pytest.approx(r.starting_capital + r.pnl)
    assert len(r.equity_curve) == 5 * 96  # one point per 15m bar close
    assert r.equity_curve == sorted(r.equity_curve)
    if r.open_qty_at_end < 1e-9:  # flat at the end: realised trade PnL (net of fees) is the whole result
        assert r.pnl == pytest.approx(sum(t.pnl for t in r.trades), abs=0.5)
    assert sum(t.fees for t in r.trades) <= r.fees + 1e-6
    assert "ema-crossover" in r.summary()


def test_report_carries_every_fill_for_the_chart(fixture_catalog: ParquetDataCatalog) -> None:
    doc = fast(example("ema_crossover"), "15m", "cross_above(ema(close, 5), ema(close, 13))")
    doc["stages"]["close"]["when"] = ev(("exit", ["cross_below(ema(close, 5), ema(close, 13))"]))
    r = bt(doc, fixture_catalog)
    assert r.fills and [f.t for f in r.fills] == sorted(f.t for f in r.fills)
    assert {f.side for f in r.fills} <= {"buy", "sell"} and all(f.qty > 0 and f.price > 0 for f in r.fills)
    assert sum(f.fee for f in r.fills) == pytest.approx(r.fees)
    buys = sum(f.qty for f in r.fills if f.side == "buy")
    sells = sum(f.qty for f in r.fills if f.side == "sell")
    assert buys - sells == pytest.approx(r.open_qty_at_end, abs=1e-9)
    assert type(r).model_validate_json(r.model_dump_json()).fills == r.fills
    old = r.model_dump(exclude={"fills"})  # a report written before fills existed still loads, with none
    assert type(r).model_validate(old).fills == []


def test_rsi_mean_reversion_runs(fixture_catalog: ParquetDataCatalog) -> None:
    doc = fast(example("rsi_mean_reversion"), "5m", "rsi(close, 14) < 35")
    r = bt(doc, fixture_catalog)
    assert r.trade_count > 0 and r.rejections == []
    assert all(t.qty > 0 and t.closed_ms >= t.opened_ms for t in r.trades)


def test_grid_is_one_document_many_concurrent_orders(fixture_catalog: ParquetDataCatalog) -> None:
    intents: list[OrderIntent] = []
    doc = example("grid_10_level")
    r = bt(doc, fixture_catalog, intent_hook=intents.append)
    entries = [i for i in intents if i.action == "open"]
    assert len(entries) >= 10 and len(entries) % 10 == 0  # every cycle places all ten limit buys together
    assert {i.order_type for i in entries} == {"limit"}
    first_cycle = entries[:10]
    assert len({i.limit_price for i in first_cycle}) == 10 and len({i.ts for i in first_cycle}) == 1
    assert r.trade_count > 0 and r.rejections == []
    exits = [i for i in intents if i.action == "close"]
    assert all(i.side == "sell" and i.order_type in {"limit", "stop", "market"} for i in exits)


def test_backtest_is_deterministic(fixture_catalog: ParquetDataCatalog) -> None:
    doc = fast(example("ema_crossover"), "15m", "cross_above(ema(close, 5), ema(close, 13))")
    a, b = bt(doc, fixture_catalog), bt(doc, fixture_catalog)
    assert a.fingerprint() == b.fingerprint()
    assert a.model_dump() == b.model_dump()
    other = bt(example("grid_10_level"), fixture_catalog)
    assert other.fingerprint() != a.fingerprint()


def test_every_order_passes_through_on_order_intent(fixture_catalog: ParquetDataCatalog) -> None:
    seen: list[OrderIntent] = []
    r = bt(example("grid_10_level"), fixture_catalog, intent_hook=seen.append)
    assert len(seen) == r.intents > 0
    fills = sum(1 for _ in r.trades)
    assert fills <= r.intents
    assert all(i.instrument == INSTRUMENT and i.timeframe == 900 for i in seen)
    assert [i.ts for i in seen] == sorted(i.ts for i in seen)


def test_failing_hook_blocks_the_order(fixture_catalog: ParquetDataCatalog) -> None:
    def boom(intent: OrderIntent) -> None:
        raise RuntimeError("signer offline")

    r = bt(example("grid_10_level"), fixture_catalog, intent_hook=boom)
    assert r.trade_count == 0 and r.fees == 0
    assert r.final_equity == r.starting_capital
    assert any("on_order_intent raised RuntimeError: signer offline; order NOT submitted" in x for x in r.rejections)


def test_subclass_override_of_the_hook_seam() -> None:
    strat = compile_strategy(example("grid_10_level"), "BTCUSDT.BINANCE", strategy_bar_type("BTCUSDT.BINANCE", "15m"))
    i = OrderIntent(
        instrument=INSTRUMENT, side="buy", action="open", size_fraction=0.1, order_type="market", timeframe=60, ts=1
    )
    strat.on_order_intent(i)
    assert strat.intents == [i]


def test_too_small_capital_is_reported_not_crashing(fixture_catalog: ParquetDataCatalog) -> None:
    r = bt(example("grid_10_level"), fixture_catalog, starting_capital=0.01)
    assert r.trade_count == 0 and r.rejections  # sizes below the instrument minimum
    assert any("below the minimum" in x or "denied" in x for x in r.rejections)


def test_1m_and_other_timeframes_bar_types() -> None:
    assert strategy_bar_type("BTCUSDT.BINANCE", "1m") == "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"
    assert strategy_bar_type("BTCUSDT.BINANCE", "4h") == "BTCUSDT.BINANCE-4-HOUR-LAST-INTERNAL@1-MINUTE-EXTERNAL"
    assert strategy_bar_type("BTCUSDT.BINANCE", "1d") == "BTCUSDT.BINANCE-1-DAY-LAST-INTERNAL@1-MINUTE-EXTERNAL"


def test_one_minute_strategy(fixture_catalog: ParquetDataCatalog) -> None:
    doc = {
        "schema_version": 2,
        "name": "m1",
        "timeframe": "1m",
        "stages": {
            "open": {
                "when": ev(("dip", ["close < sma(close, 20) * 0.998"])),
                "orders": [{"id": "e", "size_fraction": 0.5, "take_profit_pct": 0.3}],
            },
            "close": {"when": ev(("time stop", ["bars_in_position > 60"]))},
        },
    }
    r = bt(doc, fixture_catalog)
    assert len(r.equity_curve) == 5 * 1440 and r.trade_count > 0


def test_input_validation(fixture_catalog: ParquetDataCatalog, tmp_path: Path) -> None:
    doc = example("grid_10_level")
    with pytest.raises(StrategyValidationError, match=r"\$\.timeframe"):
        run_backtest({**doc, "timeframe": "3m"}, INSTRUMENT, "2025-01-01", "2025-01-02", catalog=fixture_catalog)
    with pytest.raises(ValueError, match="must be after start"):
        run_backtest(doc, INSTRUMENT, "2025-01-02", "2025-01-01", catalog=fixture_catalog)
    with pytest.raises(ValueError, match="only BINANCE"):
        run_backtest(doc, "BYBIT:BTCUSDT", "2025-01-01", "2025-01-02", catalog=fixture_catalog)
    with pytest.raises(ValueError, match="starting_capital"):
        run_backtest(doc, INSTRUMENT, "2025-01-01", "2025-01-02", catalog=fixture_catalog, starting_capital=0)
    with pytest.raises(DataError, match="no 1m bars"):
        run_backtest(doc, INSTRUMENT, "2030-01-01", "2030-01-02", catalog=fixture_catalog)
    with pytest.raises(DataError, match="offline=True"):
        run_backtest(doc, INSTRUMENT, "2025-01-01", "2025-01-02", data_dir=tmp_path, offline=True)


def test_accepts_json_text_dates_and_instrument_spellings(fixture_catalog: ParquetDataCatalog) -> None:
    from datetime import date, datetime

    text = json.dumps(example("grid_10_level"))
    a = run_backtest(text, "BTCUSDT", date(2025, 1, 1), datetime(2025, 1, 2), catalog=fixture_catalog)
    b = run_backtest(text, "BTCUSDT.BINANCE", "2025-01-01", "2025-01-02", catalog=fixture_catalog)
    assert a.fingerprint() == b.fingerprint()
