from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from sa_engine.dsl import (
    Binary,
    Bool,
    Call,
    DslError,
    IndicatorSpec,
    Node,
    Num,
    Series,
    Unary,
    Var,
    collect_indicators,
    evaluate,
    kind_of,
    max_shift,
    parse,
    parse_bool,
    parse_cached,
    to_source,
)
from tests.strategies import bool_exprs, num_exprs


@dataclass
class FakeCtx:
    prices: dict[str, list[float | None]] = field(default_factory=dict)
    inds: dict[IndicatorSpec, list[float | None]] = field(default_factory=dict)
    vars: dict[str, float | bool | None] = field(default_factory=dict)

    def series(self, name: str, shift: int, tf: str | None = None) -> float | None:
        vals = self.prices.get(name, [])
        return vals[shift] if shift < len(vals) else None

    def indicator(self, spec: IndicatorSpec, shift: int) -> float | None:
        vals = self.inds.get(spec, [])
        return vals[shift] if shift < len(vals) else None

    def var(self, name: str) -> float | bool | None:
        return self.vars.get(name)


def ctx_with(close: list[float | None], **extra: list[float | None]) -> FakeCtx:
    return FakeCtx(prices={"close": close, "open": close, "high": close, "low": close, "volume": close})


# --------------------------------------------------------------------------- parsing


def test_parses_precedence_and_associativity() -> None:
    assert to_source(parse("1 + 2 * 3 - 4")) == "((1 + (2 * 3)) - 4)"
    assert to_source(parse("not close > 1 and close < 5 or true")) == "(((not (close > 1)) and (close < 5)) or true)"
    assert to_source(parse("-close * 2")) == "((-close) * 2)"
    assert to_source(parse("8 / 4 / 2")) == "((8 / 4) / 2)"


def test_functions_parse_with_typed_arguments() -> None:
    node = parse("cross_above(ema(close, 12), sma(close, 26))")
    assert isinstance(node, Call) and node.fn == "cross_above"
    assert collect_indicators(node) == {IndicatorSpec("ema", "close", 12), IndicatorSpec("sma", "close", 26)}
    assert collect_indicators(parse("bb_upper(close, 20, 2.5)")) == {IndicatorSpec("bb_upper", "close", 20, 2.5)}
    assert collect_indicators(parse("bb_lower(close, 20, -2)")) == {IndicatorSpec("bb_lower", "close", 20, -2.0)}
    assert collect_indicators(parse("atr(14) > donchian_upper(20)")) == {
        IndicatorSpec("atr", "", 14),
        IndicatorSpec("donchian_upper", "", 20),
    }


@pytest.mark.parametrize(
    ("src", "col", "fragment"),
    [
        ("", 0, "empty"),
        ("close >", 7, "expected a value"),
        ("emaa(close, 5) > 1", 0, "unknown function 'emaa'"),
        ("foo > 1", 0, "unknown name 'foo'"),
        ("ema(close) > 1", 9, "expected ','"),
        ("ema(close, 5, 6) > 1", 12, "ema() takes 2 argument(s)"),
        ("ema(close, 0) > 1", 11, "integer in 1.."),
        ("ema(close, 2.5) > 1", 11, "integer in 1.."),
        ("ema(close, 5000) > 1", 11, "integer in 1.."),
        ("ema(1, 5) > 1", 5, "ema() takes 2 argument(s)"),
        ("ema(volume, close) > 1", 12, "number literal"),
        ("rsi(close, close) > 1", 11, "number literal"),
        ("close + true > 1", 6, "'+' needs a num operand"),
        ("close and true", 6, "'and' needs a bool operand"),
        ("not close", 0, "'not' needs a bool operand"),
        ("(close > 1", 10, "expected ')'"),
        ("close > 1 )", 10, "unexpected ')'"),
        ("close > 1 close", 10, "unexpected 'close'"),
        ("close $ 1", 6, "unexpected character '$'"),
        ("1e999 > close", 0, "too large"),
        ("prev(close, 2000) > 1", 12, "integer in 0.."),
        ("min(close) > 1", 9, "expected ','"),
        ("-true", 0, "unary '-' needs a num"),
    ],
)
def test_parse_errors_carry_column_and_reason(src: str, col: int, fragment: str) -> None:
    with pytest.raises(DslError) as exc:
        parse(src)
    assert fragment in exc.value.message
    assert exc.value.col == col
    assert f"col {col}:" in str(exc.value)


def test_condition_must_be_boolean() -> None:
    with pytest.raises(DslError, match="true/false"):
        parse_bool("close + 1")
    assert parse_bool("close > 1") == parse("close > 1")


