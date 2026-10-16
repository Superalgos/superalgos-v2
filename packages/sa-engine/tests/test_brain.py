from __future__ import annotations

from typing import Any

from sa_engine.brain import Cancel, Command, PositionState, StrategyBrain, Submit
from sa_engine.indicators import OHLCV
from sa_engine.schema import validate_strategy
from tests.strategies import ev

FLAT = PositionState()


def bar(i: int, close: float) -> OHLCV:
    return OHLCV(i * 60_000, close, close, close, close, 1.0)


def when(*conditions: str) -> dict[str, object]:
    """One situation: all ``conditions`` (AND)."""
    return ev(("s", list(conditions)))


def make(stages: dict[str, Any], tick: float | None = 0.01) -> StrategyBrain:
    doc = validate_strategy({"schema_version": 2, "name": "t", "timeframe": "1m", "stages": stages})
    return StrategyBrain(doc, "BINANCE:BTCUSDT", price_precision=2, tick=tick)


def submits(cmds: list[Command]) -> list[Submit]:
    return [c for c in cmds if isinstance(c, Submit)]


def test_entry_emits_all_orders_once_then_waits_for_the_cycle_to_end() -> None:
    b = make(
        {
            "open": {
                "when": when("true"),
                "orders": [{"id": "a", "size_fraction": 0.4}, {"id": "b", "size_fraction": 0.4}],
            }
        }
    )
    cmds = b.on_bar(bar(0, 100), FLAT)
    assert [c.tag for c in submits(cmds)] == ["a#1", "b#1"]
    assert all(c.intent.action == "open" and c.intent.side == "buy" and not c.reduce_only for c in submits(cmds))
    assert b.on_bar(bar(1, 100), FLAT) == []  # orders still working: no second cycle
    b.on_order_end("a#1")
    assert b.on_bar(bar(2, 100), FLAT) == []
    b.on_order_end("b#1")
    assert [c.tag for c in submits(b.on_bar(bar(3, 100), FLAT))] == ["a#2", "b#2"]  # idle again: new cycle


def test_trigger_on_off_gates_entries_and_cancels_pending() -> None:
    b = make(
        {
            "trigger": {"on": when("close > 100"), "off": when("close > 120")},
            "open": {
                "when": when("true"),
                "orders": [{"id": "a", "type": "limit", "size_fraction": 0.5, "offset_pct": -1}],
            },
        }
    )
    assert b.on_bar(bar(0, 90), FLAT) == []  # not triggered
    cmds = b.on_bar(bar(1, 110), FLAT)
    assert len(submits(cmds)) == 1
    off = b.on_bar(bar(2, 130), FLAT)
    assert off == [Cancel("a#1")]
    b.on_order_end("a#1")
    assert b.on_bar(bar(3, 90), FLAT) == []  # deactivated: no new entries


def test_limit_and_stop_prices_are_offsets_from_the_close_rounded_to_tick() -> None:
    b = make(
        {
            "open": {
                "when": when("true"),
                "orders": [
                    {"id": "lo", "type": "limit", "size_fraction": 0.3, "offset_pct": -1.2345},
                    {"id": "st", "type": "stop", "size_fraction": 0.3, "offset_pct": 2},
                ],
            }
        },
        tick=0.5,
    )
    lo, st = submits(b.on_bar(bar(0, 100), FLAT))
    assert lo.intent.limit_price == 99.0 and lo.intent.order_type == "limit"
    assert st.intent.stop_price == 102.0 and st.intent.order_type == "stop"
    assert make(
        {"open": {"when": when("true"), "orders": [{"id": "m", "size_fraction": 1}]}}, tick=None
    )  # no tick: decimals


def test_per_order_create_event_waits_until_it_fires() -> None:
    b = make(
        {
            "open": {
                "when": when("true"),
                "orders": [
                    {"id": "always", "size_fraction": 0.3},
                    {"id": "dip", "size_fraction": 0.3, "create": when("close < 50")},
                ],
            }
        }
    )
    assert [c.tag for c in submits(b.on_bar(bar(0, 100), FLAT))] == ["always#1"]
    b.on_order_end("always#1")
    assert submits(b.on_bar(bar(1, 100), FLAT)) == []  # "dip" is still waiting for its create event: cycle goes on
    assert [c.tag for c in submits(b.on_bar(bar(2, 40), FLAT))] == ["dip#1"]


