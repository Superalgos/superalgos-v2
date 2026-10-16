"""The compiled strategy's delivery rules for stop/target levels, without an engine: the finer bar comes before the
strategy bar that closes at the same instant (even when Nautilus delivers them the other way round), and paper mode
watches the levels on quotes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sa_engine.backtest import strategy_bar_type
from sa_engine.brain import Command, PositionState, StrategyBrain
from sa_engine.compile import CompiledStrategy, compile_strategy
from sa_engine.indicators import OHLCV
from tests.f4_helpers import MINUTE, make_doc, when

IID = "BTCUSDT.BINANCE"
LEVELS = {
    "open": {"when": when("not in_position"), "orders": [{"id": "e", "size_fraction": 0.5}]},
    "manage": {"stop": {"phases": [{"id": "p", "level": "entry_price * 0.99"}]}},
}


def fake_bar(bar_type: Any, minutes: int, price: float) -> Any:
    ref = SimpleNamespace(as_double=lambda: price)
    return SimpleNamespace(
        bar_type=bar_type, ts_event=minutes * MINUTE * 1_000_000, open=ref, high=ref, low=ref, close=ref, volume=ref
    )


def wired(monkeypatch: pytest.MonkeyPatch, timeframe: str, **kw: Any) -> tuple[CompiledStrategy, list[tuple[str, int]]]:
    strat = compile_strategy(make_doc(LEVELS, timeframe), IID, strategy_bar_type(IID, timeframe), **kw)
    strat._brain = StrategyBrain(strat.doc, "BINANCE:BTCUSDT")
    seen: list[tuple[str, int]] = []
    real_exec, real_bar = StrategyBrain.on_exec_bar, StrategyBrain.on_bar

    def exec_bar(self: StrategyBrain, bar: OHLCV, pos: PositionState) -> list[Command]:
        seen.append(("exec", bar.ts // MINUTE))
        return real_exec(self, bar, pos)

    def on_bar(self: StrategyBrain, bar: OHLCV, pos: PositionState, equity: float | None = None) -> list[Command]:
        seen.append(("bar", bar.ts // MINUTE))
        return real_bar(self, bar, pos, equity)

    monkeypatch.setattr(StrategyBrain, "on_exec_bar", exec_bar)
    monkeypatch.setattr(StrategyBrain, "on_bar", on_bar)
    monkeypatch.setattr(strat, "equity", lambda: 1000.0)
    monkeypatch.setattr(strat, "position", lambda: PositionState())
    monkeypatch.setattr(strat, "_execute", lambda cmds: None)
    return strat, seen


def test_a_strategy_bar_that_arrives_before_its_finer_bar_waits_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    strat, seen = wired(monkeypatch, "5m")
    strat._exec_bar_type = strat._one_minute()
    strat.on_bar(fake_bar(strat._one_minute(), 4, 100))  # a finer bar, as usual before the strategy bar
    strat.on_bar(fake_bar(strat._std_bar_type, 5, 100))  # arrives first this time: held
    assert seen == [("exec", 4)]
    strat.on_bar(fake_bar(strat._one_minute(), 5, 100))  # the finer bar of the same instant: it goes first
    assert seen == [("exec", 4), ("exec", 5), ("bar", 5)]
    assert strat.bars_seen == 1
    strat.on_bar(fake_bar(strat._one_minute(), 6, 100))  # nothing is held now
    assert seen[-1] == ("exec", 6) and strat.bars_seen == 1
    strat.on_bar(
        fake_bar(strat._std_bar_type, 10, 100)
    )  # a strategy bar from the future of the finer stream waits again
    assert strat._held_bar is not None and seen[-1] == ("exec", 6)


def test_a_one_minute_strategy_checks_levels_on_its_own_bar_before_deciding(monkeypatch: pytest.MonkeyPatch) -> None:
    strat, seen = wired(monkeypatch, "1m")
    strat.on_bar(fake_bar(strat._std_bar_type, 1, 100))
    assert seen == [("exec", 1), ("bar", 1)]


def test_unrelated_bar_types_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    from nautilus_trader.model.data import BarType

    strat, seen = wired(monkeypatch, "5m")
    strat.on_bar(fake_bar(BarType.from_str("ETHUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"), 1, 100))
    strat._brain = None
    strat.on_bar(fake_bar(strat._std_bar_type, 5, 100))
    assert seen == []


def test_paper_mode_watches_the_levels_on_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    strat, _ = wired(monkeypatch, "1h", subscribe_quotes=True)
    assert strat._brain is not None
    sent: list[Command] = []
    monkeypatch.setattr(strat, "_execute", sent.extend)
    monkeypatch.setattr(strat, "position", lambda: PositionState(1.0, 100.0))
    brain = strat._brain
    brain.on_bar(OHLCV(MINUTE, 100, 100, 100, 100, 1), PositionState(1.0, 100.0))  # arms the stop at 99
    assert brain.stop is not None and brain.stop.value == 99.0
    strat._last_price = 99.5
    strat._quote(SimpleNamespace(ts_event=2 * MINUTE * 1_000_000))
    assert sent == []
    strat._last_price = 98.9
    strat._quote(SimpleNamespace(ts_event=3 * MINUTE * 1_000_000))
    assert [type(c).__name__ for c in sent] == ["Submit"] and sent[0].tag == "stop#0"
    strat._brain = None
    strat._quote(SimpleNamespace(ts_event=4 * MINUTE * 1_000_000))
    assert len(sent) == 1


def test_documents_without_levels_ignore_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = {"open": LEVELS["open"]}
    strat = compile_strategy(make_doc(plain, "1h"), IID, strategy_bar_type(IID, "1h"), subscribe_quotes=True)
    strat._brain = StrategyBrain(strat.doc, "BINANCE:BTCUSDT")
    sent: list[Command] = []
    monkeypatch.setattr(strat, "_execute", sent.extend)
    strat._quote(SimpleNamespace(ts_event=1))
    assert sent == []
