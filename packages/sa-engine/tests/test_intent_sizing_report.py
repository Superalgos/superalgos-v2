from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from sa_engine.intent import OrderIntent, from_nautilus_id, normalize_instrument, to_nautilus_id
from sa_engine.report import Fill, match_trades, max_drawdown
from sa_engine.sizing import floor_to, size_intent

INC, MIN = Decimal("0.000001"), Decimal("0.000001")


def intent(**kw: object) -> OrderIntent:
    base: dict[str, object] = {
        "instrument": "BINANCE:BTCUSDT",
        "side": "buy",
        "action": "open",
        "size_fraction": 0.5,
        "order_type": "market",
        "timeframe": 60,
        "ts": 1,
    }
    return OrderIntent.model_validate({**base, **kw})


# --------------------------------------------------------------------------- intent


def test_intent_price_rules() -> None:
    assert intent(order_type="limit", limit_price=100.0).limit_price == 100.0
    assert intent(order_type="stop", stop_price=90.0).stop_price == 90.0
    for bad in (
        {"order_type": "market", "limit_price": 1.0},
        {"order_type": "market", "stop_price": 1.0},
        {"order_type": "limit"},
        {"order_type": "stop"},
        {"size_fraction": 0},
        {"size_fraction": 1.5},
        {"instrument": "btcusdt"},
        {"timeframe": 0},
        {"extra_field": 1},
    ):
        with pytest.raises(ValidationError):
            intent(**bad)


def test_intent_is_frozen_and_json_roundtrips() -> None:
    i = intent(order_type="limit", limit_price=12.5)
    with pytest.raises(ValidationError):
        i.size_fraction = 0.9  # type: ignore[misc]
    assert OrderIntent.model_validate_json(i.model_dump_json()) == i


def test_instrument_id_conversions() -> None:
    assert to_nautilus_id("BINANCE:BTCUSDT") == "BTCUSDT.BINANCE"
    assert from_nautilus_id("BTCUSDT.BINANCE") == "BINANCE:BTCUSDT"
    assert normalize_instrument("btcusdt") == "BINANCE:BTCUSDT"
    assert normalize_instrument("BTCUSDT.BINANCE") == "BINANCE:BTCUSDT"
    assert normalize_instrument("BINANCE:ETHUSDT") == "BINANCE:ETHUSDT"
    for bad in ("BTCUSDT", ":X", "X:"):
        with pytest.raises(ValueError, match="VENUE:SYMBOL"):
            to_nautilus_id(bad)
    for bad in ("BTCUSDT", ".BINANCE", "BTCUSDT."):
        with pytest.raises(ValueError, match=r"SYMBOL\.VENUE"):
            from_nautilus_id(bad)
    with pytest.raises(ValueError, match="not a symbol"):
        normalize_instrument("BTC USDT")


# --------------------------------------------------------------------------- sizing


def sized(i: OrderIntent, **kw: float) -> Decimal | None:
    args = {"equity": 10_000.0, "position_qty": 0.0, "ref_price": 50_000.0} | kw
    return size_intent(i, size_increment=INC, min_quantity=MIN, **args).quantity


def test_open_is_fraction_of_equity_over_price() -> None:
    assert sized(intent(size_fraction=0.5)) == Decimal("0.1")
    assert sized(intent(size_fraction=0.5, order_type="limit", limit_price=25_000.0)) == Decimal("0.2")
    assert sized(intent(size_fraction=0.5, order_type="stop", stop_price=100_000.0)) == Decimal("0.05")


def test_close_is_fraction_of_position() -> None:
    close = intent(side="sell", action="close", size_fraction=0.25)
    assert sized(close, position_qty=0.4) == Decimal("0.1")
    assert sized(intent(side="sell", action="close", size_fraction=1.0), position_qty=0.0123456) == Decimal("0.012345")
    assert sized(intent(side="sell", action="adjust", size_fraction=0.5), position_qty=1.0) == Decimal("0.5")
    assert sized(intent(side="buy", action="adjust", size_fraction=0.1)) == Decimal("0.02")


def test_unsizable_intents_explain_why() -> None:
    res = size_intent(
        intent(side="sell", action="open"), equity=1, position_qty=0, ref_price=1, size_increment=INC, min_quantity=MIN
    )
    assert res.quantity is None and "short selling" in res.reason
    res = size_intent(
        intent(side="sell", action="close"), equity=1, position_qty=0, ref_price=1, size_increment=INC, min_quantity=MIN
    )
    assert res.quantity is None and "nothing to close" in res.reason
    res = size_intent(intent(), equity=0, position_qty=0, ref_price=1, size_increment=INC, min_quantity=MIN)
    assert res.quantity is None and "cannot size" in res.reason
    res = size_intent(
        intent(size_fraction=0.0001), equity=10, position_qty=0, ref_price=50_000, size_increment=INC, min_quantity=MIN
    )
    assert res.quantity is None and "below the minimum" in res.reason
    with pytest.raises(ValueError, match="size increment must be positive"):
        floor_to(Decimal(1), Decimal(0))


