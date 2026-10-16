from __future__ import annotations

import io
import math
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine.catalog import write_minute_bars

START = "2025-01-01"
DAYS = 5


def synthetic_minutes(start: str = START, days: int = DAYS, seed: int = 7) -> pd.DataFrame:
    """Deterministic oscillating + noisy BTC-like 1m series indexed by bar *close* time (UTC)."""
    n = days * 1440
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = 30_000 * (1 + 0.04 * np.sin(2 * math.pi * t / 480) + 0.015 * np.sin(2 * math.pi * t / 97))
    close = base + np.cumsum(rng.normal(0, 4, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 6, n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC") + pd.Timedelta(minutes=1)
    frame = pd.DataFrame(
        {
            "open": open_.round(2),
            "high": high.round(2),
            "low": low.round(2),
            "close": close.round(2),
            "volume": rng.uniform(1, 20, n).round(5),
        },
        index=pd.DatetimeIndex(idx, name="timestamp"),
    )
    frame["high"] = frame[["open", "high", "low", "close"]].max(axis=1)
    frame["low"] = frame[["open", "high", "low", "close"]].min(axis=1)
    return frame


def kline_zip(frame: pd.DataFrame, *, micro: bool = False, header: bool = False, name: str = "x.csv") -> bytes:
    """Encode ``frame`` like a data.binance.vision monthly kline zip."""
    rows = []
    mult = 1000 if micro else 1
    for ts, r in frame.iterrows():
        assert isinstance(ts, pd.Timestamp)
        open_ms = int(ts.timestamp() * 1000) - 60_000
        rows.append(
            f"{open_ms * mult},{r.open},{r.high},{r.low},{r.close},{r.volume},{open_ms * mult + 59_999},0,1,0,0,0"
        )
    text = (
        ("open_time,open,high,low,close,volume,close_time,qav,n,tb,tq,ig\n" if header else "") + "\n".join(rows) + "\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


@pytest.fixture(scope="session")
def minutes() -> pd.DataFrame:
    return synthetic_minutes()


@pytest.fixture(scope="session")
def fixture_catalog(tmp_path_factory: pytest.TempPathFactory, minutes: pd.DataFrame) -> ParquetDataCatalog:
    path: Path = tmp_path_factory.mktemp("catalog")
    catalog = ParquetDataCatalog(path)
    write_minute_bars(catalog, "BTCUSDT", minutes)
    return catalog
