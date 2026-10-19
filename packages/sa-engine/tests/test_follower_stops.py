"""SEP-3.45 emulated stops and SEP-14.5 the disaster stop, on a Binance-spot-like backtest venue.

The venue (tests/spot_venue.py) denies STOP_MARKET like Nautilus' Binance spot client and rejects a market or limit
sell while a stop-limit rests (spot locks the base asset). Prices are scripted bar by bar; each bar also carries a
trade tick, the data the emulated stops trigger on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from nautilus_trader.model.enums import OrderSide, OrderType

from sa_engine.network.executor import Execution
from sa_engine.network.ledger import Ledger
from sa_engine.paper import STOP_EMULATION, FollowerExecutor
from sa_engine.protection import DISASTER_LIMIT_OFFSET, check_disaster_stop_pct
from tests.stop_rig import Action, Rig, intent, path, run_rig, send

ENTRY = intent()
STOP = intent(side="sell", action="close", order_type="stop", stop_price=49_000.0, size_fraction=1.0)
CLOSE = intent(side="sell", action="close", size_fraction=1.0)
HALF = intent(side="sell", action="close", size_fraction=0.5)
ADD = intent(action="adjust", size_fraction=0.1)
HELD = path((50_000, 12))  # the price never moves: no stop triggers
DROP = path((50_000, 6), (48_000, 6))  # falls through the signalled stop (49,000), not through the disaster stop


def sells(r: Rig) -> list[dict[str, object]]:
    assert r.venue is not None
    return [o for o in r.venue.submitted if o["side"] == OrderSide.SELL and o["type"] != OrderType.STOP_LIMIT]


# ---------------------------------------------------------------------------------------- SEP-3.45: emulated stops


def test_a_signalled_stop_is_held_locally_and_triggers_a_market_sell() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, DROP)
    assert r.venue is not None and r.venue.violations == []
    kinds = [(o["type"], o["side"]) for o in r.venue.submitted]
    assert kinds == [(OrderType.MARKET, OrderSide.BUY), (OrderType.MARKET, OrderSide.SELL)]
    assert r.end_position == 0
    reasons = [e.reason for e in r.by_ref("stop")]
    assert any("held locally" in x for x in reasons) and any("stop triggered" in x for x in reasons)
    assert r.by_ref("stop")[-1].filled and r.by_ref("stop")[-1].venue_order_id


def test_anti_the_exchange_never_receives_a_signalled_stop_price() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, DROP)
    assert r.venue is not None
    assert r.venue.submitted, "the venue saw the orders"
    for order in r.venue.submitted:
        assert order["type"] not in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
        assert order["trigger"] is None and order["price"] is None, order  # a market order carries no price at all
        assert "49000" not in str(order)
    assert all(not e.reason.startswith("denied") for e in r.events), "no UNSUPPORTED_ORDER_TYPE denial"


def test_the_stop_waits_locally_while_the_price_stays_above_it() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, HELD)
    assert r.venue is not None
    assert [o["type"] for o in r.venue.submitted] == [OrderType.MARKET]  # only the entry reached the venue
    assert r.end_position > 0 and r.end_working == 1  # the emulated stop is still held


def test_the_emulation_trigger_is_the_last_trade_price() -> None:
    from nautilus_trader.model.enums import TriggerType

    assert STOP_EMULATION is TriggerType.LAST_PRICE


def test_a_flat_position_cancels_the_held_stop() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")], 8: [send(CLOSE, "close")]}, HELD)
    assert r.end_position == 0 and r.end_working == 0
    assert r.by_ref("stop")[-1].reason == "cancelled"


def test_a_stop_buy_is_emulated_too_and_released_when_the_price_reaches_it() -> None:
    buy_stop = intent(order_type="stop", stop_price=51_000.0)
    r = run_rig({3: [send(buy_stop, "entry")]}, path((50_000, 5), (52_000, 5)))
    assert r.venue is not None
    assert [(o["type"], o["side"]) for o in r.venue.submitted] == [(OrderType.MARKET, OrderSide.BUY)]
    assert r.end_position > 0


# ---------------------------------------------------------------------------------------- SEP-14.5: the disaster stop


def disaster_orders(r: Rig) -> list[dict[str, object]]:
    assert r.venue is not None
    return [o for o in r.venue.submitted if o["type"] == OrderType.STOP_LIMIT]


def test_the_disaster_stop_is_placed_after_the_entry_fill_below_the_average_entry() -> None:
    r = run_rig({3: [send(ENTRY, "entry")]}, HELD, pct=10)
    (order,) = disaster_orders(r)
    (record,) = r.disaster_stops()
    assert r.venue is not None and r.venue.violations == []
    entry = r.by_ref("entry")[-1]
    assert entry.price is not None
    assert Decimal(str(order["trigger"])) == pytest.approx(
        Decimal(str(entry.price)) * Decimal("0.9"), abs=Decimal("0.01")
    )
    assert Decimal(str(order["price"])) == pytest.approx(
        Decimal(str(order["trigger"])) * (1 - DISASTER_LIMIT_OFFSET), abs=Decimal("0.01")
    )
    assert Decimal(str(order["price"])) < Decimal(str(order["trigger"]))
    assert order["side"] == OrderSide.SELL and Decimal(str(order["qty"])) == r.end_position == Decimal(str(entry.qty))
    assert (
        record.state == "working"
        and record.qty == entry.qty
        and Decimal(record.limit_price) == Decimal(str(order["price"]))
    )


def test_a_zero_percent_profile_sends_no_venue_order_at_all() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 8: [send(CLOSE, "close")]}, HELD, pct=0)
    assert disaster_orders(r) == [] and r.disaster_stops() == []
    assert r.venue is not None and r.venue.cancels == []
    assert r.end_position == 0


def test_the_disaster_stop_is_replaced_when_the_position_grows() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(ADD, "add")]}, HELD, pct=10)
    first, second = r.disaster_stops()
    assert first.state == "cancelled" and second.state == "working"
    assert Decimal(second.qty) == r.end_position > Decimal(first.qty)
    assert r.venue is not None and r.venue.violations == [] and r.observed == []
    assert r.end_resting == 1


def test_a_partial_close_cancels_first_then_sells_then_rests_a_stop_for_the_rest() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(HALF, "half")]}, HELD, pct=10)
    assert r.venue is not None and r.venue.violations == []
    first, second = r.disaster_stops()
    assert first.state == "cancelled" and second.state == "working"
    assert Decimal(second.qty) == r.end_position
    assert Decimal(first.qty) > Decimal(second.qty) > 0
    order_of_events = [("cancel", c.client_order_id) for c in r.venue.cancels]
    assert order_of_events, "the stop was cancelled before the sell"
    assert r.by_ref("half")[-1].filled


def test_closing_everything_cancels_the_disaster_stop_and_leaves_none() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(CLOSE, "close")]}, HELD, pct=10)
    assert r.venue is not None and r.venue.violations == []
    (record,) = r.disaster_stops()
    assert record.state == "cancelled" and r.end_position == 0
    assert r.end_resting == 0
    assert len(sells(r)) == 1


def test_the_market_sell_waits_for_the_cancel_confirmation() -> None:
    # the exchange holds the cancel: the sell must not be sent until the cancel is confirmed
    seen: dict[str, int] = {}

    def watch(r: Rig) -> None:
        assert r.venue is not None
        seen["sells_before_release"] = len(sells(r))
        seen["cancels_sent"] = len(r.venue.cancels)
        r.venue.hold_cancels = False
        r.venue.release_cancels()

    r = run_rig(
        {3: [send(ENTRY, "entry")], 7: [send(CLOSE, "close")], 8: [watch]},
        HELD,
        pct=10,
        hold_cancels=True,
    )
    assert seen == {"sells_before_release": 0, "cancels_sent": 1}
    assert r.venue is not None and r.venue.violations == []
    assert len(sells(r)) == 1 and r.end_position == 0 and r.by_ref("close")[-1].filled


def test_a_triggered_emulated_stop_cancels_the_disaster_stop_before_it_sells() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, DROP, pct=10)
    assert r.venue is not None and r.venue.violations == []
    (record,) = r.disaster_stops()
    assert record.state == "cancelled" and r.end_position == 0
    assert [(o["type"], o["side"]) for o in r.venue.submitted] == [
        (OrderType.MARKET, OrderSide.BUY),
        (OrderType.STOP_LIMIT, OrderSide.SELL),
        (OrderType.MARKET, OrderSide.SELL),
    ]
    assert r.by_ref("stop")[-1].filled


def test_a_failed_cancel_reports_the_unsent_close_and_leaves_the_stop_resting() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(CLOSE, "close")]}, HELD, pct=10, refuse_cancels=True)
    (record,) = r.disaster_stops()
    close = r.by_ref("close")
    assert close and not close[-1].placed
    assert "could not be cancelled" in close[-1].reason and "Unknown order" in close[-1].reason
    assert record.order_id is not None and record.order_id in close[-1].reason
    assert record.state == "working" and "cancel failed" in record.detail
    assert sells(r) == [] and r.end_position > 0
    assert [x.placed for x in r.results if x.ref == "close"] == [False]


def test_a_failed_cancel_reports_the_triggered_stop_that_could_not_be_sent() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, DROP, pct=10, refuse_cancels=True)
    last = r.by_ref("stop")[-1]
    assert not last.placed and "could not be cancelled" in last.reason
    assert sells(r) == []
    assert [x.placed for x in r.results if x.ref == "stop"] == [False]
    assert r.end_working == 1  # only the resting disaster stop: the released stop order was closed locally


def test_a_disaster_stop_that_triggers_at_the_exchange_closes_the_position_and_the_held_stop() -> None:
    r = run_rig({3: [send(ENTRY, "entry"), send(STOP, "stop")]}, path((50_000, 6), (44_900, 6)), pct=10)
    assert r.venue is not None and r.venue.violations == []
    # the held stop (49,000) triggers first, so the market sell goes out after the disaster stop is cancelled
    assert r.end_position == 0 and r.end_working == 0
    assert all(s.state in ("cancelled", "filled") for s in r.disaster_stops())


def test_a_disaster_stop_filled_by_the_exchange_is_recorded_and_the_flat_position_clears_reduce_only_orders() -> None:
    far = intent(side="sell", action="close", order_type="stop", stop_price=40_000.0, size_fraction=1.0)
    r = run_rig({3: [send(ENTRY, "entry"), send(far, "stop")]}, path((50_000, 6), (44_900, 6)), pct=10)
    (record,) = r.disaster_stops()
    assert record.state == "filled" and r.end_position == 0
    assert r.end_working == 0, "the held stop is cancelled once the position is flat"
    assert r.venue is not None and r.venue.violations == []


def test_a_close_held_for_the_cancel_is_reported_when_the_disaster_stop_fills_first() -> None:
    r = run_rig(
        {3: [send(ENTRY, "entry")], 5: [send(CLOSE, "close")]},
        path((50_000, 5), (44_900, 6)),
        pct=10,
        hold_cancels=True,  # the exchange has not confirmed the cancel when the stop triggers and fills
    )
    (record,) = r.disaster_stops()
    last = r.by_ref("close")[-1]
    assert record.state == "filled" and r.end_position == 0
    assert not last.placed and "already closed" in last.reason
    assert sells(r) == [] and r.venue is not None and r.venue.violations == []


def test_a_disaster_stop_the_exchange_refuses_is_reported_and_not_retried_in_a_loop() -> None:
    r = run_rig(
        {3: [send(ENTRY, "entry")], 6: [send(HALF, "half")]},
        HELD,
        pct=10,
        refuse=lambda o: "PERCENT_PRICE_BY_SIDE" if o.order_type == OrderType.STOP_LIMIT else None,
    )
    assert r.disaster_stops() and all(d.state == "rejected" for d in r.disaster_stops())
    assert "PERCENT_PRICE_BY_SIDE" in r.disaster_stops()[0].detail
    assert len(r.disaster_stops()) == 2, "one refusal per position size, not one per event"


def test_a_signalled_limit_sell_is_sent_after_the_disaster_stop_is_off_and_no_new_stop_rests_beside_it() -> None:
    take_profit = intent(side="sell", action="close", order_type="limit", limit_price=60_000.0, size_fraction=1.0)
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(take_profit, "tp")]}, HELD, pct=10)
    assert r.venue is not None and r.venue.violations == []
    assert len(r.disaster_stops()) == 1 and r.disaster_stops()[0].state == "cancelled"
    assert r.end_resting == 0
    assert [e.placed for e in r.by_ref("tp")][-1]


def test_a_disaster_stop_cancelled_from_outside_is_placed_again() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [lambda s: s.cancel_disaster_outside()]}, HELD, pct=10)
    first, second = r.disaster_stops()
    assert first.state == "cancelled" and second.state == "working" and r.observed == []


def test_the_disaster_stop_survives_a_latent_venue() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(HALF, "half")]}, HELD, pct=10, latency=True)
    assert r.venue is not None and r.venue.violations == [] and r.observed == []


# ---------------------------------------------------------------------------------------- ledger: placed agrees


def test_the_summary_placed_count_agrees_with_the_ledger_when_the_exchange_turns_an_order_down() -> None:
    refuse_limit = intent(order_type="limit", limit_price=2.0)  # the venue rejects this entry
    r = run_rig(
        {3: [send(ENTRY, "entry"), send(refuse_limit, "bad"), send(STOP, "stop")], 7: [send(CLOSE, "close")]},
        HELD,
        refuse=lambda o: "PRICE_FILTER" if o.order_type == OrderType.LIMIT else None,
    )
    ledger = Ledger(":memory:")
    for ref in ("entry", "bad", "stop", "close"):
        ledger.record(event_id=ref, provider="p", source="test", received_ms=1, status="accepted")
    for e in r.events:
        ledger.set_execution(e)
    from_ledger = sum(row.placed for row in ledger.rows())
    from_report = sum(1 for x in r.results if x.placed)
    assert from_report == from_ledger == 3
    assert not ledger.get("bad").placed  # type: ignore[union-attr]


def test_the_summary_lists_each_disaster_stop_with_its_venue_id_and_how_it_ended() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(CLOSE, "close")]}, HELD, pct=10)
    (record,) = r.disaster_stops()
    dumped = record.model_dump()
    assert dumped["order_id"] and dumped["state"] == "cancelled" and dumped["detail"]
    assert set(dumped) == {"order_id", "qty", "trigger_price", "limit_price", "state", "detail"}


# ---------------------------------------------------------------------------------------- property


def _actions(kind: str, i: int) -> Action:
    table = {
        "entry": send(ENTRY, f"entry-{i}"),
        "add": send(ADD, f"add-{i}"),
        "stop": send(STOP, f"stop-{i}"),
        "half": send(HALF, f"half-{i}"),
        "close": send(CLOSE, f"close-{i}"),
        "cancel_disaster": lambda s: s.cancel_disaster_outside(),
    }
    return table[kind]


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    steps=st.lists(
        st.tuples(
            st.integers(1, 14),
            st.sampled_from(["entry", "add", "stop", "half", "close", "cancel_disaster"]),
        ),
        min_size=1,
        max_size=10,
    ),
    prices=st.lists(st.sampled_from([50_000.0, 48_500.0, 44_000.0, 52_000.0]), min_size=16, max_size=16),
    latency=st.booleans(),
)
def test_property_at_most_one_disaster_stop_sized_to_the_position_and_no_sell_beside_it(
    steps: list[tuple[int, str]], prices: list[float], latency: bool
) -> None:
    script: dict[int, list[Action]] = {}
    for i, (bar, kind) in enumerate(steps):
        script.setdefault(bar, []).append(_actions(kind, i))
    r = run_rig(script, prices, pct=10, latency=latency)
    assert r.venue is not None
    # no market (or limit) sell is submitted while a disaster stop rests, and a second stop never rests beside one
    assert r.venue.violations == []
    # at every settled bar: at most one rests, its quantity is the position, none rests when flat
    assert r.observed == []
    # at the end
    assert r.end_resting <= 1
    if r.end_position <= 0:
        assert r.end_resting == 0
    elif r.end_resting and r.disaster_stops()[-1].state == "working":
        assert Decimal(r.disaster_stops()[-1].qty) == r.end_position
    # every record that is not working has ended, and at most the last one is working
    states = [d.state for d in r.disaster_stops()]
    assert all(s != "working" for s in states[:-1])


def test_the_execution_events_for_a_disaster_stop_never_reach_the_ledger_as_a_signal() -> None:
    r = run_rig({3: [send(ENTRY, "entry")], 7: [send(CLOSE, "close")]}, HELD, pct=10)
    refs = {e.ref for e in r.events}
    assert refs == {"entry", "close"}, refs
    assert all(isinstance(e, Execution) for e in r.events)


@pytest.mark.parametrize("pct", [-1.0, 50.5, 100.0, float("nan"), float("inf")])
def test_a_percent_outside_zero_to_fifty_is_refused_before_a_node_exists(pct: float) -> None:
    with pytest.raises(ValueError, match=r"disaster stop percent .* outside \(0, 50\].*0 to turn it off"):
        check_disaster_stop_pct(pct)
    with pytest.raises(ValueError, match="outside"):
        FollowerExecutor("BINANCE:BTCUSDT", 1_000, disaster_stop_pct=pct)


@given(pct=st.floats(allow_nan=True, allow_infinity=True))
def test_property_check_disaster_stop_pct_accepts_exactly_zero_and_the_half_open_range(pct: float) -> None:
    if pct == 0 or 0 < pct <= 50:
        assert check_disaster_stop_pct(pct) == pct
    else:
        with pytest.raises(ValueError, match="outside"):
            check_disaster_stop_pct(pct)
