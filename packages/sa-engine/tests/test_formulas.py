"""SEP-5.2: numeric formulas for order rate, order size, stop and target, evaluated by the safe DSL (no eval);
invalid formulas are rejected with their JSON path and column."""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sa_engine.brain import PositionState
from sa_engine.dsl import BOOL_VARS, NUM_VARS, SERIES_NAMES, DslError, IndicatorSpec, evaluate, parse_num, to_source
from sa_engine.schema import validate_issues
from tests.f4_helpers import bar, make_brain, make_doc, submits, when
from tests.strategies import num_exprs, num_sources

FORMULA_PATH = "$.stages.manage.stop.phases[0].level"


def doc_with_formula(src: str) -> dict[str, Any]:
    return make_doc(
        {
            "open": {"when": when("true"), "orders": [{"id": "a", "size_fraction": 0.5}]},
            "manage": {"stop": {"phases": [{"id": "p", "level": src}]}},
        }
    )


@dataclass
class RandomCtx:
    prices: dict[str, float | None]
    variables: dict[str, float | bool | None]
    indicator_value: float | None

    def series(self, name: str, shift: int, tf: str | None = None) -> float | None:
        return self.prices.get(name) if shift < 3 else None

    def indicator(self, spec: IndicatorSpec, shift: int) -> float | None:
        return self.indicator_value if shift < 5 else None

    def var(self, name: str) -> float | bool | None:
        return self.variables.get(name)


finite = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
maybe = st.one_of(st.none(), finite, st.sampled_from([0.0, 1e308, -1e308]))
contexts = st.builds(
    RandomCtx,
    st.fixed_dictionaries({n: maybe for n in SERIES_NAMES}),
    st.fixed_dictionaries({**{n: maybe for n in NUM_VARS}, **{n: st.booleans() for n in BOOL_VARS}}),
    maybe,
)


# --------------------------------------------------------------------------- evaluation is safe


def test_formulas_cover_the_v1_uses() -> None:
    ctx = RandomCtx(
        {"close": 100.0, "high": 101.0, "low": 99.0, "open": 100.0, "volume": 5.0},
        {
            "entry_price": 100.0,
            "stop_price": None,
            "bars_in_position": 4.0,
            "position_size": 2.0,
            "unrealized_pnl_pct": 1.5,
            "target_price": None,
        },
        2.0,
    )

    def num(src: str) -> float | None:
        v = evaluate(parse_num(src), ctx)
        assert v is None or isinstance(v, float)
        return v

    assert num("entry_price * 0.95") == 95.0  # fixed-percent stop
    assert num("entry_price - 2 * atr(14)") == 96.0  # indicator stop
    assert num("max(coalesce(stop_price, 0), close - 2.5 * atr(14))") == 95.0  # trailing, never loosens
    assert num("min(low, close) - 1") == 98.0
    assert num("abs(close - entry_price * 1.1)") == pytest.approx(10.0)
    assert num("close / (bars_in_position - 4)") is None  # division by zero: no value, not an error
    assert num("coalesce(stop_price, target_price)") is None
    assert num("-(close)") == -100.0


@settings(max_examples=300, deadline=None)
@given(num_exprs, contexts)
def test_random_formulas_evaluate_safely(node: Any, ctx: RandomCtx) -> None:
    src = to_source(node)
    if len(src) > 480:
        return
    parsed = parse_num(src)
    value = evaluate(parsed, ctx)
    assert value is None or (isinstance(value, float) and math.isfinite(value))


@settings(max_examples=100, deadline=None)
@given(num_sources)
def test_a_formula_has_one_value_per_bar_and_does_not_change_the_context(src: str) -> None:
    variables: dict[str, float | bool | None] = {n: 1.0 for n in NUM_VARS}
    variables.update({n: True for n in BOOL_VARS})
    prices: dict[str, float | None] = {n: 3.0 for n in SERIES_NAMES}
    ctx = RandomCtx(prices, variables, 2.0)
    before = (dict(ctx.prices), dict(ctx.variables))
    first, second = evaluate(parse_num(src), ctx), evaluate(parse_num(src), ctx)
    assert first == second
    assert (ctx.prices, ctx.variables) == before


