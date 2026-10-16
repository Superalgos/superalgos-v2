"""SEP-4.3: three examples over >= 1 year of real BTCUSDT 1m.

Needs network on first run (~40 MB of monthly zips); run with: pytest -m slow
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sa_engine.backtest import run_backtest

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("SA_DATA_DIR", ROOT / ".sa-data"))


@pytest.mark.parametrize("name", ["ema_crossover", "rsi_mean_reversion", "grid_10_level"])
def test_one_year_of_btcusdt_1m(name: str) -> None:
    doc = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    report = run_backtest(doc, "BINANCE:BTCUSDT", "2025-01-01", "2026-01-01", data_dir=DATA_DIR)
    assert report.bars_processed == 365 * 1440
    assert report.trade_count > 0 and report.fees > 0
    assert report.rejections == []
    print(report.summary())
    again = run_backtest(doc, "BINANCE:BTCUSDT", "2025-01-01", "2026-01-01", data_dir=DATA_DIR)
    assert again.fingerprint() == report.fingerprint()