def test_length_and_depth_limits() -> None:
    with pytest.raises(DslError, match="characters"):
        parse("close > 1 " + "and close > 1 " * 100)
    with pytest.raises(DslError, match="nested deeper"):
        parse("(" * 70 + "true" + ")" * 70)
    with pytest.raises(DslError, match="nested deeper"):
        parse("not " * 70 + "true")
    with pytest.raises(DslError, match="nested deeper"):
        parse("-" * 70 + "1 > 0")
    with pytest.raises(DslError, match="looks back"):
        parse("prev(prev(close, 900), 900) > 1")
    with pytest.raises(DslError, match="must be a string"):
        parse(5)  # type: ignore[arg-type]


def test_parse_cached_returns_same_ast() -> None:
    assert parse_cached("close > 1") is parse_cached("close > 1")


def test_scientific_notation_and_trailing_whitespace() -> None:
    assert parse("1.5e3  ") == Num(1500.0)
    assert parse("  2E-2") == Num(0.02)


# --------------------------------------------------------------------------- evaluation


def test_arithmetic_and_comparison() -> None:
    c = ctx_with([10.0, 8.0])
    assert evaluate(parse("close * 2 + 1"), c) == 21.0
    assert evaluate(parse("close - prev(close, 1)"), c) == 2.0
    assert evaluate(parse("close > 9 and close <= 10"), c) is True
    assert evaluate(parse("close != 10 or close == 11"), c) is False
    assert evaluate(parse("abs(-close) == 10"), c) is True
    assert evaluate(parse("min(close, 3) + max(close, 3)"), c) == 13.0
    assert evaluate(parse("-close"), c) == -10.0
    assert evaluate(parse("not (close < 5)"), c) is True


def test_none_propagates_through_numbers_and_makes_comparisons_false() -> None:
    c = ctx_with([None])
    assert evaluate(parse("close + 1"), c) is None
    assert evaluate(parse("abs(close)"), c) is None
    assert evaluate(parse("min(close, 1)"), c) is None
    assert evaluate(parse("-close"), c) is None
    assert evaluate(parse("close > 1"), c) is False
    assert evaluate(parse("close != 1"), c) is False
    assert evaluate(parse("not (close > 1)"), c) is True  # a missing value never satisfies a comparison
    assert evaluate(parse("cross_above(close, 1)"), c) is False


def test_division_by_zero_and_overflow_are_none_not_errors() -> None:
    c = ctx_with([0.0, 1e308])
    assert evaluate(parse("1 / close"), c) is None
    inf_minus_inf = Binary("-", Binary("*", Num(1e308), Num(10.0)), Binary("*", Num(1e308), Num(10.0)))
    assert evaluate(inf_minus_inf, c) is None


def test_cross_above_and_below() -> None:
    c = ctx_with([5.0, 3.0, 3.0])
    assert evaluate(parse("cross_above(close, 4)"), c) is True
    assert evaluate(parse("cross_below(close, 4)"), c) is False
    assert evaluate(parse("cross_above(prev(close, 1), 4)"), c) is False
    c2 = ctx_with([3.0, 5.0])
    assert evaluate(parse("cross_below(close, 4)"), c2) is True
    c3 = ctx_with([5.0])  # no previous bar
    assert evaluate(parse("cross_above(close, 4)"), c3) is False


def test_indicator_and_variable_lookup() -> None:
    spec = IndicatorSpec("ema", "close", 3)
    c = FakeCtx(inds={spec: [2.0, 1.0]}, vars={"in_position": True, "entry_price": 7.0})
    assert evaluate(parse("ema(close, 3) > prev(ema(close, 3), 1)"), c) is True
    assert evaluate(parse("in_position and entry_price == 7"), c) is True
    assert evaluate(parse("unrealized_pnl_pct > 1"), c) is False  # unknown -> None -> False


def test_max_shift_and_kind() -> None:
    assert max_shift(parse("cross_above(prev(close, 5), 1)")) == 6
    assert max_shift(parse("close > 1")) == 0
    assert kind_of(parse("close > 1")) == "bool"
    assert kind_of(parse("close + 1")) == "num"
    assert kind_of(parse("in_position")) == "bool"
    assert kind_of(parse("-close")) == "num"
    assert kind_of(parse("not true")) == "bool"
    assert kind_of(Series("close")) == "num"


# --------------------------------------------------------------------------- properties