# --------------------------------------------------------------------------- invalid formulas: path and column


def test_a_boolean_where_a_number_is_needed_is_rejected_at_column_zero() -> None:
    _, errs = validate_issues(doc_with_formula("close > 1"))
    assert [(e.path, e.column) for e in errs] == [(FORMULA_PATH, 0)]
    assert "formula must be a number" in errs[0].message


@pytest.mark.parametrize(
    ("src", "column"),
    [
        ("entry_price *", 13),
        ("entry_price * (1 + 0.05", 23),
        ("entry_price $ 2", 12),
        ("entry_pric * 2", 0),
        ("max(close)", 9),
        ("ema(close)", 9),
        ("close +* 2", 7),
        ("1 + true", 2),
        ("not close", 0),
        ("__import__('os').system('id')", 11),
        ("close.real", 5),
        ("[c for c in ()]", 0),
        ("lambda: 1", 6),
        ('eval("1")', 0),
        ("open('f')", 5),
        ("(1).__class__", 3),
        ("close ** 2", 7),
        ("close if 1 else 2", 6),
        ("close; 1", 5),
        ('"abc"', 0),
        ('ema(close, 5, "4h"', 18),
        ('ema(close, 5, "7m")', 14),
    ],
)
def test_dangerous_or_malformed_formulas_fail_with_path_and_column(src: str, column: int) -> None:
    _, errs = validate_issues(doc_with_formula(src))
    assert [e.path for e in errs] == [FORMULA_PATH], src
    assert errs[0].column == column, (src, errs[0])
    assert errs[0].message


def test_no_eval_or_exec_anywhere_in_the_engine() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "sa_engine"
    banned = {"eval", "exec", "compile", "__import__"}
    offenders = [
        f"{p.relative_to(root)}:{n.lineno}"
        for p in root.rglob("*.py")
        for n in ast.walk(ast.parse(p.read_text()))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in banned
    ]
    assert offenders == []


@settings(max_examples=200, deadline=None)
@given(num_sources, st.data())
def test_mutated_formulas_either_validate_or_fail_with_path_and_column(src: str, data: st.DataObject) -> None:
    cut = data.draw(st.integers(min_value=0, max_value=len(src)))
    junk = data.draw(st.sampled_from(["", "$", "(", ")", "'", '"', ",", "@", "1.2.3", " and ", "..", "\\", "x y", "}"]))
    mutated = src[:cut] + junk + src[cut + data.draw(st.integers(min_value=0, max_value=3)) :]
    parsed, errs = validate_issues(doc_with_formula(mutated or " "))
    if parsed is None:
        assert errs, mutated
        for e in errs:
            assert e.path.startswith("$.stages") and e.message
            if e.path == FORMULA_PATH:
                assert e.column is not None and 0 <= e.column <= max(len(mutated), 1), (mutated, e)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=60))
def test_arbitrary_text_never_crashes_validation(text: str) -> None:
    parsed, errs = validate_issues(doc_with_formula(text))
    assert (parsed is None) == bool(errs)
    for e in errs:
        assert e.path.startswith("$")
        if e.path == FORMULA_PATH and e.column is not None:
            assert 0 <= e.column <= len(text)


def test_dsl_error_carries_its_column_and_message_separately() -> None:
    with pytest.raises(DslError) as exc:
        parse_num("1 +")
    assert (exc.value.col, exc.value.message) == (3, "expected a value but found end of expression")
    assert str(exc.value) == "col 3: expected a value but found end of expression"


# --------------------------------------------------------------------------- formulas inside the brain


def sized(size: str, **extra: Any) -> Any:
    return make_brain(
        {"open": {"when": when("true"), "orders": [{"id": "a", "size": size, **extra}]}},
    )


