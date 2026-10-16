"""Live paper trading against Binance public data. Needs network; run with: pytest -m online"""

from __future__ import annotations

import time

import pytest

from sa_engine.intent import OrderIntent
from sa_engine.paper import FollowerExecutor, run_paper
from tests.strategies import ev

pytestmark = [pytest.mark.online, pytest.mark.slow]

SMOKE = {
    "schema_version": 2,
    "name": "paper-smoke",
    "timeframe": "1m",
    "stages": {
        "open": {
            "when": ev(("forced", ["close > 0"])),  # forced: enter on the first closed live bar
            "orders": [
                {"id": "entry", "type": "market", "size_fraction": 0.05, "take_profit_pct": 0.5, "stop_loss_pct": 2}
            ],
        }
    },
}


def test_run_paper_two_minute_smoke() -> None:
    seen: list[OrderIntent] = []
    report = run_paper(SMOKE, "BINANCE:BTCUSDT", 10_000, duration_s=125, intent_hook=seen.append, log_level="ERROR")
    assert report.quotes_seen > 100  # live market data flowed
    assert report.bars_seen >= 1  # at least one closed 1m bar
    assert report.orders_filled >= 1  # one simulated fill (the forced market entry)
    assert len(seen) == report.intents >= 3  # entry + take-profit + stop, each through the hook
    assert report.fees > 0 and report.final_equity > 9_900
    assert report.rejections == []


def test_follower_executes_a_forced_intent() -> None:
    ex = FollowerExecutor("BINANCE:BTCUSDT", 10_000, log_level="ERROR")
    now = int(time.time() * 1000)
    ex.submit(
        OrderIntent(
            instrument="BINANCE:BTCUSDT",
            side="buy",
            action="open",
            size_fraction=0.1,
            order_type="market",
            timeframe=60,
            ts=now,
        )
    )
    report = ex.run(duration_s=30)
    assert report.quotes_seen > 0 and report.orders_filled == 1
    assert ex.results[0].placed
    assert report.open_qty * 1 > 0 and report.fees == pytest.approx(0.001 * 1_000, rel=0.05)
