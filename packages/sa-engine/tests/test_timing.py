"""The timing rules (SEP-5.3), enforced end to end on hand-made one-minute data.

1. Decisions are made on a bar close; phase formulas (stop/target levels) are evaluated on bar close only.
2. A decision on bar N is never matched against bar N's own range: resting orders meet bar N+1 onward, a market
   order fills at the first price after the close of N, and levels set at the close of N are compared with later bars.
3. When one bar touches both the stop and the target level, the stop wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sa_engine.brain import StrategyBrain
from sa_engine.indicators import OHLCV
from tests.f4_helpers import MINUTE, T0, backtest, flat, frame_from_rows, make_doc, when

PHASED_EXITS = {
    "trigger": {"on": when("true")},
    "open": {"when": when("not in_position"), "orders": [{"id": "e", "size_fraction": 0.5}]},
    "manage": {
        "stop": {"phases": [{"id": "p", "level": "entry_price * 0.99"}]},
        "target": {"phases": [{"id": "p", "level": "entry_price * 1.01"}]},
    },
}


def hits(run: Any) -> list[tuple[str, int]]:
    return [(f.event, f.ts) for f in run.brain.log if f.event.endswith(".hit")]


def close_ts(bar_index: int) -> int:
    """Close time (ms) of the one-minute bar with this 0-based index of a frame starting at 2025-01-01."""
    return T0 + (bar_index + 1) * MINUTE


def test_stop_wins_when_one_bar_touches_stop_and_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [*flat(30_000, 4), (30_000, 30_400, 29_600, 30_000), *flat(30_000, 3)]  # bar 4 spans 29700 and 30300
    run = backtest(make_doc(PHASED_EXITS), frame_from_rows(rows), tmp_path, monkeypatch)
    assert hits(run)[0] == ("stop.hit", close_ts(4))
    assert [e for e, _ in hits(run) if e == "target.hit"] == []
    sells = [f for f in run.report.fills if f.side == "sell"]
    assert sells and sells[0].t >= close_ts(4)  # sold at market, from the close of the bar that touched the level


def test_target_exits_when_only_the_target_is_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [*flat(30_000, 4), (30_000, 30_400, 29_900, 30_300), *flat(30_300, 3)]
    run = backtest(make_doc(PHASED_EXITS), frame_from_rows(rows), tmp_path, monkeypatch)
    assert hits(run)[0] == ("target.hit", close_ts(4))
    assert run.report.fills[1].side == "sell" and run.report.fills[1].price == 30_300


def test_stop_only_bar_and_gap_through_the_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [*flat(30_000, 4), (30_000, 30_000, 29_500, 29_600), *flat(29_600, 3)]
    run = backtest(make_doc(PHASED_EXITS), frame_from_rows(rows), tmp_path, monkeypatch)
    assert hits(run)[0] == ("stop.hit", close_ts(4))
    assert (
        run.report.fills[1].side == "sell" and run.report.fills[1].price == 29_600
    )  # bot-side stop: market after the touch


def test_a_level_set_on_bar_n_is_not_compared_with_bar_n(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = {
        **PHASED_EXITS,
        "manage": {"stop": {"phases": [{"id": "p", "level": "close - 10"}]}},  # re-evaluated on every close
    }
    # bar 0 enters at its close; the first level (29990) is set at the close of bar 1, whose own low (29900) is under
    # it. Bars 2 and 3 stay above the level; bar 4 touches it. Only bar 4 may count.
    rows = [(30_000,) * 4, (30_000, 30_000, 29_900, 30_000), *flat(30_000, 2), (30_000, 30_000, 29_985, 30_000)]
    run = backtest(make_doc(stages), frame_from_rows(rows + flat(30_000, 2)), tmp_path, monkeypatch)
    assert hits(run) == [("stop.hit", close_ts(4))]


def test_limit_order_decided_on_bar_n_cannot_fill_on_bar_n(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit price is bar 0's own low, which bar 0 reached; only a later dip fills it."""
    stages = {
        "open": {
            "when": when("not in_position"),
            "orders": [{"id": "e", "type": "limit", "size_fraction": 0.5, "rate": "low"}],
        }
    }
    rows = [(30_000, 30_010, 29_990, 30_000), *flat(30_000, 8), (30_000, 30_000, 29_985, 29_995), *flat(29_995, 2)]
    run = backtest(make_doc(stages), frame_from_rows(rows), tmp_path, monkeypatch)
    buys = [f for f in run.report.fills if f.side == "buy"]
    assert len(buys) == 1
    assert buys[0].price == 29_990 and buys[0].t == close_ts(9) and buys[0].t > close_ts(0)