def test_fill_spawns_oco_take_profit_and_stop_that_cancel_each_other() -> None:
    b = make(
        {
            "open": {
                "when": when("true"),
                "orders": [{"id": "e", "size_fraction": 0.5, "take_profit_pct": 1, "stop_loss_pct": 2}],
            }
        }
    )
    b.on_bar(bar(0, 100), FLAT)
    pos = PositionState(2.0, 100.0)
    cmds = b.on_fill("e#1", 100.0, 2.0, pos, 1, done=True)
    tp, sl = submits(cmds)
    assert (tp.intent.order_type, tp.intent.limit_price, tp.reduce_only) == ("limit", 101.0, True)
    assert (sl.intent.order_type, sl.intent.stop_price, sl.reduce_only) == ("stop", 98.0, True)
    assert tp.intent.action == sl.intent.action == "close" and tp.intent.side == "sell"
    assert tp.intent.size_fraction == 1.0
    # take profit fills -> the stop is cancelled
    assert b.on_fill(tp.tag, 101.0, 2.0, FLAT, 2, done=True) == [Cancel(sl.tag)]
    b.on_order_end(sl.tag)
    assert b.pending == {}


def test_partial_fills_size_each_exit_to_the_filled_part() -> None:
    b = make(
        {
            "open": {
                "when": when("true"),
                "orders": [{"id": "e", "type": "limit", "size_fraction": 1, "take_profit_pct": 1}],
            }
        }
    )
    b.on_bar(bar(0, 100), FLAT)
    first = b.on_fill("e#1", 99.0, 1.0, PositionState(1.0, 99.0), 1, done=False)
    assert submits(first)[0].intent.size_fraction == 1.0
    second = b.on_fill("e#1", 98.0, 1.0, PositionState(2.0, 98.5), 2, done=True)
    assert submits(second)[0].intent.size_fraction == 0.5  # 1 of the 2 now held
    assert "e#1" not in b.pending


def test_unknown_tags_and_fills_without_position_are_ignored() -> None:
    b = make({"open": {"when": when("true"), "orders": [{"id": "e", "size_fraction": 1, "take_profit_pct": 1}]}})
    assert b.on_fill("nope", 1, 1, FLAT, 1, done=True) == []
    b.on_bar(bar(0, 100), FLAT)
    assert b.on_fill("e#1", 100, 1, FLAT, 1, done=True) == []  # position already gone: nothing to protect


def test_close_stage_cancels_everything_and_flattens_once() -> None:
    b = make(
        {
            "open": {
                "when": when("close < 100"),
                "orders": [
                    {"id": "e", "size_fraction": 0.5},
                    {"id": "g", "type": "limit", "size_fraction": 0.4, "offset_pct": -5},
                ],
            },
            "close": {"when": when("close > 120")},
        }
    )
    b.on_bar(bar(0, 90), FLAT)
    b.on_fill("e#1", 90, 1.0, PositionState(1.0, 90.0), 1, done=True)
    cmds = b.on_bar(bar(1, 130), PositionState(1.0, 90.0))
    assert Cancel("g#1") in cmds
    (close,) = submits(cmds)
    assert close.intent.action == "close" and close.intent.size_fraction == 1.0 and close.reduce_only
    # the close order is pending: a second bar does not send another one
    assert submits(b.on_bar(bar(2, 130), PositionState(1.0, 90.0))) == []
    b.on_fill(close.tag, 130, 1.0, FLAT, 3, done=True)
    b.on_order_end("g#1")


def test_close_with_only_pending_orders_just_cancels() -> None:
    b = make(
        {
            "open": {
                "when": when("close < 100"),
                "orders": [{"id": "g", "type": "limit", "size_fraction": 0.4, "offset_pct": -5}],
            },
            "close": {"when": when("close > 120")},
        }
    )
    b.on_bar(bar(0, 90), FLAT)
    assert b.on_bar(bar(1, 130), FLAT) == [Cancel("g#1")]


