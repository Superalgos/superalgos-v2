"""SEP-5.4: orders have create and cancel events and can re-arm, so a 10-level ladder is one strategy whose backtest
re-places filled levels."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine.brain import Cancel, PositionState, StrategyBrain
from tests.f4_helpers import backtest_on, bar, make_brain, submits, when

FLAT = PositionState()
ROOT = Path(__file__).resolve().parents[1]


def slot_state(b: StrategyBrain) -> str:
    return str(b.slots["l"].state)


def one_order(**order: Any) -> StrategyBrain:
    return make_brain(
        {"open": {"when": when("true"), "orders": [{"id": "l", "type": "limit", "size_fraction": 0.2, **order}]}}
    )


def held(qty: float = 1.0) -> PositionState:
    return PositionState(qty, 100.0)


# --------------------------------------------------------------------------- create


def test_create_event_decides_when_an_order_is_placed_and_the_cycle_waits_for_it() -> None:
    b = one_order(create=when("close < 95"))
    assert submits(b.on_bar(bar(0, 100), FLAT)) == []
    assert submits(b.on_bar(bar(1, 100), FLAT)) == []
    assert b.in_cycle and slot_state(b) == "idle"  # still waiting, the cycle did not end
    (cmd,) = submits(b.on_bar(bar(2, 90), FLAT))
    assert cmd.tag == "l#1" and slot_state(b) == "working"
    assert [f.event for f in b.log if f.event.startswith("order:")] == ["order:l.create"]


def test_without_create_the_order_is_placed_with_the_cycle() -> None:
    assert [c.tag for c in submits(one_order().on_bar(bar(0, 100), FLAT))] == ["l#1"]


# --------------------------------------------------------------------------- re-arm


def test_a_filled_order_that_re_arms_is_placed_again_by_its_create_event() -> None:
    b = one_order(rearm=True, create=when("close > 0"))
    (first,) = submits(b.on_bar(bar(0, 100), FLAT))
    assert b.on_fill(first.tag, 99, 1.0, held(), 1, done=True) == []
    assert slot_state(b) == "idle"
    (second,) = submits(b.on_bar(bar(1, 100), held()))
    assert second.tag == "l#1.2" and second.intent.order_type == "limit"
    b.on_fill(second.tag, 99, 1.0, held(2.0), 2, done=True)
    (third,) = submits(b.on_bar(bar(2, 100), held(2.0)))
    assert third.tag == "l#1.3"
    assert b.cycle == 1  # all within one cycle: the ladder never has to be "restarted"


def test_without_rearm_a_filled_order_is_not_placed_again() -> None:
    b = one_order(create=when("close > 0"))
    (first,) = submits(b.on_bar(bar(0, 100), FLAT))
    b.on_fill(first.tag, 99, 1.0, held(), 1, done=True)
    assert slot_state(b) == "done"
    assert submits(b.on_bar(bar(1, 100), held())) == []


def test_partial_fills_keep_the_order_working_until_it_is_done() -> None:
    b = one_order(rearm=True, create=when("true"))
    (first,) = submits(b.on_bar(bar(0, 100), FLAT))
    b.on_fill(first.tag, 99, 0.5, held(0.5), 1, done=False)
    assert slot_state(b) == "working" and submits(b.on_bar(bar(1, 100), held(0.5))) == []
    b.on_fill(first.tag, 99, 0.5, held(1.0), 2, done=True)
    assert slot_state(b) == "idle"


def test_a_ladder_level_re_places_at_the_then_current_price() -> None:
    orders = [
        {
            "id": f"l{n}",
            "type": "limit",
            "size_fraction": 0.09,
            "rate": f"close - {n}",
            "rearm": True,
            "create": when("true"),
            "take_profit_pct": 1,
        }
        for n in range(1, 11)
    ]
    b = make_brain({"open": {"when": when("true"), "orders": orders}})
    placed = submits(b.on_bar(bar(0, 100), FLAT))
    assert [c.intent.limit_price for c in placed] == [99.0 - n + 1 for n in range(1, 11)]
    fill = b.on_fill("l3#1", 97.0, 0.9, held(0.9), 1, done=True)
    (tp,) = submits(fill)
    assert tp.intent.order_type == "limit" and tp.intent.limit_price == 97.97
    b.on_fill(tp.tag, 98.97, 0.9, FLAT, 2, done=True)
    again = submits(b.on_bar(bar(1, 120), FLAT))
    assert [(c.tag, c.intent.limit_price) for c in again] == [("l3#1.2", 117.0)]  # only level 3, at 120 - 3


# --------------------------------------------------------------------------- cancel


def test_cancel_event_cancels_a_working_order_and_the_slot_follows_rearm() -> None:
    b = one_order(rearm=True, create=when("close > 90"), cancel=when("close > 110"))
    (cmd,) = submits(b.on_bar(bar(0, 100), FLAT))
    assert b.on_bar(bar(1, 105), FLAT) == []
    assert b.on_bar(bar(2, 120), FLAT) == [Cancel(cmd.tag)]
    b.on_order_end(cmd.tag)
    assert slot_state(b) == "idle"
    (again,) = submits(b.on_bar(bar(3, 120), FLAT))  # create is true again: the same level is placed again
    assert again.tag == "l#1.2"
    assert b.on_bar(bar(4, 120), FLAT) == [Cancel(again.tag)]  # and the cancel event applies to it too

    n = one_order(create=when("close > 90"), cancel=when("close > 110"))  # no rearm: cancelled for the cycle
    (c2,) = submits(n.on_bar(bar(0, 100), FLAT))
    assert n.on_bar(bar(1, 120), FLAT) == [Cancel(c2.tag)]
    n.on_order_end(c2.tag)
    assert slot_state(n) == "done"
    (next_cycle,) = submits(n.on_bar(bar(2, 100), FLAT))  # the cycle ended; open.when starts the next one
    assert next_cycle.tag == "l#2"


def test_cancel_is_evaluated_only_while_the_order_is_working() -> None:
    b = one_order(create=when("close < 50"), cancel=when("true"))
    assert b.on_bar(bar(0, 100), FLAT) == []
    assert [f.event for f in b.log if f.event.endswith("cancel")] == []


# --------------------------------------------------------------------------- safety


def test_total_exposure_never_exceeds_the_equity_however_often_a_level_re_arms() -> None:
    b = one_order(rearm=True, create=when("true"), size_fraction=0.4, type="market")
    equity = 1000.0
    pos = FLAT
    total = 0.0
    for i in range(6):
        placed = submits(b.on_bar(bar(i, 100), pos, equity))
        if not placed:
            continue
        (cmd,) = placed
        total += cmd.intent.size_fraction
        assert total <= 1.0 + 1e-9
        qty = cmd.intent.size_fraction * equity / 100
        pos = PositionState(pos.qty + qty, 100.0)
        b.on_fill(cmd.tag, 100, qty, pos, i, done=True)
    assert total == pytest.approx(1.0)  # 0.4 + 0.4 + the 0.2 that is left, then nothing


def test_refused_orders_are_not_re_armed_so_a_dust_size_cannot_loop() -> None:
    b = one_order(rearm=True, create=when("true"))
    (cmd,) = submits(b.on_bar(bar(0, 100), FLAT))
    b.on_order_end(cmd.tag, rejected=True)
    assert slot_state(b) == "done"
    (next_cycle,) = submits(b.on_bar(bar(1, 100), FLAT))  # not re-armed: the ladder only restarts as a new cycle
    assert next_cycle.tag == "l#2" and b.cycle == 2


def test_trigger_off_close_and_cancel_pending_stop_a_ladder_from_re_arming() -> None:
    stages: dict[str, Any] = {
        "trigger": {"on": when("true"), "off": when("close > 200")},
        "open": {"when": when("true"), "orders": [{"id": "l", "type": "limit", "size_fraction": 0.2, "rearm": True}]},
        "close": {"when": when("close > 300")},
        "manage": {"rules": [{"id": "stop_new", "when": when("close > 150"), "action": "cancel_pending"}]},
    }
    for price in (160, 210, 310):
        b = make_brain(copy.deepcopy(stages))
        (cmd,) = submits(b.on_bar(bar(0, 100), FLAT))
        b.on_fill(cmd.tag, 100, 1.0, held(), 1, done=True)
        b.on_bar(bar(1, price), held())  # the rule / off / close fires
        assert b.entries_closed
        assert submits(b.on_bar(bar(2, 100), held())) == []  # even when the price is fine again


def test_the_cycle_ends_only_when_flat_with_nothing_working_and_nothing_left_to_place() -> None:
    b = make_brain(
        {
            "open": {
                "when": when("true"),
                "orders": [
                    {"id": "a", "type": "limit", "size_fraction": 0.2},
                    {"id": "b", "type": "limit", "size_fraction": 0.2, "create": when("close < 50")},
                ],
            }
        }
    )
    (a,) = submits(b.on_bar(bar(0, 100), FLAT))
    b.on_order_end(a.tag)
    assert submits(b.on_bar(bar(1, 100), FLAT)) == [] and b.cycle == 1  # b is still to be placed
    assert [c.tag for c in submits(b.on_bar(bar(2, 40), FLAT))] == ["b#1"]


# --------------------------------------------------------------------------- the example, in a backtest


def ladder_doc(rearm: bool) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((ROOT / "examples" / "ladder_rearm.json").read_text())
    for o in doc["stages"]["open"]["orders"]:
        o["rearm"] = rearm
    return doc


def test_ten_level_ladder_re_places_filled_levels_in_one_cycle(
    fixture_catalog: ParquetDataCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = backtest_on(ladder_doc(True), fixture_catalog, monkeypatch)
    placements = Counter(f.event for f in run.brain.log if f.event.startswith("order:") and f.event.endswith(".create"))
    assert len(placements) == 10
    assert max(placements.values()) >= 3  # a level is placed, filled, sold and placed again
    buys = [f for f in run.report.fills if f.side == "buy"]
    assert len(buys) > 10  # more entry fills than the ten levels
    assert run.brain.cycle == 1  # one cycle, never restarted: the ladder re-armed itself
    assert run.report.rejections == [] and run.report.trade_count > 10


def test_without_rearm_the_same_ladder_places_each_level_once_per_cycle(
    fixture_catalog: ParquetDataCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = backtest_on(ladder_doc(False), fixture_catalog, monkeypatch)
    placements = Counter(f.event for f in run.brain.log if f.event.endswith(".create"))
    assert max(placements.values()) <= run.brain.cycle
    assert run.brain.cycle > 1  # without re-arming the ladder is rebuilt by new cycles instead (v1 "grid reset")
    rearmed = backtest_on(ladder_doc(True), fixture_catalog, monkeypatch)
    assert rearmed.brain.cycle == 1 and run.report.fingerprint() != rearmed.report.fingerprint()
