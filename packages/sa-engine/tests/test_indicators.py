from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sa_engine.dsl import IndicatorSpec
from sa_engine.indicators import HISTORY, OHLCV, IndicatorBank, make_indicator


def bar(close: float, high: float | None = None, low: float | None = None, i: int = 0) -> OHLCV:
    return OHLCV(i, close, close if high is None else high, close if low is None else low, close, 1.0)


def run(spec: IndicatorSpec, closes: list[float]) -> list[float | None]:
    ind = make_indicator(spec)
    return [ind.update(bar(c, i=i)) for i, c in enumerate(closes)]


def test_sma_known_values() -> None:
    out = run(IndicatorSpec("sma", "close", 3), [1, 2, 3, 4, 5])
    assert out == [None, None, 2.0, 3.0, 4.0]


def test_ema_seeds_with_sma_then_smooths() -> None:
    out = run(IndicatorSpec("ema", "close", 3), [1, 2, 3, 4, 5])
    assert out[:2] == [None, None]
    assert out[2] == 2.0  # SMA seed
    assert out[3] == pytest.approx(2.0 + 0.5 * (4 - 2.0))  # alpha = 2 / (3 + 1)
    assert out[4] == pytest.approx(3.0 + 0.5 * (5 - 3.0))


def test_rsi_wilder_reference_series() -> None:
    # Classic Wilder/StockCharts worked example (14 periods)
    closes = [
        44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328,
        45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
    ]  # fmt: skip
    out = run(IndicatorSpec("rsi", "close", 14), closes)
    assert all(v is None for v in out[:14])
    assert out[14] == pytest.approx(70.53, abs=0.05)
    assert out[15] == pytest.approx(66.32, abs=0.05)
    assert out[16] == pytest.approx(66.55, abs=0.05)


def test_rsi_extremes() -> None:
    up = run(IndicatorSpec("rsi", "close", 3), [1, 2, 3, 4, 5, 6])
    assert up[-1] == 100.0
    down = run(IndicatorSpec("rsi", "close", 3), [6, 5, 4, 3, 2, 1])
    assert down[-1] == 0.0
    flat = run(IndicatorSpec("rsi", "close", 3), [5, 5, 5, 5, 5])
    assert flat[-1] == 50.0


def test_bollinger_known_values() -> None:
    closes = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    mid = run(IndicatorSpec("bb_middle", "close", 8, 2.0), closes)[-1]
    up = run(IndicatorSpec("bb_upper", "close", 8, 2.0), closes)[-1]
    lo = run(IndicatorSpec("bb_lower", "close", 8, 2.0), closes)[-1]
    assert mid == 5.0
    assert up == pytest.approx(5.0 + 2 * 2.0)  # population std of this classic set is exactly 2
    assert lo == pytest.approx(5.0 - 2 * 2.0)


def test_atr_and_donchian() -> None:
    ind = make_indicator(IndicatorSpec("atr", "", 2))
    vals = [
        ind.update(OHLCV(0, 10, 12, 9, 11, 1)),  # TR = 3
        ind.update(OHLCV(1, 11, 13, 10, 12, 1)),  # TR = 3 -> seed 3
        ind.update(OHLCV(2, 12, 16, 11, 15, 1)),  # TR = max(5, 4, 1) = 5 -> (3 + 5) / 2
    ]
    assert vals == [None, 3.0, 4.0]
    up = make_indicator(IndicatorSpec("donchian_upper", "", 3))
    lo = make_indicator(IndicatorSpec("donchian_lower", "", 3))
    mid = make_indicator(IndicatorSpec("donchian_mid", "", 3))
    bars = [OHLCV(i, 0, h, low, 0, 1) for i, (h, low) in enumerate([(5, 1), (7, 2), (6, 0), (4, 3)])]
    assert [up.update(b) for b in bars] == [None, None, 7, 7]
    assert [lo.update(b) for b in bars] == [None, None, 0, 0]
    assert [mid.update(b) for b in bars] == [None, None, 3.5, 3.5]


def test_unknown_indicator_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown indicator kind 'macd'"):
        make_indicator(IndicatorSpec("macd", "close", 5))


