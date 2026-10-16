"""Instruments and the Nautilus Parquet catalog of Binance 1m bars."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from sa_engine import data
from sa_engine.data import Fetch, http_fetch, utc_ms

NS_PER_MS = 1_000_000
_INSTRUMENTS = {"BTCUSDT": TestInstrumentProvider.btcusdt_binance, "ETHUSDT": TestInstrumentProvider.ethusdt_binance}


def instrument_for(symbol: str) -> CurrencyPair:
    """Backtest instrument definition (Binance spot precision and 0.1% maker/taker fees)."""
    factory = _INSTRUMENTS.get(symbol.upper())
    if factory is None:
        raise ValueError(
            f"no built-in instrument definition for {symbol!r}; supported: {', '.join(sorted(_INSTRUMENTS))}"
        )
    inst = factory()
    assert isinstance(inst, CurrencyPair)
    return inst


def minute_bar_type(instrument_id: str) -> BarType:
    return BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")


def write_minute_bars(catalog: ParquetDataCatalog, symbol: str, frame: pd.DataFrame) -> int:
    """Write a close-time-indexed OHLCV frame as 1m EXTERNAL bars; returns the number written."""
    inst = instrument_for(symbol)
    if not catalog.instruments(instrument_ids=[str(inst.id)]):
        catalog.write_data([inst])
    bars = BarDataWrangler(minute_bar_type(str(inst.id)), inst).process(frame)
    catalog.write_data(bars)
    return len(bars)


def ensure_catalog(
    symbol: str,
    start: date | datetime,
    end: date | datetime,
    data_dir: Path,
    fetch: Fetch = http_fetch,
    *,
    offline: bool = False,
) -> ParquetDataCatalog:
    """Download (unless ``offline``), verify and convert every month touching [start, end) into the catalog."""
    catalog_dir = data_dir / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(catalog_dir)
    marks = catalog_dir / ".converted"
    marks.mkdir(exist_ok=True)
    # `end` is exclusive: 2026-10-01T00:00 does not need October's archive
    last = (end - timedelta(microseconds=1)).date() if isinstance(end, datetime) else end - timedelta(days=1)
    first = start.date() if isinstance(start, datetime) else start
    for month in data.month_range(first, last):
        mark = marks / f"{symbol}-{month}"
        if mark.exists():
            continue
        if offline:
            raise data.DataError(
                f"{symbol} {month} is not in the catalog at {catalog_dir} and offline=True; run once online"
            )
        with data.file_lock(catalog_dir / ".lock"):  # processes sharing the data dir convert a month once
            if mark.exists():
                continue
            frame = data.read_month(data.download_month(symbol, month, data_dir, fetch))
            write_minute_bars(catalog, symbol, frame)
            mark.write_text("ok\n")
    return catalog


def load_bars(
    catalog: ParquetDataCatalog, instrument_id: str, start: date | datetime, end: date | datetime
) -> list[Bar]:
    """1m bars whose *open* time is in [start, end); ``ts_event`` is the bar close time."""
    start_ns = (utc_ms(start) + data.MINUTE_MS) * NS_PER_MS  # first bar closes one minute after start
    end_ns = utc_ms(end) * NS_PER_MS
    bars = catalog.bars(bar_types=[str(minute_bar_type(instrument_id))], start=start_ns, end=end_ns)
    return list(bars)