def test_float_noise_does_not_lose_an_increment() -> None:
    # 0.0096569999999 must size to 0.009657 not 0.009656
    res = size_intent(
        intent(side="sell", action="close", size_fraction=0.009657 / 0.5),
        equity=1,
        position_qty=0.5,
        ref_price=1,
        size_increment=INC,
        min_quantity=MIN,
    )
    assert res.quantity == Decimal("0.009657")


@given(
    st.floats(min_value=0.001, max_value=1.0),
    st.floats(min_value=100, max_value=1e6),
    st.floats(min_value=1_000, max_value=1e5),
)
@settings(max_examples=100, deadline=None)
def test_open_notional_never_exceeds_the_budget(frac: float, equity: float, price: float) -> None:
    q = sized(intent(size_fraction=frac), equity=equity, ref_price=price)
    if q is not None:
        assert float(q) * price <= frac * equity * (1 + 1e-9)
        assert q % INC == 0


@given(st.floats(min_value=0.001, max_value=1.0), st.floats(min_value=0.0001, max_value=100))
@settings(max_examples=100, deadline=None)
def test_close_never_exceeds_position(frac: float, pos: float) -> None:
    q = sized(intent(side="sell", action="close", size_fraction=frac), position_qty=pos)
    if q is not None:
        assert float(q) <= pos + 1e-9


# --------------------------------------------------------------------------- report


def test_match_trades_fifo_with_fees() -> None:
    fills = [
        Fill(1, "buy", 1.0, 100.0, 0.1),
        Fill(2, "buy", 1.0, 110.0, 0.11),
        Fill(3, "sell", 1.5, 120.0, 0.18),
    ]
    trades, open_qty = match_trades(fills)
    assert open_qty == pytest.approx(0.5)
    assert len(trades) == 2
    t0, t1 = trades
    assert (t0.qty, t0.entry_price, t0.exit_price) == (1.0, 100.0, 120.0)
    assert t0.fees == pytest.approx(0.1 + 1.0 * (0.18 / 1.5))
    assert t0.pnl == pytest.approx(20.0 - t0.fees)
    assert t1.qty == pytest.approx(0.5) and t1.entry_price == 110.0
    assert t1.pnl == pytest.approx(0.5 * 10.0 - t1.fees)


def test_match_trades_ignores_empty_fills_and_orphan_sells() -> None:
    trades, open_qty = match_trades([Fill(1, "buy", 0.0, 1, 0), Fill(2, "sell", 1.0, 1, 0)])
    assert trades == [] and open_qty == 0


@given(
    st.lists(
        st.tuples(st.sampled_from(["buy", "sell"]), st.floats(0.01, 10), st.floats(1, 1000), st.floats(0, 1)),
        min_size=0,
        max_size=40,
    )
)
@settings(max_examples=100, deadline=None)
def test_matched_trades_never_sell_more_than_bought(raw: list[tuple[str, float, float, float]]) -> None:
    fills = [Fill(i, side, q, px, fee) for i, (side, q, px, fee) in enumerate(raw)]
    trades, open_qty = match_trades(fills)
    bought = sum(f.qty for f in fills if f.side == "buy")
    assert sum(t.qty for t in trades) + open_qty == pytest.approx(bought)
    assert open_qty >= -1e-9
    assert all(t.qty > 0 and t.closed_ms >= t.opened_ms for t in trades)


def test_max_drawdown() -> None:
    assert max_drawdown([]) == (0.0, 0.0)
    assert max_drawdown([(0, 100.0), (1, 120.0), (2, 90.0), (3, 130.0), (4, 110.0)]) == (30.0, 25.0)
    assert max_drawdown([(0, 1.0), (1, 2.0), (2, 3.0)]) == (0.0, 0.0)


@given(st.lists(st.floats(0.01, 1e6), min_size=1, max_size=100))
@settings(max_examples=100, deadline=None)
def test_drawdown_bounds(values: list[float]) -> None:
    dd_abs, dd_pct = max_drawdown(list(enumerate(values)))
    assert 0 <= dd_pct <= 100
    assert 0 <= dd_abs <= max(values)