def test_bank_history_and_shift() -> None:
    spec = IndicatorSpec("sma", "close", 2)
    bank = IndicatorBank({spec})
    for i, c in enumerate([1.0, 3.0, 5.0]):
        bank.update(bar(c, i=i))
    assert bank.value(spec, 0) == 4.0
    assert bank.value(spec, 1) == 2.0
    assert bank.value(spec, 2) is None
    assert bank.value(spec, 3) is None  # beyond history
    assert bank.price("close", 0) == 5.0
    assert bank.price("close", 2) == 1.0
    assert bank.price("close", 3) is None
    assert bank.price("close", -1) is None
    assert bank.bars == 3
    with pytest.raises(KeyError, match="was not registered"):
        bank.value(IndicatorSpec("ema", "close", 9), 0)


def test_bank_history_is_bounded() -> None:
    bank = IndicatorBank({IndicatorSpec("sma", "close", 2)})
    for i in range(HISTORY + 50):
        bank.update(bar(float(i), i=i))
    assert bank.price("close", HISTORY - 1) is not None
    assert bank.price("close", HISTORY) is None


prices = st.lists(st.floats(min_value=0.01, max_value=1e6, allow_nan=False), min_size=1, max_size=120)


@given(prices, st.integers(min_value=1, max_value=30))
@settings(max_examples=80, deadline=None)
def test_warmup_length_and_value_bounds(closes: list[float], period: int) -> None:
    for kind, warm in (("sma", period), ("ema", period), ("rsi", period + 1)):
        out = run(IndicatorSpec(kind, "close", period), closes)
        assert len(out) == len(closes)
        assert all(v is None for v in out[: warm - 1])
        assert all(v is not None for v in out[warm - 1 :])
    rsi = [v for v in run(IndicatorSpec("rsi", "close", period), closes) if v is not None]
    assert all(0.0 <= v <= 100.0 for v in rsi)
    sma = [v for v in run(IndicatorSpec("sma", "close", period), closes) if v is not None]
    assert all(min(closes) - 1e-6 <= v <= max(closes) + 1e-6 for v in sma)


@given(prices, st.integers(min_value=1, max_value=20), st.floats(min_value=0, max_value=5))
@settings(max_examples=80, deadline=None)
def test_band_ordering(closes: list[float], period: int, k: float) -> None:
    up = run(IndicatorSpec("bb_upper", "close", period, k), closes)
    mid = run(IndicatorSpec("bb_middle", "close", period, k), closes)
    lo = run(IndicatorSpec("bb_lower", "close", period, k), closes)
    for u, m, low in zip(up, mid, lo, strict=True):
        if u is None:
            assert m is None and low is None
        else:
            assert m is not None and low is not None
            assert low <= m + 1e-9 <= u + 2e-9


@given(prices, st.integers(min_value=1, max_value=20))
@settings(max_examples=60, deadline=None)
def test_constant_series_gives_constant_averages(closes: list[float], period: int) -> None:
    c = closes[0]
    flat = [c] * (period * 3)
    for kind in ("sma", "ema"):
        tail = run(IndicatorSpec(kind, "close", period), flat)[-1]
        assert tail is not None and math.isclose(tail, c, rel_tol=1e-9)


@given(st.lists(st.tuples(st.floats(1, 1000), st.floats(0, 50)), min_size=3, max_size=60), st.integers(1, 10))
@settings(max_examples=60, deadline=None)
def test_donchian_and_atr_invariants(hl: list[tuple[float, float]], period: int) -> None:
    bars = [OHLCV(i, low, low + rng, low, low + rng / 2, 1.0) for i, (low, rng) in enumerate(hl)]
    up, lo = (
        make_indicator(IndicatorSpec("donchian_upper", "", period)),
        make_indicator(IndicatorSpec("donchian_lower", "", period)),
    )
    atr = make_indicator(IndicatorSpec("atr", "", period))
    for b in bars:
        u, low, a = up.update(b), lo.update(b), atr.update(b)
        if u is not None and low is not None:
            assert low <= u
        if a is not None:
            assert a >= 0