@given(bool_exprs)
@settings(max_examples=150, deadline=None)
def test_roundtrip_bool_expressions(node: Node) -> None:
    src = to_source(node)
    assume(len(src) <= 500)
    assert parse(src) == node
    assert to_source(parse(src)) == src


@given(num_exprs)
@settings(max_examples=150, deadline=None)
def test_roundtrip_numeric_expressions(node: Node) -> None:
    src = to_source(node)
    assume(len(src) <= 500)
    assert parse(src) == node


def _ctx(values: list[float | None], flags: list[bool]) -> FakeCtx:
    ctx = FakeCtx(
        prices={n: values for n in ("open", "high", "low", "close", "volume")},
        vars={
            **dict.fromkeys(("position_size", "entry_price", "unrealized_pnl_pct", "bars_in_position"), values[0]),
            **dict.fromkeys(("in_position", "is_long", "is_short"), flags[0]),
        },
    )
    return ctx


class _AnyIndicators(FakeCtx):
    """Every indicator returns ``values[shift]`` so the whole expression space is exercised."""

    def __init__(self, values: list[float | None], flags: list[bool]) -> None:
        base = _ctx(values, flags)
        super().__init__(base.prices, {}, base.vars)
        self._values = values

    def indicator(self, spec: IndicatorSpec, shift: int) -> float | None:
        return self._values[shift] if shift < len(self._values) else None


finite_or_none = st.one_of(st.none(), st.floats(min_value=-1e9, max_value=1e9, allow_nan=False))


@given(bool_exprs, st.lists(finite_or_none, min_size=1, max_size=60), st.lists(st.booleans(), min_size=1, max_size=1))
@settings(max_examples=150, deadline=None)
def test_bool_expressions_always_evaluate_to_a_bool(node: Node, values: list[float | None], flags: list[bool]) -> None:
    result = evaluate(node, _AnyIndicators(values, flags))
    assert isinstance(result, bool)


@given(num_exprs, st.lists(finite_or_none, min_size=1, max_size=60), st.lists(st.booleans(), min_size=1, max_size=1))
@settings(max_examples=150, deadline=None)
def test_numeric_expressions_evaluate_to_float_or_none(
    node: Node, values: list[float | None], flags: list[bool]
) -> None:
    result = evaluate(node, _AnyIndicators(values, flags))
    assert result is None or (isinstance(result, float) and not math.isnan(result))


@given(
    num_exprs,
    num_exprs,
    st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), min_size=1, max_size=60),
)
@settings(max_examples=100, deadline=None)
def test_comparison_negation_identity_when_no_missing_values(a: Node, b: Node, values: list[float]) -> None:
    ctx = _AnyIndicators(list(values), [False])
    left, right = evaluate(a, ctx), evaluate(b, ctx)
    assume(left is not None and right is not None)
    assert evaluate(Unary("not", Binary("<", a, b)), ctx) == evaluate(Binary(">=", a, b), ctx)
    assert evaluate(Unary("not", Binary("==", a, b)), ctx) == evaluate(Binary("!=", a, b), ctx)


@given(bool_exprs, bool_exprs, st.lists(finite_or_none, min_size=1, max_size=30))
@settings(max_examples=100, deadline=None)
def test_boolean_algebra(a: Node, b: Node, values: list[float | None]) -> None:
    ctx = _AnyIndicators(values, [True])
    va, vb = evaluate(a, ctx), evaluate(b, ctx)
    assert evaluate(Binary("and", a, b), ctx) == (bool(va) and bool(vb))
    assert evaluate(Binary("or", a, b), ctx) == (bool(va) or bool(vb))
    assert evaluate(Unary("not", Unary("not", a)), ctx) == va
    assert evaluate(Binary("and", a, Bool(True)), ctx) == va


@given(bool_exprs)
@settings(max_examples=100, deadline=None)
def test_collected_indicators_are_all_valid_specs(node: Node) -> None:
    for spec in collect_indicators(node):
        assert spec.period >= 1
        assert spec.kind.split("_")[0] in {"ema", "sma", "rsi", "bb", "atr", "donchian"}


@given(st.text(max_size=80))
@settings(max_examples=300, deadline=None)
def test_arbitrary_text_never_crashes_the_parser(text: str) -> None:
    try:
        node = parse(text)
    except DslError as e:
        assert e.col >= -1
    else:
        assert parse(to_source(node)) == node


def test_var_series_nodes_are_hashable_and_frozen() -> None:
    assert hash(Var("in_position")) == hash(Var("in_position"))
    with pytest.raises(AttributeError):
        Num(1.0).value = 2.0  # type: ignore[misc]