def test_order_size_formula_is_a_fraction_of_equity_evaluated_at_placement() -> None:
    b = sized("min(0.25, close / 1000)")
    (cmd,) = submits(b.on_bar(bar(0, 100), PositionState()))
    assert cmd.intent.size_fraction == pytest.approx(0.1)


def test_order_size_formula_without_a_value_or_not_positive_places_nothing_yet() -> None:
    b = sized("close / (close - 100)")  # division by zero on the first bar
    assert submits(b.on_bar(bar(0, 100), PositionState())) == []
    assert submits(b.on_bar(bar(1, 110), PositionState())) != []  # the order is still waiting: it is placed next bar
    z = sized("close - 100")
    assert submits(z.on_bar(bar(0, 100), PositionState())) == []  # zero: v1's "size zero" places no order


def test_order_size_is_clamped_to_what_is_left_of_the_equity() -> None:
    b = make_brain(
        {
            "open": {
                "when": when("true"),
                "orders": [
                    {"id": "a", "size": "0.7"},
                    {"id": "b", "size": "0.7"},  # only 0.3 is left
                    {"id": "c", "size": "0.5"},  # nothing is left
                ],
            }
        }
    )
    placed = submits(b.on_bar(bar(0, 100), PositionState()))
    assert [(c.tag, round(c.intent.size_fraction, 6)) for c in placed] == [("a#1", 0.7), ("b#1", 0.3)]


def test_rate_formula_sets_limit_and_stop_prices_with_the_market_side_enforced() -> None:
    b = make_brain(
        {
            "open": {
                "when": when("true"),
                "orders": [
                    {"id": "lo", "type": "limit", "size_fraction": 0.2, "rate": "close - 3"},
                    {"id": "hi", "type": "limit", "size_fraction": 0.2, "rate": "close + 3"},  # capped at the close
                    {"id": "st", "type": "stop", "size_fraction": 0.2, "rate": "close + 3"},
                    {"id": "lw", "type": "stop", "size_fraction": 0.2, "rate": "close - 3"},  # floored at the close
                ],
            }
        }
    )
    lo, hi, st_, lw = submits(b.on_bar(bar(0, 100), PositionState()))
    assert lo.intent.limit_price == 97.0 and hi.intent.limit_price == 100.0
    assert st_.intent.stop_price == 103.0 and lw.intent.stop_price == 100.0


def test_rate_formula_without_a_value_or_not_positive_waits() -> None:
    b = make_brain(
        {
            "open": {
                "when": when("true"),
                "orders": [{"id": "a", "type": "limit", "size_fraction": 0.5, "rate": "sma(close, 2) - 1000"}],
            }
        }
    )
    assert submits(b.on_bar(bar(0, 100), PositionState())) == []  # no sma yet
    assert submits(b.on_bar(bar(1, 100), PositionState())) == []  # negative price: not placed
    assert b.slots["a"].state == "idle"


def test_semantics_of_the_formula_variables_stop_price_and_target_price() -> None:
    b = make_brain(
        {
            "open": {"when": when("true"), "orders": [{"id": "a", "size_fraction": 0.5}]},
            "close": {"when": when("stop_price > 0 and target_price > stop_price")},
            "manage": {
                "stop": {"phases": [{"id": "p", "level": "entry_price - 1"}]},
                "target": {"phases": [{"id": "p", "level": "stop_price + 10"}]},
            },
        }
    )
    b.on_bar(bar(0, 100), PositionState())
    b.on_fill("a#1", 100, 1.0, PositionState(1.0, 100.0), 1, done=True)
    out = b.on_bar(bar(1, 100), PositionState(1.0, 100.0))
    assert (b.stop.value if b.stop else None, b.target.value if b.target else None) == (99.0, 109.0)
    assert out == []
    closes = submits(b.on_bar(bar(2, 100), PositionState(1.0, 100.0)))
    assert closes and closes[0].tag.startswith("close#")  # the close condition saw both levels
