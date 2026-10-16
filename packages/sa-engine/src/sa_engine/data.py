"""Binance spot 1m klines: download from data.binance.vision (checksum verified), cache, convert to a Nautilus catalog.

Layout under the data dir (default ``./.sa-data`` or ``$SA_DATA_DIR``; gitignored)::

    raw/<SYMBOL>/1m/<SYMBOL>-1m-YYYY-MM.zip      (+ .CHECKSUM as published)
    catalog/                                     Nautilus ParquetDataCatalog (instrument + 1m EXTERNAL bars)
    catalog/.converted/<SYMBOL>-YYYY-MM          marker: month already in the catalog
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import time
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal

import httpx
import pandas as pd

BASE_URL: Final = "https://data.binance.vision/data/spot/monthly/klines"
MINUTE_MS: Final = 60_000
KLINE_COLUMNS: Final = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_base",
    "taker_quote",
    "ignore",
]

Fetch = Callable[[str], bytes]


class DataError(RuntimeError):
    """Download, checksum or format problem with market data."""


def default_data_dir() -> Path:
    return Path(os.environ.get("SA_DATA_DIR", ".sa-data")).resolve()


def month_range(start: date, end: date) -> list[str]:
    """``YYYY-MM`` strings covering ``start``..``end`` inclusive."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    out: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_url(symbol: str, month: str) -> str:
    return f"{BASE_URL}/{symbol}/1m/{symbol}-1m-{month}.zip"


class LockTimeout(DataError):
    """The data-dir lock stayed taken for longer than the caller was willing to wait."""


@contextlib.contextmanager
def file_lock(path: Path, *, timeout_s: float | None = None) -> Iterator[None]:
    """Exclusive advisory lock on ``path`` (created if needed): several processes (a provider, its followers, a
    backtest) share one data dir, and the first run must download and convert each month once, not in parallel.
    ``timeout_s`` bounds the wait (raises ``LockTimeout``); ``None`` waits as long as it takes.
    A platform without ``fcntl`` runs unlocked."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        if timeout_s is None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"{path} is locked by another process (a download or catalog conversion); "
                            f"waited {timeout_s:g} s, try again shortly"
                        ) from exc
                    time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _write_atomic(path: Path, data: bytes | str) -> None:
    """Replace ``path`` in one step, so a concurrent reader never sees a half-written file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data)
    tmp.replace(path)


def raw_path(data_dir: Path, symbol: str, month: str) -> Path:
    return data_dir / "raw" / symbol / "1m" / f"{symbol}-1m-{month}.zip"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def http_fetch(url: str) -> bytes:  # pragma: no cover - network
    try:
        r = httpx.get(url, timeout=120.0, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise DataError(
            f"GET {url} failed: {e}. Check the network and that the month exists on data.binance.vision."
        ) from e
    return r.content


def parse_checksum(text: str, url: str) -> str:
    parts = text.split()
    if not parts or len(parts[0]) != 64:
        raise DataError(f"checksum file for {url} is not '<sha256>  <name>': {text[:80]!r}")
    return parts[0].lower()


def download_month(symbol: str, month: str, data_dir: Path, fetch: Fetch = http_fetch) -> Path:
    """Fetch one monthly zip into the cache (if absent) and verify it against Binance's published sha256.

    Safe to call from several processes at once: one downloads, the others wait and find it cached.
    """
    path = raw_path(data_dir, symbol, month)
    url = month_url(symbol, month)
    sidecar = path.parent / (path.name + ".CHECKSUM")
    with file_lock(path.parent / ".lock"):
        if path.exists() and sidecar.exists():  # verified earlier: no network needed
            want = parse_checksum(sidecar.read_text(), url)
        else:
            want = parse_checksum(fetch(url + ".CHECKSUM").decode(), url)
        if not path.exists():
            _write_atomic(path, fetch(url))
        got = sha256_bytes(path.read_bytes())
        if got != want:
            raise DataError(f"{path}: sha256 {got} != published {want}. Delete the file and rerun to fetch it again.")
        line = f"{want}  {path.name}\n"
        if not sidecar.exists() or sidecar.read_text() != line:
            _write_atomic(sidecar, line)
    return path


def read_month(path: Path) -> pd.DataFrame:
    """Parse a monthly kline zip into a DataFrame indexed by UTC bar *close* time with OHLCV columns."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise DataError(f"{path}: expected exactly one CSV in the zip, found {names}")
        raw = zf.read(names[0])
    return parse_klines_csv(io.BytesIO(raw), source=str(path))


def parse_klines_csv(buf: io.BytesIO, source: str = "<csv>") -> pd.DataFrame:
    df = pd.read_csv(buf, header=None, names=KLINE_COLUMNS, dtype=str)
    if df.empty:
        raise DataError(f"{source}: no rows")
    if not str(df.iloc[0]["open_time"]).isdigit():  # header row present
        df = df.iloc[1:]
    open_time = df["open_time"].astype("int64")
    # Binance switched spot files to microsecond timestamps in 2025.
    unit: Literal["us", "ms"] = "us" if int(open_time.iloc[0]) > 10**14 else "ms"
    open_ts = pd.to_datetime(open_time.to_numpy(), unit=unit, utc=True)
    out = pd.DataFrame(
        {c: df[c].astype("float64") for c in ("open", "high", "low", "close", "volume")},
    )
    out.index = pd.DatetimeIndex(open_ts + pd.Timedelta(minutes=1), name="timestamp")
    if not out.index.is_monotonic_increasing:
        raise DataError(f"{source}: timestamps are not increasing")
    return out


def iter_months(
    symbol: str,
    start: date,
    end: date,
    data_dir: Path,
    fetch: Fetch = http_fetch,
) -> Iterator[tuple[str, Path]]:
    for month in month_range(start, end):
        yield month, download_month(symbol, month, data_dir, fetch)


def utc_ms(d: date | datetime) -> int:
    dt = d if isinstance(d, datetime) else datetime(d.year, d.month, d.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
