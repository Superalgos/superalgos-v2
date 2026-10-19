"""A protective close that arrives while its entry is still in flight waits for it; flat cancels resting stops.

Observed on the Binance spot testnet (2026-10-19): the provider's stop-loss intent follows its market entry before the
venue reports the fill, so the follower sized the close against a zero position and dropped it, leaving the position
unprotected. The backtest venue below delays every order by one data step (``LatencyModel``), which reproduces it.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine import catalog as cat
from sa_engine import paper
from sa_engine.catalog import load_bars, write_minute_bars
from sa_engine.intent import OrderIntent
from sa_engine.network.executor import Execution, NautilusTestnetExecutor
from sa_engine.paper import FollowerConfig
from sa_engine.sizing import SizeResult
from sa_engine.testnet import SpotTestnetStrategy
from tests.conftest import synthetic_minutes
from tests.spot_venue import SpotVenue

IID = "BTCUSDT.BINANCE"
BARS = 30
Action = Callable[["Follower"], None]


def intent(**kw: Any) -> OrderIntent:
    base: dict[str, Any] = {
        "instrument": "BINANCE:BTCUSDT",
        "side": "buy",
        "action": "open",
        "size_fraction": 0.1,
        "order_type": "market",
        "timeframe": 60,
        "ts": 1,
    }
    return OrderIntent.model_validate({**base, **kw})


ENTRY = intent()
STOP = intent(side="sell", action="close", order_type="stop", stop_price=1.0, size_fraction=1.0)  # far below: rests
CLOSE = intent(side="sell", action="close", size_fraction=1.0)
REJECTED_ENTRY = intent(order_type="limit", limit_price=29_000.0)  # the venue below rejects this price
RESTING_ENTRY = intent(order_type="limit", limit_price=1.0)  # far below the market: never fills


class Follower(SpotTestnetStrategy):
    """The testnet strategy on a backtest venue; ``script`` maps a bar number to the actions run on that bar."""

    def __init__(self, script: dict[int, list[Action]]) -> None:
        super().__init__(FollowerConfig(instrument_id=IID, order_id_tag="004", disaster_stop_pct=0))
        self.script, self.n = script, 0
        self.executed = 0
        self.violations: list[str] = []
        self.events: list[Execution] = []
        self.end_position, self.end_working = 0.0, 0
        self.end_stops: list[Any] = []
        self.stop_qtys: list[str] = []

    def on_start(self) -> None:
        if self._load_instrument() is not None:
            self.subscribe_bars(BarType.from_str(f"{IID}-1-MINUTE-LAST-EXTERNAL"))

    def on_bar(self, bar: Bar) -> None:
        self.n += 1
        self._last_price = bar.close.as_double()
        for action in self.script.get(self.n, []):
            action(self)

    def _size(self, intent: OrderIntent) -> SizeResult:
        entry_open = any(
            o.is_buy and not o.is_reduce_only and not o.is_closed for o in self.cache.orders(instrument_id=self._iid)
        )
        if intent.action == "close" and self.position().qty <= 0 and entry_open:
            self.violations.append(f"close sized against a zero position while an entry is in flight: {intent}")
        return super()._size(intent)

    def snapshot(self) -> None:
        """Read the venue state before the engine is disposed."""
        orders = self.cache.orders(instrument_id=self._iid, strategy_id=self.id)
        self.end_position = self.position().qty
        self.end_working = self.working_orders()
        self.end_stops = [o for o in orders if o.is_reduce_only and not o.is_closed]
        self.stop_qtys = [str(o.quantity) for o in self.end_stops]

    def by_ref(self, ref: str) -> list[Execution]:
        return [e for e in self.events if e.ref == ref]


def send(it: OrderIntent, ref: str) -> Action:
    def go(s: Follower) -> None:
        s.executed += 1
        s.execute(it, ref)

    return go


@pytest.fixture(scope="module")
def bars_catalog() -> ParquetDataCatalog:
    path = Path(tempfile.mkdtemp())
    catalog = ParquetDataCatalog(path)
    write_minute_bars(catalog, "BTCUSDT", synthetic_minutes(days=1))
    return catalog


@contextmanager
def wait_limit(seconds: float) -> Any:
    with patch.object(paper, "CLOSE_WAIT_TIMEOUT_S", seconds, create=True):
        yield


def run(
    script: dict[int, list[Action]], catalog: ParquetDataCatalog, *, latency: bool = True, timeout_s: float = 120.0
) -> Follower:
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            starting_balances=[Money(10_000, USDT)],
            bar_execution=True,
            latency_model=LatencyModel(base_latency_nanos=1_000_000) if latency else None,
        )
        engine.add_instrument(cat.instrument_for("BTCUSDT"))
        engine.add_data(load_bars(catalog, IID, date(2025, 1, 1), date(2025, 1, 2))[:BARS])
        strategy = Follower(script)
        for algorithm in strategy.exec_algorithms():
            engine.add_exec_algorithm(algorithm)
        engine.add_strategy(strategy)
        SpotVenue.install(
            engine, refuse=lambda o: "PRICE_FILTER" if o.has_price and float(o.price) == 29_000.0 else None
        )
        with wait_limit(timeout_s):
            engine.run()
        strategy.snapshot()
        strategy.events = strategy.drain()
        return strategy
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- the observed failure


def test_a_stop_that_arrives_before_the_entry_fill_waits_and_is_sized_to_the_fill(
    bars_catalog: ParquetDataCatalog,
) -> None:
    s = run({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, bars_catalog)
    entry = s.by_ref("entry")[-1]
    assert entry.filled and entry.qty is not None
    placed = s.by_ref("stop")
    assert placed and all(e.placed for e in placed), [e.reason for e in placed]
    assert "held locally" in placed[-1].reason and placed[-1].venue_order_id is None, "the exchange never sees a stop"
    assert s.stop_qtys == [entry.qty]
    assert [r.placed for r in s.results] == [True, True] and s.violations == []


def test_the_wait_is_in_arrival_order_for_several_closes(bars_catalog: ParquetDataCatalog) -> None:
    half = intent(side="sell", action="close", order_type="stop", stop_price=1.0, size_fraction=0.5)
    s = run({3: [send(ENTRY, "entry"), send(half, "s1"), send(STOP, "s2")]}, bars_catalog)
    assert [r.ref for r in s.results] == ["entry", "s1", "s2"] and all(r.placed for r in s.results)


def test_a_close_with_no_entry_in_flight_still_says_nothing_to_close(bars_catalog: ParquetDataCatalog) -> None:
    s = run({3: [send(STOP, "stop")]}, bars_catalog)
    (event,) = s.by_ref("stop")
    assert not event.placed and "nothing to close" in event.reason


def test_the_close_waits_even_when_the_entry_is_a_resting_limit_until_the_timeout(
    bars_catalog: ParquetDataCatalog,
) -> None:
    s = run({3: [send(RESTING_ENTRY, "entry"), send(STOP, "stop")]}, bars_catalog, timeout_s=20)
    (dropped,) = [e for e in s.by_ref("stop") if not e.placed]
    assert "timeout" in dropped.reason and "20s" in dropped.reason


# --------------------------------------------------------------------------- the entry ends without a fill


def test_a_rejected_entry_drops_the_waiting_stop_with_the_reason(bars_catalog: ParquetDataCatalog) -> None:
    s = run({3: [send(REJECTED_ENTRY, "entry"), send(STOP, "stop")]}, bars_catalog)
    (dropped,) = s.by_ref("stop")
    assert not dropped.placed
    assert "entry order ended without a fill" in dropped.reason and "rejected" in dropped.reason
    assert s.end_stops == []
    assert [r.placed for r in s.results if r.ref == "stop"] == [False]


def test_a_cancelled_entry_drops_the_waiting_stop_with_the_reason(bars_catalog: ParquetDataCatalog) -> None:
    def cancel(s: Follower) -> None:
        assert s._cancel_tag("entry")

    s = run({3: [send(RESTING_ENTRY, "entry"), send(STOP, "stop")], 4: [cancel]}, bars_catalog)
    dropped = [e for e in s.by_ref("stop") if not e.placed]
    assert len(dropped) == 1 and "cancelled" in dropped[0].reason and s.end_stops == []


# --------------------------------------------------------------------------- timeout


def test_a_close_that_waits_longer_than_the_timeout_is_dropped_naming_it(bars_catalog: ParquetDataCatalog) -> None:
    # the venue fills the entry 30 s after it is sent; the limit here is 5 s
    s = run({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, bars_catalog, timeout_s=5)
    (dropped,) = [e for e in s.by_ref("stop")]
    assert not dropped.placed and "timeout" in dropped.reason and "5s" in dropped.reason
    assert s.end_stops == []


def test_the_default_timeout_is_a_named_constant() -> None:
    assert paper.CLOSE_WAIT_TIMEOUT_S == 30.0


def test_a_close_still_waiting_when_the_node_stops_is_reported_dropped(bars_catalog: ParquetDataCatalog) -> None:
    s = run({BARS: [send(RESTING_ENTRY, "entry"), send(STOP, "stop")]}, bars_catalog)
    (dropped,) = s.by_ref("stop")
    assert not dropped.placed and "node stopped" in dropped.reason


# --------------------------------------------------------------------------- anti-orphan


def test_closing_the_position_cancels_the_working_stop(bars_catalog: ParquetDataCatalog) -> None:
    s = run({3: [send(ENTRY, "entry"), send(STOP, "stop")], 8: [send(CLOSE, "close")]}, bars_catalog)
    assert s.end_position == 0
    assert s.end_stops == [] and s.end_working == 0
    assert [e.reason for e in s.by_ref("stop")][-1] == "cancelled"
    assert s.by_ref("close")[-1].filled


def test_a_stop_survives_while_the_position_is_open(bars_catalog: ParquetDataCatalog) -> None:
    s = run({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, bars_catalog)
    assert s.end_position > 0 and len(s.end_stops) == 1 and s.end_working == 1


def test_the_stop_survives_a_partial_close(bars_catalog: ParquetDataCatalog) -> None:
    half = intent(side="sell", action="close", size_fraction=0.5)
    s = run({3: [send(ENTRY, "entry"), send(STOP, "stop")], 8: [send(half, "half")]}, bars_catalog)
    assert s.end_position > 0 and len(s.end_stops) == 1


# --------------------------------------------------------------------------- ledger truth


async def test_a_close_is_not_reported_placed_until_the_exchange_says_so() -> None:
    class Inner:
        def submit(self, it: OrderIntent, ref: str) -> None: ...

    ex = NautilusTestnetExecutor(Inner())
    entry, stop = await ex.execute("e", ENTRY, 1), await ex.execute("s", STOP, 2)
    assert entry.placed and not stop.placed and stop.queued and "not at the exchange yet" in stop.reason


# --------------------------------------------------------------------------- property: random interleavings

OPS: dict[str, tuple[OrderIntent, bool]] = {
    "entry": (ENTRY, False),
    "rejected_entry": (REJECTED_ENTRY, False),
    "resting_entry": (RESTING_ENTRY, False),
    "stop": (STOP, True),
    "close": (CLOSE, True),
}


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    steps=st.lists(
        st.tuples(st.integers(1, 6), st.sampled_from([*sorted(OPS), "entry", "stop", "close"])), min_size=2, max_size=8
    ),
    latency=st.booleans(),
    timeout_s=st.sampled_from([5.0, 120.0]),
)
def test_property_a_close_never_meets_a_zero_position_while_an_entry_is_in_flight(
    bars_catalog: ParquetDataCatalog, steps: list[tuple[int, str]], latency: bool, timeout_s: float
) -> None:
    script: dict[int, list[Action]] = {}
    for i, (bar, kind) in enumerate(steps):
        script.setdefault(bar, []).append(send(OPS[kind][0], f"{kind}-{i}"))
    s = run(script, bars_catalog, latency=latency, timeout_s=timeout_s)
    assert s.violations == []
    # every intent ends up in the results exactly once, and every refusal says why
    assert len(s.results) == s.executed
    assert all(r.reason for r in s.results if not r.placed)
    # after the position went flat no reduce-only order is left working
    if s.end_position <= 0:
        assert s.end_stops == []
