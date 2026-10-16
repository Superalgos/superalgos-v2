"""The simulated paper account follows the sizing contract (open = fraction x equity, close = fraction x position)."""

from __future__ import annotations

import asyncio
from decimal import ROUND_DOWN, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sa_engine.network.executor import NautilusPaperExecutor, SimulatedPaperExecutor
from sa_engine.network.replay import PriceFeed
from tests.net_helpers import candles, flat_feed, intent

START = 1_000_000


def acct(price: float = 50_000.0, capital: float = 10_000.0) -> SimulatedPaperExecutor:
    return SimulatedPaperExecutor("BINANCE:BTCUSDT", capital, flat_feed(price, START))


async def test_open_is_sized_from_equity_and_close_from_the_position() -> None:
    a = acct()
    got = await a.execute("e1", intent(size=0.5), START)
    assert got.filled and got.qty == "0.1" and got.price == 50_000.0 and got.equity_before == 10_000.0
    assert a.cash == Decimal("10000") - Decimal("0.1") * 50_000 * Decimal("1.001")  # 0.1% fee
    half = await a.execute("e2", intent(action="close", side="sell", size=0.5), START)
    assert half.filled and half.qty == "0.05"
    rest = await a.execute("e3", intent(action="close", side="sell", size=1.0), START)
    assert rest.filled and rest.qty == "0.05" and a.qty == 0
    assert (
        a.equity(START) == pytest.approx(10_000 - 2 * 0.1 * 0.001 * 50_000 * 0.5 * 2, abs=1e-6)
        or a.equity(START) < 10_000
    )
    assert a.snapshot(START)["fills"] == 3


async def test_refusals_say_why() -> None:
    a = acct()
    assert "short selling" in (await a.execute("e", intent(side="sell", action="open"), START)).reason
    assert "nothing to close" in (await a.execute("e", intent(side="sell", action="close", size=1), START)).reason
    other = await a.execute("e", intent(instrument="BINANCE:ETHUSDT"), START)
    assert not other.placed and "ETHUSDT" in other.reason
    tiny = SimulatedPaperExecutor("BINANCE:BTCUSDT", 0.01, flat_feed(50_000.0, START))
    assert "below the minimum" in (await tiny.execute("e", intent(size=0.5), START)).reason
    with pytest.raises(ValueError, match="capital"):
        SimulatedPaperExecutor("BINANCE:BTCUSDT", 0, flat_feed())


async def test_buy_is_capped_by_cash_when_already_invested() -> None:
    a = acct()
    await a.execute("e1", intent(size=1.0), START)  # all in (minus fee headroom)
    again = await a.execute("e2", intent(size=1.0), START)
    assert not again.filled and "not enough cash" in again.reason
    b = acct()
    await b.execute("e1", intent(size=0.9), START)
    capped = await b.execute("e2", intent(size=0.9), START)  # wants 0.9 x equity, has ~0.1 x equity in cash
    assert capped.filled and Decimal(capped.qty or "0") < Decimal("0.18")


async def test_marketable_limit_fills_now_and_resting_limit_waits_for_the_price() -> None:
    feed = PriceFeed(candles([100.0, 100.0, 90.0, 90.0], START, step_ms=1000, spread=1))
    a = SimulatedPaperExecutor("BINANCE:BTCUSDT", 1000.0, feed)
    now = await a.execute("e1", intent(order_type="limit", limit=101.0, size=0.2), START)
    assert now.filled and now.price == 100.0  # limit buy above the market fills at the market
    rest = await a.execute("e2", intent(order_type="limit", limit=95.0, size=0.2), START)
    assert rest.placed and not rest.filled and rest.reason == "working order"
    assert await a.poll(START + 500) == []  # price has not reached 95
    fills = await a.poll(START + 2500)  # the 90 candle (low 89) is current
    assert [f.ref for f in fills] == ["e2"] and fills[0].price == 95.0 and fills[0].filled


async def test_stop_loss_triggers_on_the_low_and_closing_cancels_working_exits() -> None:
    feed = PriceFeed(candles([100.0, 100.0, 80.0, 80.0], START, step_ms=1000, spread=0.5))
    a = SimulatedPaperExecutor("BINANCE:BTCUSDT", 1000.0, feed)
    await a.execute("e1", intent(size=0.5), START)
    stop = await a.execute("e2", intent(action="close", side="sell", order_type="stop", stop=90.0, size=1.0), START)
    assert stop.placed and not stop.filled
    await a.execute("e3", intent(action="close", side="sell", order_type="stop", stop=85.0, size=1.0), START)
    fills = await a.poll(START + 2500)
    assert [f.ref for f in fills] == ["e2"] and fills[0].price == 90.0
    assert a.qty == 0 and a.working == []  # flat: the second exit is cancelled, not left to open a short
    assert await a.poll(START + 3500) == []


async def test_stop_buy_and_sell_limit_directions() -> None:
    feed = PriceFeed(candles([100.0, 100.0, 120.0], START, step_ms=1000, spread=1))
    a = SimulatedPaperExecutor("BINANCE:BTCUSDT", 1000.0, feed)
    breakout = await a.execute("b", intent(order_type="stop", stop=115.0, size=0.1), START)
    assert breakout.placed and not breakout.filled
    await a.execute("m", intent(size=0.1), START)
    target = await a.execute("t", intent(action="close", side="sell", order_type="limit", limit=119.0, size=0.5), START)
    assert target.placed and not target.filled
    done = {f.ref for f in await a.poll(START + 2500)}
    assert done == {"b", "t"}
    gap = await a.execute(
        "g", intent(action="close", side="sell", order_type="stop", stop=125.0, size=0.1), START + 2500
    )
    assert gap.filled and gap.price == 120.0  # a sell stop above the market is already triggered: fills at the market


@given(
    equity=st.floats(100, 1_000_000),
    price=st.floats(1_000, 200_000),
    frac=st.floats(0.01, 1.0),
)
def test_property_open_quantity_is_the_floored_fraction_of_equity_over_price(
    equity: float, price: float, frac: float
) -> None:
    a = SimulatedPaperExecutor("BINANCE:BTCUSDT", equity, flat_feed(price, START))
    got = asyncio.run(a.execute("e", intent(size=frac), START))
    want = (Decimal(str(frac)) * Decimal(str(equity)) / Decimal(str(price))).quantize(Decimal("1e-10"))
    want = (want / Decimal("0.00001")).to_integral_value(rounding=ROUND_DOWN) * Decimal("0.00001")
    if got.filled:
        # either the sized quantity, or less when the fee made it unaffordable at frac close to 1
        assert Decimal(got.qty or "0") <= want
        if frac < 0.99:
            assert Decimal(got.qty or "0") == want
    assert a.cash >= 0


async def test_nautilus_adapter_forwards_intents() -> None:
    class Inner:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.results: list[object] = []

        def submit(self, it: object) -> None:
            self.sent.append(it)

    inner = Inner()
    ex = NautilusPaperExecutor(inner)
    res = await ex.execute("e1", intent(), 5)
    assert res.placed and not res.filled and len(inner.sent) == 1
    assert await ex.poll(6) == [] and ex.snapshot(7) == {"submitted": 1, "results": 0}