def test_market_order_fills_at_the_decision_close_not_at_a_later_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = {"open": {"when": when("not in_position"), "orders": [{"id": "e", "size_fraction": 0.5}]}}
    rows = [(30_000, 30_000, 30_000, 30_000), (30_500, 30_900, 30_400, 30_800), *flat(30_800, 2)]
    run = backtest(make_doc(stages), frame_from_rows(rows), tmp_path, monkeypatch)
    first = run.report.fills[0]
    assert (first.side, first.price, first.t) == ("buy", 30_000, close_ts(0))  # never bar 1's 30500 range


def test_phase_formulas_run_on_the_strategy_bar_close_and_after_the_finer_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strategy bars of 5m on 1m data: levels move only at 5m closes, and the 1m bar closing at the same instant is
    compared with the *old* level (the 1m bar is delivered first)."""
    stages = {
        **PHASED_EXITS,
        "manage": {"stop": {"phases": [{"id": "p", "level": "close * 0.5"}]}},  # far below: never hit
    }
    calls: list[tuple[str, int, float | None]] = []
    real_exec, real_bar = StrategyBrain.on_exec_bar, StrategyBrain.on_bar

    def exec_bar(self: StrategyBrain, bar: OHLCV, pos: Any) -> Any:
        calls.append(("exec", bar.ts, None if self.stop is None else self.stop.value))
        return real_exec(self, bar, pos)

    def on_bar(self: StrategyBrain, bar: OHLCV, pos: Any, equity: float | None = None) -> Any:
        out = real_bar(self, bar, pos, equity)
        calls.append(("bar", bar.ts, None if self.stop is None else self.stop.value))
        return out

    monkeypatch.setattr(StrategyBrain, "on_exec_bar", exec_bar)
    monkeypatch.setattr(StrategyBrain, "on_bar", on_bar)
    rows = [(30_000 + i, 30_000 + i, 30_000 + i, 30_000 + i) for i in range(40)]
    backtest(make_doc(stages, "5m"), frame_from_rows(rows), tmp_path, monkeypatch)
    bars = [(ts, level) for kind, ts, level in calls if kind == "bar"]
    execs = {ts: level for kind, ts, level in calls if kind == "exec"}
    assert [ts for ts, _ in bars] == [close_ts(i) for i in range(4, 40, 5)]
    for ts, _level in bars:
        assert ts in execs  # the finer bar of the same instant was seen
    order = [(kind, ts) for kind, ts, _ in calls]
    for ts, _ in bars:
        assert order.index(("exec", ts)) < order.index(("bar", ts))  # finer bar first, then the decision
    held_levels = [level for _, level in bars if level is not None]
    assert len(set(held_levels)) == len(held_levels) > 1  # one new level per strategy bar
    # between two strategy closes the level is frozen: a finer bar sees the level set by the latest *earlier* close
    after = dict(bars)
    for ts, seen in execs.items():
        earlier = [b for b, _ in bars if b < ts]
        assert seen == (after[max(earlier)] if earlier else None)


def test_no_finer_bar_subscription_without_levels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = {"open": {"when": when("not in_position"), "orders": [{"id": "e", "size_fraction": 0.5}]}}
    run = backtest(make_doc(stages, "5m"), frame_from_rows(flat(30_000, 20)), tmp_path, monkeypatch)
    assert run.strategy._exec_bar_type is None and run.report.bars_processed == 20
