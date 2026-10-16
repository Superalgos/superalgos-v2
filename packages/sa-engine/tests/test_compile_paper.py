"""Nautilus glue exercised offline through a BacktestEngine, plus the pure parts of paper trading."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine import catalog as cat
from sa_engine.backtest import run_backtest, strategy_bar_type
from sa_engine.compile import ExecutionBase, compile_strategy
from sa_engine.intent import OrderIntent
from sa_engine.paper import (
    DEFAULT_FEE_RATE,
    ExecutionResult,
    FollowerConfig,
    FollowerExecutor,
    FollowerStrategy,
    PaperReport,
    PaperSession,
    _binance_timeframe_bar_type,
    _report,
    run_paper,
)
from sa_engine.schema import StrategyValidationError
from tests.strategies import ev

INSTRUMENT = "BINANCE:BTCUSDT"
IID = "BTCUSDT.BINANCE"


def doc(**stages: Any) -> dict[str, Any]:
    """Stages with plain condition strings in ``when`` fields; each becomes a one-situation event."""

    def wrap(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: ev(("s", [v])) if k in {"when", "on", "off"} and isinstance(v, str) else wrap(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [wrap(v) for v in value]
        return value

    return {"schema_version": 2, "name": "t", "timeframe": "5m", "stages": wrap(stages)}


def bt(d: dict[str, Any], catalog: ParquetDataCatalog) -> Any:
    return run_backtest(d, INSTRUMENT, "2025-01-01", "2025-01-04", catalog=catalog)


# --------------------------------------------------------------------------- compiled strategy glue


def test_close_stage_flattens_and_cancels_pending(fixture_catalog: ParquetDataCatalog) -> None:
    d = doc(
        open={
            "when": "close > sma(close, 10)",
            "orders": [
                {"id": "m", "size_fraction": 0.3},
                {"id": "far", "type": "limit", "size_fraction": 0.3, "offset_pct": -20},
            ],
        },
        close={"when": "bars_in_position > 12"},
    )
    intents: list[OrderIntent] = []
    r = run_backtest(d, INSTRUMENT, "2025-01-01", "2025-01-04", catalog=fixture_catalog, intent_hook=intents.append)
    assert r.trade_count > 3
    closes = [i for i in intents if i.action == "close"]
    assert closes and all(i.order_type == "market" and i.size_fraction == 1.0 for i in closes)


def test_trigger_off_and_cancel_pending_rule_cancel_working_orders(fixture_catalog: ParquetDataCatalog) -> None:
    d = doc(
        trigger={"on": "true", "off": "close > 31000"},
        open={"when": "true", "orders": [{"id": "far", "type": "limit", "size_fraction": 0.5, "offset_pct": -30}]},
        manage={"rules": [{"id": "c", "when": "bars_in_position >= 0 and close < 29000", "action": "cancel_pending"}]},
    )
    r = bt(d, fixture_catalog)
    assert r.trade_count == 0 and r.fees == 0 and r.intents >= 1
    assert r.rejections == []  # cancels are not rejections


def test_rejected_orders_are_recorded(fixture_catalog: ParquetDataCatalog) -> None:
    # a stop-buy at the current price is "in the market": the simulated venue rejects it
    d = doc(open={"when": "true", "orders": [{"id": "s", "type": "stop", "size_fraction": 0.5, "offset_pct": 0}]})
    r = bt(d, fixture_catalog)
    assert any(x.startswith("s#") and "rejected" in x for x in r.rejections)


def test_unknown_instrument_stops_the_strategy(fixture_catalog: ParquetDataCatalog) -> None:
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            starting_balances=[Money(1000, USDT)],
        )
        engine.add_instrument(cat.instrument_for("BTCUSDT"))
        engine.add_data(cat.load_bars(fixture_catalog, IID, date(2025, 1, 1), date(2025, 1, 2)))
        eth = "ETHUSDT.BINANCE"
        strat = compile_strategy(
            doc(open={"when": "true", "orders": [{"id": "a", "size_fraction": 1}]}), eth, strategy_bar_type(eth, "5m")
        )
        engine.add_strategy(strat)
        engine.run()
    finally:
        engine.dispose()
    assert strat.bars_seen == 0 and strat.intents == []


def test_compile_validates_the_document() -> None:
    with pytest.raises(StrategyValidationError, match=r"\$\.stages\.open"):
        compile_strategy({"name": "x", "timeframe": "1h", "stages": {}}, IID, strategy_bar_type(IID, "1h"))


# --------------------------------------------------------------------------- follower strategy offline


class ScriptedFollower(FollowerStrategy):
    """Feeds the follower bar closes as prices and executes scripted intents at given bar numbers."""

    def __init__(self, script: dict[int, OrderIntent]) -> None:
        super().__init__(FollowerConfig(instrument_id=IID, order_id_tag="009"))
        self.script = script
        self.n = 0

    def on_start(self) -> None:
        if self._load_instrument() is not None:
            self.subscribe_bars(
                __import__("nautilus_trader.model.data", fromlist=["BarType"]).BarType.from_str(
                    f"{IID}-1-MINUTE-LAST-EXTERNAL"
                )
            )

    def on_bar(self, bar: Bar) -> None:
        self.n += 1
        self._last_price = bar.close.as_double()
        self._quote(None)
        if self.n in self.script:
            self.execute(self.script[self.n])


def run_follower(
    script: dict[int, OrderIntent], catalog: ParquetDataCatalog, capital: float = 10_000.0
) -> ScriptedFollower:
    from sa_engine.catalog import load_bars

    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            starting_balances=[Money(capital, USDT)],
            bar_execution=True,
        )
        engine.add_instrument(cat.instrument_for("BTCUSDT"))
        engine.add_data(load_bars(catalog, IID, date(2025, 1, 1), date(2025, 1, 2)))
        follower = ScriptedFollower(script)
        engine.add_strategy(follower)
        engine.run()
        return follower
    finally:
        engine.dispose()


def intent(**kw: Any) -> OrderIntent:
    base: dict[str, Any] = {
        "instrument": INSTRUMENT,
        "side": "buy",
        "action": "open",
        "size_fraction": 0.1,
        "order_type": "market",
        "timeframe": 60,
        "ts": 1,
    }
    return OrderIntent.model_validate({**base, **kw})


def test_follower_sizes_open_by_fraction_of_capital_and_close_by_fraction_of_position(
    fixture_catalog: ParquetDataCatalog,
) -> None:
    f = run_follower(
        {
            5: intent(size_fraction=0.2),
            10: intent(side="sell", action="close", size_fraction=0.5),
            15: intent(side="sell", action="close", size_fraction=1.0),
        },
        fixture_catalog,
    )
    assert [r.placed for r in f.results] == [True, True, True]
    buy, half, rest = f.fills
    assert buy.side == "buy" and half.side == rest.side == "sell"
    assert buy.qty * buy.price == pytest.approx(0.2 * 10_000, rel=1e-3)
    assert half.qty == pytest.approx(buy.qty / 2, abs=2e-6)
    assert rest.qty == pytest.approx(buy.qty / 2, abs=2e-6)
    assert f.position().qty == pytest.approx(0, abs=1e-9)
    assert f.quotes_seen == 0


def test_follower_limit_and_unplaceable_intents(fixture_catalog: ParquetDataCatalog) -> None:
    f = run_follower(
        {
            2: intent(order_type="limit", limit_price=1.0),  # far below: rests unfilled
            3: intent(side="sell", action="close"),  # nothing to close
            4: intent(side="sell", action="open"),  # shorting unsupported on spot
            5: intent(instrument="BINANCE:ETHUSDT"),  # wrong market
        },
        fixture_catalog,
    )
    assert [r.placed for r in f.results] == [True, False, False, False]
    assert "nothing to close" in f.results[1].reason
    assert "short selling" in f.results[2].reason
    assert "this executor trades BTCUSDT.BINANCE" in f.results[3].reason
    assert f.fills == []


def test_follower_waits_for_the_first_price(fixture_catalog: ParquetDataCatalog) -> None:
    class Early(ScriptedFollower):
        def on_start(self) -> None:
            super().on_start()
            self.execute(intent())  # before any price is known

    from sa_engine.catalog import load_bars

    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            starting_balances=[Money(10_000, USDT)],
        )
        engine.add_instrument(cat.instrument_for("BTCUSDT"))
        engine.add_data(load_bars(fixture_catalog, IID, date(2025, 1, 1), date(2025, 1, 2)))
        f = Early({})
        engine.add_strategy(f)
        engine.run()
    finally:
        engine.dispose()
    assert len(f.results) == 1 and f.results[0].placed and len(f.fills) == 1


def test_execution_base_hooks_are_noops() -> None:
    f = FollowerStrategy(FollowerConfig(instrument_id=IID, order_id_tag="003"))
    f._filled("t", 1.0, 1.0, 1, True)
    f._order_ended("t", "x")
    f._quote(None)
    assert isinstance(f, ExecutionBase)


# --------------------------------------------------------------------------- paper session / executor (no network)


def test_paper_helpers() -> None:
    assert _binance_timeframe_bar_type(IID, "1h") == "BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL"
    assert _binance_timeframe_bar_type(IID, "15m") == "BTCUSDT.BINANCE-15-MINUTE-LAST-EXTERNAL"
    assert _binance_timeframe_bar_type(IID, "1d") == "BTCUSDT.BINANCE-1-DAY-LAST-EXTERNAL"
    assert Decimal("0.001") == DEFAULT_FEE_RATE


def test_node_config_uses_public_data_and_a_sandbox_exec_client_only() -> None:
    f = FollowerExecutor(INSTRUMENT, 5_000)
    cfg = f.session.node_config()
    data_cfg = cfg.data_clients["BINANCE"]
    exec_cfg = cfg.exec_clients["BINANCE"]
    assert data_cfg.api_key is None and data_cfg.api_secret is None
    assert type(exec_cfg).__name__ == "SandboxExecutionClientConfig"
    assert exec_cfg.starting_balances == ["5000 USDT"]
    assert set(cfg.exec_clients) == {"BINANCE"}  # no real execution client is ever configured


def test_run_paper_validates_before_touching_the_network() -> None:
    good = doc(open={"when": "true", "orders": [{"id": "a", "size_fraction": 1}]})
    with pytest.raises(ValueError, match="capital must be positive"):
        run_paper(good, INSTRUMENT, 0)
    with pytest.raises(StrategyValidationError, match=r"\$\.timeframe"):
        run_paper({**good, "timeframe": "9m"}, INSTRUMENT, 100)
    with pytest.raises(ValueError, match="capital must be positive"):
        FollowerExecutor(INSTRUMENT, -1)
    with pytest.raises(ValueError, match="not a symbol"):
        FollowerExecutor("BTC USDT", 1)


def test_session_needs_to_run_before_cross_thread_calls() -> None:
    s = FollowerExecutor(INSTRUMENT, 100).session
    with pytest.raises(RuntimeError, match="not running"):
        s.call_soon(lambda: None)
    s.stop()  # harmless before start


class FakeLoop:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def call_soon_threadsafe(self, fn: Any) -> None:
        self.calls.append(fn)
        fn()


def test_follower_executor_queues_until_ready_then_dispatches_in_loop() -> None:
    ex = FollowerExecutor(INSTRUMENT, 1_000)
    executed: list[OrderIntent] = []
    ex.strategy.execute = executed.append  # type: ignore[method-assign,assignment]
    a, b = intent(size_fraction=0.1), intent(size_fraction=0.2)
    ex.submit(a)  # node not started: backlog
    assert executed == []
    ex.session.started.set()
    ex._ready(ex.session)
    assert executed == [a]
    ex.session._loop = FakeLoop()  # type: ignore[assignment]
    ex.submit(b)
    assert executed == [a, b]
    assert ex.results == []


def test_follower_run_collects_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = FollowerExecutor(INSTRUMENT, 1_000)
    ready_calls: list[FollowerExecutor] = []

    def fake_run(duration_s: float | None, on_ready: Any) -> None:
        ex.strategy.final_equity = 1_010.0
        ex.strategy.results.append(ExecutionResult(intent(), True))
        on_ready(ex.session)

    monkeypatch.setattr(ex.session, "run", fake_run)
    report = ex.run(1.0, on_ready=ready_calls.append)
    assert ready_calls == [ex]
    assert isinstance(report, PaperReport)
    assert report.final_equity == 1_010.0 and report.starting_capital == 1_000 and report.intents == 1
    assert ex.run.__name__ == "run" and PaperSession.stop.__name__ == "stop"


def test_report_defaults_final_equity_to_capital() -> None:
    f = FollowerStrategy(FollowerConfig(instrument_id=IID, order_id_tag="004"))
    rep = _report(f, INSTRUMENT, 1.5, 500.0, 0, 0)
    assert rep.final_equity == 500.0 and rep.orders_filled == 0 and rep.trades == []


def test_example_documents_compile_for_paper() -> None:
    root = Path(__file__).resolve().parents[1] / "examples"
    for p in root.glob("*.json"):
        d = json.loads(p.read_text())
        s = compile_strategy(d, IID, _binance_timeframe_bar_type(IID, d["timeframe"]), subscribe_quotes=True)
        assert s.doc.name == d["name"]


def test_quote_tick_updates_reference_price(fixture_catalog: ParquetDataCatalog) -> None:
    f = FollowerStrategy(FollowerConfig(instrument_id=IID, order_id_tag="005"))
    tick = QuoteTick(
        cat.instrument_for("BTCUSDT").id,
        Price.from_str("100.00"),
        Price.from_str("102.00"),
        Quantity.from_str("1.000000"),
        Quantity.from_str("1.000000"),
        1,
        1,
    )
    f.on_quote_tick(tick)
    assert f.quotes_seen == 1 and f._last_price == 101.0