def test_manage_rules_fire_once_per_cycle_unless_repeating() -> None:
    stages = {
        "open": {"when": when("true"), "orders": [{"id": "e", "size_fraction": 0.5}]},
        "manage": {
            "rules": [
                {"id": "trim", "when": when("unrealized_pnl_pct > 5"), "action": "reduce", "size_fraction": 0.5},
                {
                    "id": "again",
                    "when": when("unrealized_pnl_pct > 5"),
                    "action": "reduce",
                    "size_fraction": 0.1,
                    "once": False,
                },
            ]
        },
    }
    b = make(stages)
    b.on_bar(bar(0, 100), FLAT)
    b.on_fill("e#1", 100, 1.0, PositionState(1.0, 100.0), 1, done=True)
    pos = PositionState(1.0, 100.0)
    first = submits(b.on_bar(bar(1, 110), pos))
    assert [(c.intent.size_fraction, c.intent.action, c.reduce_only) for c in first] == [
        (0.5, "close", True),
        (0.1, "close", True),
    ]
    second = submits(b.on_bar(bar(2, 111), pos))
    assert [c.intent.size_fraction for c in second] == [0.1]  # only the repeating rule fires again


def test_cancel_pending_rule_and_noops() -> None:
    b = make(
        {
            "open": {
                "when": when("true"),
                "orders": [{"id": "g", "type": "limit", "size_fraction": 0.5, "offset_pct": -10}],
            },
            "manage": {
                "rules": [
                    {"id": "c", "when": when("bars_in_position >= 0"), "action": "cancel_pending"},
                    {"id": "r", "when": when("true"), "action": "reduce", "size_fraction": 0.5},
                ]
            },
        }
    )
    b.on_bar(bar(0, 100), FLAT)
    cmds = b.on_bar(bar(1, 100), FLAT)
    assert cmds == [Cancel("g#1")]  # reduce is a no-op without a position
    b.on_order_end("g#1")
    b.on_order_end("g#1")  # idempotent
    assert b.on_bar(bar(2, 100), FLAT)  # idle again -> a new cycle starts


def test_position_variables_in_conditions() -> None:
    b = make(
        {
            "open": {"when": when("not in_position"), "orders": [{"id": "e", "size_fraction": 0.5}]},
            "close": {
                "when": when(
                    "bars_in_position >= 2 and entry_price == 100 and position_size > 0 and is_long and not is_short"
                )
            },
        }
    )
    b.on_bar(bar(0, 100), FLAT)
    b.on_fill("e#1", 100, 1.0, PositionState(1.0, 100.0), 1, done=True)
    pos = PositionState(1.0, 100.0)
    assert submits(b.on_bar(bar(1, 100), pos)) == []  # bars_in_position == 1
    assert len(submits(b.on_bar(bar(2, 100), pos))) == 1


def test_dust_counts_as_flat() -> None:
    doc = validate_strategy(
        {
            "name": "t",
            "timeframe": "1m",
            "stages": {"open": {"when": when("true"), "orders": [{"id": "e", "size_fraction": 0.5}]}},
        }
    )
    b = StrategyBrain(doc, "BINANCE:BTCUSDT", dust=0.001)
    b.on_bar(bar(0, 100), FLAT)
    b.on_order_end("e#1")
    assert len(submits(b.on_bar(bar(1, 100), PositionState(0.0005, 100.0)))) == 1  # dust does not block a new cycle


def test_indicators_warm_up_inside_the_brain() -> None:
    b = make({"open": {"when": when("close > sma(close, 3)"), "orders": [{"id": "e", "size_fraction": 0.5}]}})
    assert b.on_bar(bar(0, 1), FLAT) == [] and b.on_bar(bar(1, 2), FLAT) == []  # sma not ready: comparison false
    assert len(submits(b.on_bar(bar(2, 9), FLAT))) == 1
