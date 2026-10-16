"""SEP-5.5: formulas and conditions can read another timeframe, e.g. ``ema(close, 20, "4h")`` or ``close("1d")``,
without look-ahead: the value seen when a strategy bar closes is the one of the last *closed* bar of that timeframe."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sa_engine.brain import PositionState
from sa_engine.dsl import (
    Call,
    DslError,
    IndicatorSpec,
    Num,
    Series,
    collect_indicators,
    collect_series_tfs,
    collect_timeframes,
    parse,
    parse_bool,
    to_source,
)
from sa_engine.indicators import OHLCV, HigherTimeframeFeed, IndicatorBank, make_indicator
from tests.f4_helpers import make_brain, submits, when

MIN = 60_000
TF_MS = {"1m": MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": 60 * MIN, "2h": 120 * MIN, "4h": 240 * MIN}

# --------------------------------------------------------------------------- the syntax


@pytest.mark.parametrize(
    ("src", "canonical"),
    [
        ('ema(close, 20, "4h")', 'ema(close, 20, "4h")'),
        ('ema(20, "4h")', 'ema(close, 20, "4h")'),  # the source may be left out: close
        ("ema(20)", "ema(close, 20)"),
        ('rsi(14, "1d")', 'rsi(close, 14, "1d")'),
        ('sma(high, 5, "1h")', 'sma(high, 5, "1h")'),
        ('bb_upper(20, 2, "4h")', 'bb_upper(close, 20, 2, "4h")'),
        ('bb_lower(close, 20, 2.5, "1d")', 'bb_lower(close, 20, 2.5, "1d")'),
        ('atr(14, "1h")', 'atr(14, "1h")'),
        ('donchian_upper(20, "1d")', 'donchian_upper(20, "1d")'),
        ('close("4h")', 'close("4h")'),
        ('high("1d") - low("1d")', '(high("1d") - low("1d"))'),
        ('cross_above(ema(5), ema(20, "1h"))', 'cross_above(ema(close, 5), ema(close, 20, "1h"))'),
        ('prev(ema(20, "4h"), 3) < close', '(prev(ema(close, 20, "4h"), 3) < close)'),
    ],
)
def test_timeframe_syntax_parses_and_round_trips(src: str, canonical: str) -> None:
    node = parse(src)
    assert to_source(node) == canonical
    assert parse(to_source(node)) == node


def test_the_ast_and_analysis_helpers_know_the_timeframe() -> None:
    node = parse('ema(close, 20, "4h") > close("1d") and rsi(close, 14) < 30')
    specs = collect_indicators(node)
    assert IndicatorSpec("ema", "close", 20, 0.0, "4h") in specs and IndicatorSpec("rsi", "close", 14) in specs
    assert collect_series_tfs(node) == {("close", "1d")}
    assert collect_timeframes(node) == {"4h", "1d"}
    call = parse('ema(20, "4h")')
    assert isinstance(call, Call) and call.tf == "4h" and call.args == (Series("close"), Num(20.0))
    assert IndicatorSpec("ema", "close", 20) != IndicatorSpec("ema", "close", 20, 0.0, "4h")  # different series


@pytest.mark.parametrize(
    ("src", "col", "fragment"),
    [
        ('ema(close, 20, "7m") > 1', 15, "unknown timeframe"),
        ('ema(close, 20, "4h) > 1', 15, "unterminated string"),
        ('close("") > 1', 6, "unknown timeframe"),
        ("close(5) > 1", 6, 'takes a quoted timeframe, e.g. close("4h")'),
        ("close() > 1", 6, "takes a quoted timeframe"),
        ('close("4h", "1d") > 1', 10, "expected ')'"),
        ('abs(1, "4h") > 1', 5, "abs() takes 1 argument(s)"),
        ('"4h" > 1', 0, "expected a value but found '\"4h\"'"),
        ('ema(close, "4h") > 1', 11, "number literal"),
        ('ema(close, 20, "4h", "1d") > 1', 19, "takes 2 argument(s)"),
        ('cross_above(close, "4h") > 1', 19, "expected a value"),
    ],
)
def test_timeframe_syntax_errors_carry_the_column(src: str, col: int, fragment: str) -> None:
    with pytest.raises(DslError) as exc:
        parse(src)
    assert fragment in exc.value.message, exc.value.message
    assert exc.value.col == col, (src, exc.value)


def test_a_timeframe_must_be_a_multiple_of_the_strategy_timeframe() -> None:
    parse('ema(20, "1h")', 3600)  # same timeframe: fine
    parse('ema(20, "1d")', 3600)
    for tf, base in (("1m", 300), ("5m", 900), ("1h", 7200), ("30m", 900 * 7)):
        with pytest.raises(DslError, match="multiple of the strategy timeframe") as exc:
            parse(f'ema(20, "{tf}")', base)
        assert exc.value.col == 8
    with pytest.raises(DslError, match="multiple"):
        parse_bool('close("5m") > close', 3600)


# --------------------------------------------------------------------------- the feed


def window_bars(first_close_units: int, n: int, base_ms: int = 5 * MIN) -> list[OHLCV]:
    return [OHLCV((first_close_units + i) * base_ms, 10.0 + i, 12.0 + i, 8.0 + i, 11.0 + i, 1.0 + i) for i in range(n)]


def test_a_higher_timeframe_bar_closes_with_the_strategy_bar_that_ends_it() -> None:
    feed = HigherTimeframeFeed(15 * MIN, 5 * MIN)
    bars = window_bars(3, 6)  # closes at 15, 20, 25, 30, 35, 40 minutes
    got = [feed.push(b) for b in bars]
    # (0,15] began before the first bar did, so it is dropped; (15,30] is whole and closes with the bar at 30
    assert got[:3] == [None, None, None] and got[4:] == [None, None]
    assert got[3] == OHLCV(30 * MIN, 11.0, 15.0, 9.0, 14.0, 9.0)


def test_aggregation_is_open_first_high_max_low_min_close_last_volume_sum() -> None:
    feed = HigherTimeframeFeed(15 * MIN, 5 * MIN)
    a = OHLCV(20 * MIN, 1.0, 5.0, 0.5, 2.0, 10.0)
    b = OHLCV(25 * MIN, 2.0, 9.0, 1.5, 3.0, 20.0)
    c = OHLCV(30 * MIN, 3.0, 4.0, 0.2, 3.5, 30.0)
    assert feed.push(a) is None  # the window (15,30] started at 15: a covers (15,20] -> complete start
    assert feed.push(b) is None
    assert feed.push(c) == OHLCV(30 * MIN, 1.0, 9.0, 0.2, 3.5, 60.0)


def test_a_series_that_starts_mid_window_never_completes_that_window_from_partial_data() -> None:
    feed = HigherTimeframeFeed(15 * MIN, 5 * MIN)
    assert feed.push(OHLCV(25 * MIN, 1, 1, 1, 1, 1)) is None  # covers (20,25]: the window (15,30] began earlier
    assert feed.push(OHLCV(30 * MIN, 1, 1, 1, 1, 1)) is None  # closes that window, but it is partial: dropped
    assert feed.push(OHLCV(35 * MIN, 2, 3, 1, 2, 1)) is None
    assert feed.push(OHLCV(40 * MIN, 2, 2, 2, 2, 1)) is None
    assert feed.push(OHLCV(45 * MIN, 2, 2, 2, 4, 1)) == OHLCV(45 * MIN, 2, 3, 1, 4, 3)  # (30,45] is whole


def test_a_gap_releases_the_window_only_when_the_next_one_starts() -> None:
    feed = HigherTimeframeFeed(15 * MIN, 5 * MIN)
    assert feed.push(OHLCV(5 * MIN, 1, 2, 0, 1, 1)) is None
    assert feed.push(OHLCV(10 * MIN, 1, 3, 0, 2, 1)) is None  # the bar closing the window (15) never arrives
    released = feed.push(OHLCV(20 * MIN, 5, 5, 5, 5, 1))  # first bar of the next window
    assert released == OHLCV(10 * MIN, 1, 3, 0, 2, 2)


def test_feed_rejects_a_timeframe_that_is_not_a_multiple() -> None:
    with pytest.raises(ValueError, match="whole multiple"):
        HigherTimeframeFeed(7 * MIN, 5 * MIN)
    with pytest.raises(ValueError, match="whole multiple"):
        HigherTimeframeFeed(MIN, 5 * MIN)


def test_bank_needs_the_base_timeframe_for_higher_timeframe_specs() -> None:
    with pytest.raises(ValueError, match="base_seconds"):
        IndicatorBank({IndicatorSpec("ema", "close", 3, 0.0, "1h")})
    bank = IndicatorBank(set(), 60, {("close", "1h")})
    with pytest.raises(KeyError, match="was not registered"):
        bank.price("open", 0, "1h")
    with pytest.raises(KeyError, match="not registered"):
        bank.value(IndicatorSpec("ema", "close", 9), 0)


# --------------------------------------------------------------------------- the property


def reference_closed_bars(bars: list[OHLCV], base_ms: int, tf_ms: int, upto: int) -> list[OHLCV]:
    """Independent definition: aggregate whole windows that are complete and closed at ``bars[upto].ts``."""
    windows: dict[int, list[OHLCV]] = {}
    for b in bars[: upto + 1]:
        windows.setdefault((b.ts - 1) // tf_ms, []).append(b)
    out = []
    for w in sorted(windows):
        bs = windows[w]
        complete = len(bs) == tf_ms // base_ms and bs[0].ts - base_ms == w * tf_ms
        if complete and bs[-1].ts == (w + 1) * tf_ms:
            out.append(
                OHLCV(
                    bs[-1].ts,
                    bs[0].open,
                    max(b.high for b in bs),
                    min(b.low for b in bs),
                    bs[-1].close,
                    sum(b.volume for b in bs),
                )
            )
    return out


def reference_indicator(spec: IndicatorSpec, bars: list[OHLCV], base_ms: int, tf_ms: int, upto: int) -> float | None:
    ind = make_indicator(replace(spec, tf=None))
    value: float | None = None
    for hb in reference_closed_bars(bars, base_ms, tf_ms, upto):
        value = ind.update(hb)
    return value


@st.composite
def series_case(draw: st.DrawFn) -> tuple[list[OHLCV], str, str]:
    base = draw(st.sampled_from(["1m", "5m", "15m"]))
    tfs = [t for t, ms in TF_MS.items() if ms % TF_MS[base] == 0]
    tf = draw(st.sampled_from(tfs))
    n = draw(st.integers(min_value=1, max_value=120))
    start = draw(st.integers(min_value=1, max_value=400))  # the series may begin anywhere inside a window
    price = draw(st.floats(min_value=50, max_value=150))
    bars = []
    for i in range(n):
        o = price
        price = max(1.0, price + draw(st.floats(min_value=-5, max_value=5)))
        hi = max(o, price) + draw(st.floats(min_value=0, max_value=3))
        lo = max(0.5, min(o, price) - draw(st.floats(min_value=0, max_value=3)))
        bars.append(OHLCV((start + i) * TF_MS[base], o, hi, lo, price, draw(st.floats(min_value=0, max_value=50))))
    return bars, base, tf


def bank_values(bars: list[OHLCV], base: str, tf: str, specs: list[IndicatorSpec]) -> list[list[float | None]]:
    bank = IndicatorBank(set(specs), TF_MS[base] // 1000, {("close", tf), ("high", tf)})
    rows = []
    for b in bars:
        bank.update(b)
        rows.append([bank.value(s, 0) for s in specs] + [bank.price("close", 0, tf), bank.price("high", 0, tf)])
    return rows


@settings(max_examples=120, deadline=None)
@given(series_case())
def test_higher_timeframe_values_equal_the_reference_built_from_closed_bars_only(
    case: tuple[list[OHLCV], str, str],
) -> None:
    bars, base, tf = case
    specs = [
        IndicatorSpec("ema", "close", 3, 0.0, tf),
        IndicatorSpec("sma", "high", 2, 0.0, tf),
        IndicatorSpec("rsi", "close", 3, 0.0, tf),
        IndicatorSpec("atr", "", 2, 0.0, tf),
    ]
    rows = bank_values(bars, base, tf, specs)
    for i, row in enumerate(rows):
        closed = reference_closed_bars(bars, TF_MS[base], TF_MS[tf], i)
        for spec, got in zip(specs, row, strict=False):
            want = reference_indicator(spec, bars, TF_MS[base], TF_MS[tf], i)
            assert got == want or (got is not None and want is not None and abs(got - want) < 1e-9), (
                i,
                spec,
                got,
                want,
            )
        assert row[-2] == (closed[-1].close if closed else None)
        assert row[-1] == (closed[-1].high if closed else None)


@settings(max_examples=120, deadline=None)
@given(series_case(), st.data())
def test_property_never_uses_an_unclosed_higher_timeframe_bar(
    case: tuple[list[OHLCV], str, str], data: st.DataObject
) -> None:
    bars, base, tf = case
    specs = [IndicatorSpec("ema", "close", 2, 0.0, tf), IndicatorSpec("sma", "close", 2, 0.0, tf)]
    full = bank_values(bars, base, tf, specs)
    i = data.draw(st.integers(min_value=0, max_value=len(bars) - 1))
    tf_ms = TF_MS[tf]
    current_window = (bars[i].ts - 1) // tf_ms
    closes_here = bars[i].ts % tf_ms == 0
    # 1) the future cannot matter
    assert bank_values(bars[: i + 1], base, tf, specs)[i] == full[i]
    # 2) neither can the bars of the window that is still open at bar i (bar i itself included unless it closes it)
    mangled = [
        replace(b, open=b.open * 3, high=b.high * 3 + 9, low=b.low / 3, close=b.close * 2 + 5, volume=b.volume + 7)
        if (b.ts - 1) // tf_ms == current_window and j <= i and not closes_here
        else b
        for j, b in enumerate(bars)
    ]
    assert bank_values(mangled, base, tf, specs)[i] == full[i]
    # 3) and the value only changes when a window closes (or the first bar of the next window releases a gapped one)
    if i > 0 and not closes_here and (bars[i - 1].ts - 1) // tf_ms == current_window:
        assert full[i] == full[i - 1]


@settings(max_examples=60, deadline=None)
@given(series_case(), st.integers(min_value=0, max_value=6))
def test_prev_of_a_higher_timeframe_value_is_the_value_the_strategy_saw_then(
    case: tuple[list[OHLCV], str, str], shift: int
) -> None:
    bars, base, tf = case
    spec = IndicatorSpec("ema", "close", 2, 0.0, tf)
    bank = IndicatorBank({spec}, TF_MS[base] // 1000)
    seen: list[float | None] = []
    for b in bars:
        bank.update(b)
        seen.append(bank.value(spec, 0))
        expected = seen[-1 - shift] if shift < len(seen) else None
        assert bank.value(spec, shift) == expected


# --------------------------------------------------------------------------- inside a strategy


def test_a_condition_on_a_higher_timeframe_only_sees_it_after_that_bar_closes() -> None:
    b = make_brain(
        {"open": {"when": when('close > high("15m")'), "orders": [{"id": "e", "size_fraction": 0.5}]}},
        timeframe="5m",
    )
    flat = PositionState()
    ms = 5 * MIN
    # (close, high) of the 5m bars closing at 5, 10, 15 | 20, 25, 30 | 35, 40 minutes; 15m windows (0,15] (15,30] (30,45]
    series = [(100, 100), (100, 100), (105, 110), (100, 100), (100, 100), (118, 120), (115, 115), (500, 500)]
    fired = []
    for k, (close, high) in enumerate(series, start=1):
        fired.append(bool(submits(b.on_bar(OHLCV(k * ms, close, high, close, close, 1), flat))))
    # only the last bar fires: its close (500) beats the closed (15,30] high of 120. The still-open (30,45] window's
    # own high (500, set by that very bar) is not used; if it were, 500 > 500 would be false and nothing would fire.
    assert fired == [False] * 7 + [True]
    assert b.bank.price("high", 0, "15m") == 120.0


def test_higher_timeframe_warmup_is_counted_in_strategy_bars() -> None:
    from sa_engine.schema import validate_strategy
    from tests.f4_helpers import make_doc

    doc = validate_strategy(
        make_doc(
            {"open": {"when": when('close > ema(close, 20, "4h")'), "orders": [{"id": "e", "size_fraction": 0.5}]}},
            "1h",
        )
    )
    assert doc.warmup_bars() == 20 * 3 * 4
