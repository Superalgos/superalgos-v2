"""SEP-3.17: signals execute on the Binance spot testnet, and only there. Offline: no node touches the network."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.adapters.binance import BinanceAccountType, BinanceDataClientConfig
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LiveExecEngineConfig, LoggingConfig, TradingNodeConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from pydantic import SecretStr
from sa_core.keys import ExchangeCredentials

from sa_engine import catalog as cat
from sa_engine.catalog import load_bars
from sa_engine.intent import OrderIntent
from sa_engine.network.executor import Execution, NautilusTestnetExecutor
from sa_engine.network.ledger import Ledger
from sa_engine.paper import ExecutionResult, FollowerConfig
from sa_engine.testnet import (
    NotTestnetError,
    SpotTestnetFollower,
    SpotTestnetSession,
    SpotTestnetStrategy,
    assert_testnet_only,
    check_testnet_target,
)

INSTRUMENT = "BINANCE:BTCUSDT"
IID = "BTCUSDT.BINANCE"
CANARY_KEY = "CANARYKEY0123456789abcdefCANARYKEY0123456789abcdefCANARYKEYAAAA"
CANARY_SECRET = "CANARYSECRET0123456789abcdefCANARYSECRET0123456789abcdefAAAA"  # noqa: S105
CREDS = ExchangeCredentials(api_key=SecretStr(CANARY_KEY), api_secret=SecretStr(CANARY_SECRET))


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


# --------------------------------------------------------------------------- Anti: no non-testnet endpoint


@pytest.mark.parametrize("env", [BinanceEnvironment.LIVE, BinanceEnvironment.DEMO, None, "TESTNET", "testnet", 0])
def test_anti_any_environment_but_testnet_is_refused(env: object) -> None:
    with pytest.raises(NotTestnetError, match=r"only runs on BinanceEnvironment\.TESTNET"):
        check_testnet_target(env)
    with pytest.raises(NotTestnetError):
        SpotTestnetFollower(INSTRUMENT, CREDS, environment=env, disaster_stop_pct=0)


@pytest.mark.parametrize("name", ["base_url_http", "base_url_ws", "base_url_ws_stream"])
@pytest.mark.parametrize("url", ["https://api.binance.com", "https://testnet.binance.vision", ""])
def test_anti_any_endpoint_override_is_refused_even_the_testnet_url(name: str, url: str) -> None:
    with pytest.raises(NotTestnetError, match=f"{name}=.*remove the override"):
        check_testnet_target(**{name: url})
    with pytest.raises(NotTestnetError, match=name):
        SpotTestnetFollower(INSTRUMENT, CREDS, disaster_stop_pct=0, **{name: url})


def test_the_default_target_is_the_testnet() -> None:
    check_testnet_target()
    check_testnet_target(BinanceEnvironment.TESTNET)


def test_anti_a_session_refuses_a_bad_target_before_building_anything() -> None:
    strategy = SpotTestnetStrategy(FollowerConfig(instrument_id=IID, order_id_tag="003", disaster_stop_pct=0))
    with pytest.raises(NotTestnetError):
        SpotTestnetSession(strategy, INSTRUMENT, CREDS, environment=BinanceEnvironment.LIVE)
    with pytest.raises(ValueError, match=r"must not be empty.*sa exchange add binance-testnet"):
        SpotTestnetSession(strategy, INSTRUMENT, ExchangeCredentials(api_key=SecretStr(""), api_secret=SecretStr("x")))


def test_the_node_config_is_testnet_spot_for_data_and_exec_and_checked() -> None:
    cfg = SpotTestnetFollower(INSTRUMENT, CREDS, disaster_stop_pct=0).session.node_config()
    assert set(cfg.data_clients) == set(cfg.exec_clients) == {"BINANCE"}
    for client in (cfg.data_clients["BINANCE"], cfg.exec_clients["BINANCE"]):
        assert client.environment is BinanceEnvironment.TESTNET
        assert client.account_type is BinanceAccountType.SPOT
        assert client.base_url_http is None and client.base_url_ws is None
    assert type(cfg.exec_clients["BINANCE"]).__name__ == "BinanceExecClientConfig"  # the real adapter, not the sandbox
    assert cfg.exec_engine.reconciliation is False and cfg.logging is not None and cfg.logging.print_config is False
    assert cfg.data_clients["BINANCE"].api_key == CANARY_KEY  # compared, never printed
    assert_testnet_only(cfg)


def test_anti_the_built_config_check_catches_a_tampered_node() -> None:
    def config(env: BinanceEnvironment | None, **kw: Any) -> TradingNodeConfig:
        return TradingNodeConfig(
            trader_id=TraderId("T-001"),
            logging=LoggingConfig(bypass_logging=True),
            exec_engine=LiveExecEngineConfig(reconciliation=False),
            data_clients={"BINANCE": BinanceDataClientConfig(environment=env, **kw)},
        )

    with pytest.raises(NotTestnetError, match="environment"):
        assert_testnet_only(config(BinanceEnvironment.LIVE))
    with pytest.raises(NotTestnetError, match="environment"):
        assert_testnet_only(config(None))
    with pytest.raises(NotTestnetError, match="base_url_http"):
        assert_testnet_only(config(BinanceEnvironment.TESTNET, base_url_http="https://example.invalid"))
    with pytest.raises(NotTestnetError, match="spot only"):
        assert_testnet_only(config(BinanceEnvironment.TESTNET, account_type=BinanceAccountType.USDT_FUTURES))


# --------------------------------------------------------------------------- order id and fill events (backtest venue)


class Scripted(SpotTestnetStrategy):
    def __init__(self, script: dict[int, tuple[OrderIntent, str]]) -> None:
        super().__init__(FollowerConfig(instrument_id=IID, order_id_tag="004", disaster_stop_pct=0))
        self.script, self.n = script, 0

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
        if self.n in self.script:
            it, ref = self.script[self.n]
            self.execute(it, ref)


def run(script: dict[int, tuple[OrderIntent, str]], catalog: ParquetDataCatalog) -> Scripted:
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            starting_balances=[Money(10_000, USDT)],
            bar_execution=True,
        )
        engine.add_instrument(cat.instrument_for("BTCUSDT"))
        engine.add_data(load_bars(catalog, IID, date(2025, 1, 1), date(2025, 1, 2)))
        strategy = Scripted(script)
        engine.add_strategy(strategy)
        engine.run()
        return strategy
    finally:
        engine.dispose()


def test_a_market_fill_carries_the_venue_order_id_even_without_an_accepted_event(
    fixture_catalog: ParquetDataCatalog,
) -> None:
    # the backtest venue fills a market order without an OrderAccepted: the id must still reach the ledger
    s = run({5: (intent(size_fraction=0.2), "evt-1")}, fixture_catalog)
    (filled,) = s.drain()
    assert filled.ref == "evt-1" and filled.placed and filled.filled and filled.reason == "filled"
    assert filled.venue_order_id
    assert filled.qty is not None and filled.price is not None and filled.price > 0 and filled.fee is not None
    assert float(filled.qty) * filled.price == pytest.approx(2_000, rel=1e-3)
    assert s.drain() == []  # taken once


def test_a_resting_order_reports_its_venue_order_id_when_accepted(fixture_catalog: ParquetDataCatalog) -> None:
    s = run({5: (intent(order_type="limit", limit_price=1.0), "evt-2")}, fixture_catalog)
    (accepted,) = s.drain()
    assert accepted.ref == "evt-2" and accepted.placed and not accepted.filled
    assert accepted.venue_order_id and accepted.reason == "accepted by the Binance testnet"


def test_a_sell_closes_and_refusals_come_back_as_not_placed_events(fixture_catalog: ParquetDataCatalog) -> None:
    s = run(
        {
            3: (intent(side="sell", action="close"), "nothing"),
            4: (intent(side="sell", action="open"), "short"),
            5: (intent(instrument="BINANCE:ETHUSDT"), "wrong"),
            6: (intent(), "buy"),
            9: (intent(side="sell", action="close", size_fraction=1.0), "close"),
        },
        fixture_catalog,
    )
    by_ref: dict[str, list[Execution]] = {}
    for e in s.drain():
        by_ref.setdefault(e.ref, []).append(e)
    for ref in ("nothing", "short", "wrong"):
        assert len(by_ref[ref]) == 1 and not by_ref[ref][0].placed and by_ref[ref][0].reason
    assert "short selling" in by_ref["short"][0].reason
    assert by_ref["buy"][-1].filled and by_ref["close"][-1].filled
    assert by_ref["close"][-1].venue_order_id != by_ref["buy"][-1].venue_order_id


def test_partial_fills_aggregate_into_one_average_price_and_cancellation_is_reported() -> None:
    s = SpotTestnetStrategy(FollowerConfig(instrument_id=IID, order_id_tag="005", disaster_stop_pct=0))
    s._venue_ids["t"] = "123"
    from sa_engine.compile import FillRecord

    s.fills += [FillRecord(1, "buy", 1.0, 100.0, 0.1, "t"), FillRecord(2, "buy", 3.0, 200.0, 0.2, "t")]
    s._report_fill("t", 200.0, 3.0, 2, False)
    s._report_fill("none", 5.0, 0.0, 3, True)  # no fills recorded: falls back to the given price, no division by zero
    first, second = s.drain()
    assert first.reason == "partially filled" and first.qty == "4" and first.price == pytest.approx(175.0)
    assert first.fee == pytest.approx(0.3) and first.venue_order_id == "123" and first.filled
    assert second.qty == "0" and second.price == 5.0
    s._report_end("t", "")
    s._report_end("t", "rejected: min notional")
    s._report_end("t", "denied: bad")
    s._report_end("t", "expired")
    cancelled, rejected, denied, expired = s.drain()
    assert cancelled.placed and cancelled.reason == "cancelled" and cancelled.venue_order_id == "123"
    assert not rejected.placed and "min notional" in rejected.reason and not denied.placed
    assert expired.placed and expired.reason == "expired"


def test_events_for_orders_it_did_not_place_are_ignored() -> None:
    s = SpotTestnetStrategy(FollowerConfig(instrument_id=IID, order_id_tag="006", disaster_stop_pct=0))
    s.on_order_accepted(_FakeEvent())
    s.on_order_filled(_FakeEvent())
    assert s.drain() == []


class _FakeEvent:
    client_order_id = "unknown"


# --------------------------------------------------------------------------- executor adapter


class FakeFollower:
    def __init__(self) -> None:
        self.sent: list[tuple[OrderIntent, str]] = []
        self.events: list[Execution] = []
        self.results: list[object] = []

    def submit(self, it: OrderIntent, ref: str) -> None:
        self.sent.append((it, ref))

    def drain(self) -> list[Execution]:
        out, self.events = self.events, []
        return out


async def test_the_executor_queues_with_the_signal_ref_and_polls_exchange_events() -> None:
    inner = FakeFollower()
    ex = NautilusTestnetExecutor(inner)
    res = await ex.execute("e1", intent(), 5)
    assert res.placed and not res.filled and res.venue_order_id is None and inner.sent == [(intent(), "e1")]
    assert await ex.poll(6) == []
    inner.events = [Execution("e1", True, venue_order_id="42")]
    assert [e.venue_order_id for e in await ex.poll(7)] == ["42"] and await ex.poll(8) == []
    assert ex.snapshot(9) == {"submitted": 1, "results": 0}


# --------------------------------------------------------------------------- ledger


def test_the_ledger_records_the_venue_order_id_and_keeps_it_through_later_updates(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.record(event_id="e1", provider="p", source="t", received_ms=1, status="accepted")
    ledger.set_execution(Execution("e1", True, reason="submitted"))
    assert ledger.get("e1") is not None and ledger.get("e1").venue_order_id is None  # type: ignore[union-attr]
    ledger.set_execution(Execution("e1", True, reason="accepted", venue_order_id="777"))
    ledger.set_execution(Execution("e1", True, True, reason="filled", qty="0.001", price=50_000.0, fee=0.05))
    row = ledger.get("e1")
    assert row is not None and row.venue_order_id == "777" and row.filled and row.qty == "0.001"
    ledger.set_execution(Execution("e1", False, reason="rejected: x"))
    assert ledger.get("e1").placed is False  # type: ignore[union-attr]
    ledger.close()


def test_a_ledger_created_before_venue_order_ids_gets_the_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE signals (event_id TEXT PRIMARY KEY, provider TEXT NOT NULL, strategy_id TEXT, seq INTEGER,"
        " ref TEXT, prev TEXT, source TEXT NOT NULL, received_ms INTEGER NOT NULL, created_ms INTEGER,"
        " status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', signal_json TEXT,"
        " anchor_status TEXT NOT NULL DEFAULT 'pending', anchored_s INTEGER, placed INTEGER NOT NULL DEFAULT 0,"
        " filled INTEGER NOT NULL DEFAULT 0, qty TEXT, price REAL, fee REAL, filled_ms INTEGER,"
        " equity_before REAL, ref_price REAL, exec_reason TEXT NOT NULL DEFAULT '')"
    )
    db.execute(
        "INSERT INTO signals (event_id, provider, source, received_ms, status) VALUES ('old','p','t',1,'accepted')"
    )
    db.commit()
    db.close()
    ledger = Ledger(path)
    ledger.set_execution(Execution("old", True, venue_order_id="9"))
    row = ledger.get("old")
    assert row is not None and row.venue_order_id == "9"
    ledger.close()


def test_a_ledger_created_before_queued_closes_gets_the_column_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE signals (event_id TEXT PRIMARY KEY, provider TEXT NOT NULL, strategy_id TEXT, seq INTEGER,"
        " ref TEXT, prev TEXT, source TEXT NOT NULL, received_ms INTEGER NOT NULL, created_ms INTEGER,"
        " status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', signal_json TEXT,"
        " anchor_status TEXT NOT NULL DEFAULT 'pending', anchored_s INTEGER, placed INTEGER NOT NULL DEFAULT 0,"
        " filled INTEGER NOT NULL DEFAULT 0, qty TEXT, price REAL, fee REAL, filled_ms INTEGER,"
        " equity_before REAL, ref_price REAL, exec_reason TEXT NOT NULL DEFAULT '', venue_order_id TEXT)"
    )
    db.execute(
        "INSERT INTO signals (event_id, provider, source, received_ms, status) VALUES ('old','p','t',1,'accepted')"
    )
    db.commit()
    db.close()
    ledger = Ledger(path)
    old = ledger.get("old")
    assert old is not None and old.queued is False
    ledger.set_execution(Execution("old", False, reason="queued", queued=True))
    queued = ledger.get("old")
    assert queued is not None and queued.queued and not queued.placed
    ledger.set_execution(Execution("old", True, venue_order_id="9"))
    placed = ledger.get("old")
    assert placed is not None and placed.placed and not placed.queued
    ledger.close()
    Ledger(path).close()  # opening again after the migration is harmless


# --------------------------------------------------------------------------- Anti: secret leakage


def test_anti_nautilus_never_logs_the_secret_when_it_builds_the_node(tmp_path: Path) -> None:
    """Build a real node from the testnet config at DEBUG with print_config, in a fresh process; the canary must not appear.

    Nautilus writes its log to the process' stdout from native code, so only a subprocess can see all of it.
    """
    script = tmp_path / "build.py"
    script.write_text(
        textwrap.dedent(
            """
            import asyncio, os
            from pydantic import SecretStr
            from nautilus_trader.config import LoggingConfig
            from nautilus_trader.live.node import TradingNode
            from sa_core.keys import ExchangeCredentials
            from sa_engine.testnet import SpotTestnetFollower

            creds = ExchangeCredentials(
                api_key=SecretStr(os.environ["CANARY_KEY"]), api_secret=SecretStr(os.environ["CANARY_SECRET"])
            )
            follower = SpotTestnetFollower("BINANCE:BTCUSDT", creds, disaster_stop_pct=0, log_level="DEBUG")
            cfg = follower.session.node_config()
            cfg = cfg.__class__(**{**{f: getattr(cfg, f) for f in cfg.__struct_fields__},
                                   "logging": LoggingConfig(log_level="DEBUG", use_pyo3=False, print_config=True)})
            node = TradingNode(cfg, loop=asyncio.new_event_loop())
            follower.session.add_factories(node)
            node.build()
            node.dispose()
            print("BUILT-OK")
            """
        )
    )
    import os

    env = {**os.environ, "CANARY_KEY": CANARY_KEY, "CANARY_SECRET": CANARY_SECRET}
    done = subprocess.run(  # noqa: S603 - the project's own interpreter running a script written above
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=120, check=False
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "BUILT-OK" in out, out[-2000:].replace(CANARY_SECRET, "[REDACTED]")
    assert "TradingNode" in out  # Nautilus did log at DEBUG, so the absence below means something
    assert CANARY_SECRET not in out and CANARY_KEY not in out and "CANARY" not in out


# --------------------------------------------------------------------------- follower front


def test_the_follower_queues_until_ready_dispatches_with_the_ref_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    f = SpotTestnetFollower(INSTRUMENT, CREDS, disaster_stop_pct=0)
    executed: list[tuple[OrderIntent, str | None]] = []
    monkeypatch.setattr(f.strategy, "execute", lambda it, ref=None: executed.append((it, ref)))
    f.submit(intent(), "evt-1")  # node not started: backlog
    assert executed == []
    ready: list[SpotTestnetFollower] = []

    def fake_run(duration_s: float | None, on_ready: Any) -> None:
        f.session.started.set()
        on_ready(f.session)
        f.strategy.results.append(ExecutionResult(intent(), True, ref="evt-1"))
        f.strategy.results.append(ExecutionResult(intent(), False, "nope", "evt-2"))
        f.strategy._events.append(Execution("evt-1", True, venue_order_id="5"))

    monkeypatch.setattr(f.session, "run", fake_run)
    report = f.run(1.0, on_ready=ready.append)
    assert ready == [f] and executed == [(intent(), "evt-1")]
    assert report.intents == 2 and report.placed == 1 and report.instrument == INSTRUMENT
    assert [e.venue_order_id for e in f.drain()] == ["5"] and len(f.results) == 2


def test_the_session_registers_the_real_binance_factories_for_its_venue() -> None:
    from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory, BinanceLiveExecClientFactory

    class Node:
        def __init__(self) -> None:
            self.added: list[tuple[str, str, object]] = []

        def add_data_client_factory(self, name: str, factory: object) -> None:
            self.added.append(("data", name, factory))

        def add_exec_client_factory(self, name: str, factory: object) -> None:
            self.added.append(("exec", name, factory))

    node = Node()
    SpotTestnetFollower(INSTRUMENT, CREDS, disaster_stop_pct=0).session.add_factories(node)  # type: ignore[arg-type]
    assert node.added == [
        ("data", "BINANCE", BinanceLiveDataClientFactory),
        ("exec", "BINANCE", BinanceLiveExecClientFactory),
    ]
