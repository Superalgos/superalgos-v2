"""SEP-5.1: a situation is an AND of conditions, an event is an OR of situations, first match wins.

The reference below is a line-by-line transcription of v1's logic:
``TradingStages.js:148-155`` (a situation with conditions starts as passed, any condition that is not ``=== true``
fails it, no conditions never passes), ``TradingSystem.js:60-78`` (``checkConditions``) and the loop that stops at the
first passing situation (``TradingStages.js:186``).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from sa_engine.brain import compile_event
from sa_engine.dsl import IndicatorSpec, parse_bool
from sa_engine.events import CompiledEvent, CompiledSituation, fires, first_match, situation_passes
from sa_engine.schema import Event
from tests.f4_helpers import bar, make_brain, when
from tests.strategies import bool_sources, ev


@dataclass
class Prices:
    close: float = 10.0
    vars: dict[str, float | bool | None] = field(default_factory=dict)

    def series(self, name: str, shift: int, tf: str | None = None) -> float | None:
        return self.close if shift == 0 else None

    def indicator(self, spec: IndicatorSpec, shift: int) -> float | None:
        return None

    def var(self, name: str) -> float | bool | None:
        return self.vars.get(name)


def v1_first_passing(situations: list[list[Any]]) -> int | None:
    """v1: index of the first situation that passes, or None. ``situations[i]`` holds each condition's value."""
    for i, conditions in enumerate(situations):
        passed = None
        if len(conditions) > 0:
            passed = True
        for value in conditions:  # checkConditions: value === true keeps it, anything else fails it
            if value is not True:
                passed = False
        if passed:
            return i
    return None


def build(situations: list[list[bool]]) -> CompiledEvent:
    """An event whose conditions are the constants ``true`` / ``false``."""
    return CompiledEvent(
        tuple(
            CompiledSituation(f"s{i}", tuple(parse_bool("true" if v else "false") for v in conds))
            for i, conds in enumerate(situations)
        )
    )


def test_truth_table_matches_v1_for_every_small_shape() -> None:
    """Exhaustive: 0..3 situations with 0..2 conditions each, every true/false assignment."""
    ctx = Prices()
    checked = 0
    for n_sit in range(4):
        for sizes in itertools.product(range(3), repeat=n_sit):
            for values in itertools.product([True, False], repeat=sum(sizes)):
                it = iter(values)
                situations = [[next(it) for _ in range(k)] for k in sizes]
                expected = v1_first_passing(situations)
                got = first_match(build(situations), ctx)
                assert got == (None if expected is None else f"s{expected}"), (situations, got)
                assert fires(build(situations), ctx) == (expected is not None)
                checked += 1
    assert checked == 400


def test_empty_situation_and_empty_event_never_fire() -> None:
    ctx = Prices()
    assert not situation_passes(CompiledSituation("none", ()), ctx)
    assert first_match(CompiledEvent(()), ctx) is None
    assert first_match(build([[], [True]]), ctx) == "s1"  # an empty situation is skipped, not an error


def test_and_inside_a_situation_or_across_situations() -> None:
    ctx = Prices(close=10)
    event = compile_event(
        Event.model_validate(ev(("a", ["close > 5", "close > 20"]), ("b", ["close > 5", "close < 20"])))
    )
    assert first_match(event, ctx) == "b"  # a fails on its second condition; b passes
    assert first_match(event, Prices(close=30)) == "a"  # both pass: the first in array order wins
    assert first_match(event, Prices(close=1)) is None


def test_first_match_wins_and_the_label_is_recorded() -> None:
    brain = make_brain(
        {
            "trigger": {"on": ev(("late", ["close > 0"]), ("early", ["close > 0"]))},
            "open": {"when": when("false"), "orders": [{"id": "a", "size_fraction": 0.5}]},
        }
    )
    from sa_engine.brain import PositionState

    brain.on_bar(bar(0, 10), PositionState())
    fired = [(f.event, f.situation) for f in brain.log]
    assert fired == [("trigger.on", "late")]  # both pass, the first one is the label


def test_unnamed_situations_are_labelled_by_index() -> None:
    event = compile_event(Event.model_validate({"any": [{"all": ["false"]}, {"all": ["true"]}]}))
    assert first_match(event, Prices()) == "#1"


def test_a_condition_is_true_or_false_never_merely_truthy() -> None:
    """v1 counted only ``=== true``; a number such as 1 failed. Here a non-boolean never reaches an event at all."""
    import pytest

    from sa_engine.dsl import DslError

    with pytest.raises(DslError, match="true/false"):
        parse_bool("close")
    with pytest.raises(DslError, match="true/false"):
        parse_bool("1")


@settings(max_examples=150, deadline=None)
@given(
    shape=st.lists(
        st.lists(st.sampled_from(["true", "false", "close > 5", "close < 5", "not close > 5"]), max_size=3), max_size=4
    ),
    close=st.sampled_from([1.0, 5.0, 9.0]),
)
def test_random_events_match_the_v1_reference(shape: list[list[str]], close: float) -> None:
    ctx = Prices(close=close)
    event = CompiledEvent(
        tuple(CompiledSituation(f"s{i}", tuple(parse_bool(c) for c in cs)) for i, cs in enumerate(shape))
    )
    values = [[bool(_eval(c, close)) for c in cs] for cs in shape]
    expected = v1_first_passing(values)
    assert first_match(event, ctx) == (None if expected is None else f"s{expected}")


def _eval(src: str, close: float) -> bool:
    return {
        "true": True,
        "false": False,
        "close > 5": close > 5,
        "close < 5": close < 5,
        "not close > 5": not close > 5,
    }[src]


@settings(max_examples=60, deadline=None)
@given(conds=st.lists(bool_sources, min_size=1, max_size=3), others=st.lists(bool_sources, min_size=1, max_size=3))
def test_situation_is_and_and_event_is_or_for_generated_conditions(conds: list[str], others: list[str]) -> None:
    from sa_engine.dsl import evaluate

    ctx = Prices(close=7.0, vars={"in_position": False, "is_long": False, "is_short": False})
    a = CompiledSituation("a", tuple(parse_bool(c) for c in conds))
    b = CompiledSituation("b", tuple(parse_bool(c) for c in others))

    def truth(conditions: list[str]) -> bool:
        return all(evaluate(parse_bool(c), ctx) is True for c in conditions)

    assert situation_passes(a, ctx) == truth(conds)
    assert fires(CompiledEvent((a, b)), ctx) == (truth(conds) or truth(others))
    if truth(conds):
        assert first_match(CompiledEvent((a, b)), ctx) == "a"
