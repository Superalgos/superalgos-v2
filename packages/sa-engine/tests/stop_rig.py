"""A scripted follower on a Binance-spot-like backtest venue with a price path (SEP-3.45, SEP-14.5).

``Rig`` is the testnet strategy (the one that reports order events) driven bar by bar: ``script`` maps a bar number
(1-based) to the actions run on it, ``prices`` is the price of every bar. Each bar also carries a trade tick at its
price, the data the emulated stops trigger on. The venue is :class:`spot_venue.SpotVenue`.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pandas as pd
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType
from nautilus_trader.model.identifiers import TradeId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler

from sa_engine import catalog as cat
from sa_engine import paper
from sa_engine.intent import OrderIntent
from sa_engine.network.executor import Execution
from sa_engine.paper import FollowerConfig
from sa_engine.testnet import SpotTestnetStrategy
from tests.spot_venue import SpotVenue

IID = "BTCUSDT.BINANCE"
BAR_TYPE = BarType.from_str(f"{IID}-1-MINUTE-LAST-EXTERNAL")
START = pd.Timestamp("2025-01-01", tz="UTC")
Action = Callable[["Rig"], None]


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


def send(it: OrderIntent, ref: str) -> Action:
    def go(s: Rig) -> None:
        s.executed += 1
        s.execute(it, ref)

    return go


class Rig(SpotTestnetStrategy):
    def __init__(self, script: dict[int, list[Action]], *, pct: float, tag: str = "007") -> None:
        super().__init__(FollowerConfig(instrument_id=IID, order_id_tag=tag, disaster_stop_pct=pct))
        self.script, self.n, self.executed = script, 0, 0
        self.venue: SpotVenue | None = None
        self.events: list[Execution] = []
        self.observed: list[str] = []  # invariant breaches seen at settled points
        self.end_position = Decimal(0)
        self.end_working = 0
        self.end_resting = 0  # stop-limits resting at the venue when the run ended

    def on_start(self) -> None:
        if self._load_instrument() is not None:
            self.subscribe_bars(BAR_TYPE)

    def on_bar(self, bar: Bar) -> None:
        self.observe()  # the previous bar's actions have settled
        self.n += 1
        self._last_price = bar.close.as_double()
        for action in self.script.get(self.n, []):
            action(self)

    # -- what the venue holds
    def observe(self) -> None:
        assert self.venue is not None
        resting = self.venue.resting_stop_limits()
        held = Decimal(str(self.position().qty))
        if len(resting) > 1:
            self.observed.append(f"bar {self.n}: {len(resting)} stop-limits rest")
        if held <= 0 and resting:
            self.observed.append(f"bar {self.n}: a stop-limit rests while flat")
        d = self._disaster
        if resting and d is not None and d.state == "working" and Decimal(str(resting[0].quantity)) != held:
            self.observed.append(f"bar {self.n}: stop-limit {resting[0].quantity} against a position of {held}")

    def by_ref(self, ref: str) -> list[Execution]:
        return [e for e in self.events if e.ref == ref]

    def cancel_disaster_outside(self) -> None:
        """The exchange cancels the disaster stop behind the follower's back (the user did it by hand)."""
        assert self.venue is not None
        for order in self.venue.resting_stop_limits():
            self.venue.real.cancel_order(
                CancelOrder(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=order.venue_order_id,
                    command_id=UUID4(),
                    ts_init=self.clock.timestamp_ns(),
                )
            )


def market_data(prices: list[float]) -> tuple[list[Bar], list[TradeTick]]:
    inst = cat.instrument_for("BTCUSDT")
    idx = pd.DatetimeIndex([START + pd.Timedelta(minutes=i + 1) for i in range(len(prices))], name="timestamp")
    frame = pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [10.0] * len(prices)}, index=idx
    )
    bars = BarDataWrangler(BAR_TYPE, inst).process(frame)
    ticks = [
        TradeTick(
            inst.id,
            Price(p, inst.price_precision),
            Quantity(1, inst.size_precision),
            AggressorSide.SELLER,
            TradeId(str(i)),
            bar.ts_event - 1,
            bar.ts_event - 1,
        )
        for i, (p, bar) in enumerate(zip(prices, bars, strict=True))
    ]
    return bars, ticks


def run_rig(
    script: dict[int, list[Action]],
    prices: list[float],
    *,
    pct: float = 0,
    latency: bool = False,
    close_wait_s: float | None = None,
    **venue: Any,
) -> Rig:
    """Run ``script`` against ``prices`` on the spot-like venue; returns the strategy, venue state included."""
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
        bars, ticks = market_data(prices)
        engine.add_data(bars)
        engine.add_data(ticks)
        rig = Rig(script, pct=pct)
        for algorithm in rig.exec_algorithms():
            engine.add_exec_algorithm(algorithm)
        engine.add_strategy(rig)
        rig.venue = SpotVenue.install(engine, **venue)
        limit = close_wait_s if close_wait_s is not None else paper.CLOSE_WAIT_TIMEOUT_S
        with patch.object(paper, "CLOSE_WAIT_TIMEOUT_S", limit):
            engine.run()
        rig.observe()
        assert rig.venue is not None
        rig.end_position = Decimal(str(rig.position().qty))
        rig.end_working = rig.working_orders()
        rig.end_resting = len(rig.venue.resting_stop_limits())
        rig.events = rig.drain()
        return rig
    finally:
        engine.dispose()


def flat(price: float, bars: int) -> list[float]:
    return [price] * bars


def path(*legs: tuple[float, int]) -> list[float]:
    """``path((50_000, 6), (44_000, 4))``: six bars at 50,000, then four at 44,000."""
    out: list[float] = []
    for price, count in legs:
        out += [price] * count
    return out
