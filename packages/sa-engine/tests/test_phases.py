"""SEP-5.3: the manage stage holds ordered phases for stop and target, each with a formula and next-phase /
move-to-phase events. v1 order: the current phase's next event, else its move-to events in array
order, at most one move per bar, then the formula of the phase that is current afterwards."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from sa_engine.brain import Cancel, PositionState, StrategyBrain
from tests.f4_helpers import MINUTE, bar, make_brain, submits, when

HELD = PositionState(1.0, 100.0)
FLAT = PositionState()


def entered(manage: dict[str, Any], **stages: Any) -> StrategyBrain:
    """A brain that has bought 1.0 at 100 (bar 0 decided, filled at its close)."""
    b = make_brain(
        {
            "open": {"when": when("not in_position"), "orders": [{"id": "e", "size_fraction": 0.5}]},
            "manage": manage,
            **stages,
        }
    )
    b.on_bar(bar(0, 100), FLAT)
    b.on_fill("e#1", 100.0, 1.0, HELD, 0, done=True)
    return b


def levels(b: StrategyBrain) -> tuple[float | None, float | None]:
    return (b.stop.value if b.stop else None, b.target.value if b.target else None)


def phase_ids(b: StrategyBrain, which: str = "stop") -> str:
    lv = b.stop if which == "stop" else b.target
    assert lv is not None
    return lv.phases[lv.index].id


def test_phase_one_is_current_when_the_position_opens_and_its_formula_runs_on_the_next_close() -> None:
    b = entered({"stop": {"phases": [{"id": "a", "level": "entry_price * 0.95"}]}})
    assert levels(b) == (None, None)  # nothing is armed between the fill and the next bar close
    b.on_bar(bar(1, 101), HELD)
    assert levels(b) == (95.0, None) and phase_ids(b) == "a"
    assert b.stop is not None and b.stop.armed_ts == bar(1, 0).ts


def test_next_event_moves_to_the_following_phase_and_its_formula_applies_the_same_bar() -> None:
    b = entered(
        {
            "stop": {
                "phases": [
                    {"id": "initial", "level": "entry_price * 0.95", "next": when("close > 105")},
                    {"id": "break_even", "level": "entry_price"},
                ]
            }
        }
    )
    b.on_bar(bar(1, 101), HELD)
    assert (phase_ids(b), levels(b)[0]) == ("initial", 95.0)
    b.on_bar(bar(2, 106), HELD)
    assert (phase_ids(b), levels(b)[0]) == ("break_even", 100.0)  # moved and re-evaluated on the same close
    assert [(f.event, f.situation) for f in b.log if "phase" in f.event] == [("stop.phase:initial.next", "s")]


def test_next_is_checked_before_goto_and_goto_in_array_order_one_move_per_bar() -> None:
    phases = [
        {
            "id": "a",
            "level": "10",
            "next": when("true"),
            "goto": [{"when": when("true"), "to": "c"}],
        },
        {"id": "b", "level": "20", "next": when("true")},
        {"id": "c", "level": "30"},
    ]
    b = entered({"stop": {"phases": phases}})
    b.on_bar(bar(1, 100), HELD)
    assert phase_ids(b) == "b" and levels(b)[0] == 20.0  # next won over goto; only one move although b.next is true
    b.on_bar(bar(2, 100), HELD)
    assert phase_ids(b) == "c" and levels(b)[0] == 30.0

    g = entered(
        {
            "stop": {
                "phases": [
                    {
                        "id": "a",
                        "level": "10",
                        "goto": [
                            {"when": when("close > 200"), "to": "c"},
                            {"when": when("close > 100"), "to": "b"},
                            {"when": when("close > 100"), "to": "c"},
                        ],
                    },
                    {"id": "b", "level": "20"},
                    {"id": "c", "level": "30"},
                ]
            }
        }
    )
    g.on_bar(bar(1, 150), HELD)
    assert phase_ids(g) == "b"  # the first goto whose event fires, not the later one
    g.on_bar(bar(2, 150), HELD)
    assert phase_ids(g) == "b"  # b has no events: it stays


def test_goto_can_move_backwards_and_a_loop_costs_one_move_per_bar() -> None:
    b = entered(
        {
            "stop": {
                "phases": [
                    {"id": "a", "level": "10", "next": when("close > 110")},
                    {"id": "b", "level": "20", "goto": [{"when": when("close < 105"), "to": "a"}]},
                ]
            }
        }
    )
    b.on_bar(bar(1, 120), HELD)
    assert phase_ids(b) == "b"
    b.on_bar(bar(2, 100), HELD)
    assert phase_ids(b) == "a" and levels(b)[0] == 10.0
    b.on_bar(bar(3, 120), HELD)
    assert phase_ids(b) == "b"


def test_trailing_stop_never_loosens_using_the_previous_level() -> None:
    b = entered({"stop": {"phases": [{"id": "t", "level": "max(coalesce(stop_price, 0), close - 5)"}]}})
    seen = []
    for i, price in enumerate([110, 120, 112, 130, 90], start=1):
        b.on_bar(bar(i, price), HELD)
        seen.append(levels(b)[0])
    assert seen == [105.0, 115.0, 115.0, 125.0, 125.0]


def test_a_formula_without_a_value_keeps_the_previous_level_and_zero_means_no_stop() -> None:
    b = entered({"stop": {"phases": [{"id": "a", "level": "entry_price * 0.9 + 0 * (close - 105) / (close - 105)"}]}})
    b.on_bar(bar(1, 101), HELD)
    assert levels(b)[0] == 90.0
    b.on_bar(bar(2, 105), HELD)  # 0/0: no value this bar
    assert levels(b)[0] == 90.0
    z = entered({"stop": {"phases": [{"id": "a", "level": "close - close"}]}})
    z.on_bar(bar(1, 101), HELD)
    assert levels(z)[0] is None  # 0 is v1's "no stop"
    assert z.on_exec_bar(bar(2, 90, low=1), HELD) == []


def test_levels_reset_when_flat_and_the_next_position_starts_at_phase_one() -> None:
    b = entered({"stop": {"phases": [{"id": "a", "level": "50", "next": when("true")}, {"id": "b", "level": "60"}]}})
    b.on_bar(bar(1, 100), HELD)
    assert phase_ids(b) == "b"
    b.on_bar(bar(2, 100), FLAT)  # position is gone
    assert levels(b) == (None, None) and phase_ids(b) == "a"
    assert b.stop is not None and b.stop.armed_ts == -1


def test_stop_and_target_phases_are_independent_lists() -> None:
    b = entered(
        {
            "stop": {
                "phases": [{"id": "s1", "level": "90", "next": when("close > 105")}, {"id": "s2", "level": "100"}]
            },
            "target": {"phases": [{"id": "t1", "level": "120"}, {"id": "t2", "level": "130"}]},
        }
    )
    b.on_bar(bar(1, 110), HELD)
    assert (phase_ids(b, "stop"), phase_ids(b, "target")) == ("s2", "t1")
    assert levels(b) == (100.0, 120.0)


# --------------------------------------------------------------------------- hits: stop first, only later bars


def hit_brain() -> StrategyBrain:
    b = entered(
        {
            "stop": {"phases": [{"id": "s", "level": "95"}]},
            "target": {"phases": [{"id": "t", "level": "105"}]},
        }
    )
    b.on_bar(bar(1, 100), HELD)  # arms 95 / 105 at the close of bar 1
    return b


def test_a_level_armed_by_bar_n_is_not_compared_with_bar_n() -> None:
    b = hit_brain()
    same = bar(1, 100, high=200, low=1)  # the very bar that armed the levels
    assert b.on_exec_bar(same, HELD) == []
    later = bar(2, 100, high=200, low=1)
    assert b.on_exec_bar(later, HELD) != []


def test_stop_is_checked_before_target_when_a_bar_touches_both() -> None:
    b = hit_brain()
    cmds = b.on_exec_bar(bar(2, 100, high=106, low=94), HELD)
    assert [type(c).__name__ for c in cmds].count("Submit") == 1
    (sell,) = submits(cmds)
    assert sell.tag == "stop#1" and sell.intent.action == "close" and sell.intent.size_fraction == 1.0
    assert sell.reduce_only and sell.intent.order_type == "market"
    assert [(f.event, f.situation) for f in b.log if f.event.endswith(".hit")] == [("stop.hit", "stop")]


def test_target_hit_when_only_the_target_is_touched_and_levels_are_inclusive() -> None:
    b = hit_brain()
    (sell,) = submits(b.on_exec_bar(bar(2, 100, high=105, low=96), HELD))  # high == target
    assert sell.tag == "target#1"
    b2 = hit_brain()
    assert b2.on_exec_bar(bar(2, 100, high=104.99, low=95.01), HELD) == []
    (sell2,) = submits(b2.on_exec_bar(bar(3, 100, high=100, low=95), HELD))  # low == stop
    assert sell2.tag == "stop#1"


def test_a_hit_cancels_everything_working_closes_the_entries_and_is_not_repeated() -> None:
    b = make_brain(
        {
            "open": {
                "when": when("not in_position"),
                "orders": [
                    {"id": "e", "size_fraction": 0.4, "take_profit_pct": 10},
                    {"id": "g", "type": "limit", "size_fraction": 0.4, "offset_pct": -5},
                ],
            },
            "manage": {"stop": {"phases": [{"id": "s", "level": "95"}]}},
        }
    )
    b.on_bar(bar(0, 100), FLAT)
    fill = b.on_fill("e#1", 100, 1.0, HELD, 0, done=True)
    tp = submits(fill)[0].tag
    b.on_bar(bar(1, 100), HELD)
    cmds = b.on_exec_bar(bar(2, 94, low=90), HELD)
    assert {c.tag for c in cmds if isinstance(c, Cancel)} == {"g#1", tp}
    assert b.entries_closed
    assert b.on_exec_bar(bar(3, 90, low=80), HELD) == []  # the exit is pending: no second order
    b.on_fill("stop#1", 94, 1.0, FLAT, 3, done=True)
    assert "stop#1" not in b.pending


def test_a_rejected_exit_is_retried_on_the_next_bar() -> None:
    b = hit_brain()
    b.on_exec_bar(bar(2, 90, low=80), HELD)
    b.on_order_end("stop#1", rejected=True)
    assert submits(b.on_exec_bar(bar(3, 90, low=80), HELD))


def test_documents_without_levels_ignore_finer_bars() -> None:
    b = make_brain({"open": {"when": when("true"), "orders": [{"id": "e", "size_fraction": 0.5}]}})
    assert b.on_exec_bar(bar(1, 100, low=0), HELD) == []


@settings(max_examples=200, deadline=None)
@given(
    stop=st.floats(min_value=1, max_value=99),
    target=st.floats(min_value=101, max_value=200),
    low=st.floats(min_value=0.5, max_value=150),
    span=st.floats(min_value=0, max_value=150),
)
def test_property_stop_always_beats_target_on_the_same_bar(stop: float, target: float, low: float, span: float) -> None:
    b = entered(
        {
            "stop": {"phases": [{"id": "s", "level": repr(stop)}]},
            "target": {"phases": [{"id": "t", "level": repr(target)}]},
        }
    )
    b.on_bar(bar(1, 100), HELD)
    high = low + span
    cmds = submits(b.on_exec_bar(bar(2, 100, high=high, low=low), HELD))
    touches_stop, touches_target = low <= stop, high >= target
    if touches_stop:
        assert [c.tag for c in cmds] == ["stop#1"]  # also when the same bar reaches the target
    elif touches_target:
        assert [c.tag for c in cmds] == ["target#1"]
    else:
        assert cmds == []


def test_bars_are_closed_at_the_minute_for_these_tests() -> None:
    assert bar(2, 1).ts == 2 * MINUTE


def test_a_position_held_without_a_cycle_is_managed_too() -> None:
    """After a restart the brain has no cycle but the account may hold a position: its stop and target still work."""
    b = make_brain(
        {
            "trigger": {"on": when("false")},  # never starts a cycle
            "open": {"when": when("true"), "orders": [{"id": "e", "size_fraction": 0.5}]},
            "manage": {"stop": {"phases": [{"id": "s", "level": "entry_price * 0.9"}]}},
        }
    )
    b.on_bar(bar(0, 100), HELD)
    assert not b.in_cycle and levels(b) == (90.0, None)
    assert [c.tag for c in submits(b.on_exec_bar(bar(1, 80, low=70), HELD))] == ["stop#0"]
