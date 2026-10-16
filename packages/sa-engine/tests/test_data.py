from __future__ import annotations

import hashlib
import io
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from sa_engine import data
from sa_engine.catalog import ensure_catalog, instrument_for, load_bars, minute_bar_type, write_minute_bars
from tests.conftest import kline_zip, synthetic_minutes

SMALL = synthetic_minutes(days=1)


class FakeBinance:
    """Serves one zip + its .CHECKSUM and counts requests."""

    def __init__(self, zip_bytes: bytes, checksum: str | None = None) -> None:
        self.zip = zip_bytes
        self.checksum = checksum or hashlib.sha256(zip_bytes).hexdigest()
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if url.endswith(".CHECKSUM"):
            return f"{self.checksum}  {url.rsplit('/', 1)[1]}\n".encode()
        return self.zip


def test_month_range_and_urls() -> None:
    assert data.month_range(date(2025, 11, 15), date(2026, 2, 1)) == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert data.month_range(date(2025, 1, 1), date(2025, 1, 31)) == ["2025-01"]
    with pytest.raises(ValueError, match="before start"):
        data.month_range(date(2025, 2, 1), date(2025, 1, 1))
    assert data.month_url("BTCUSDT", "2025-01").endswith("/BTCUSDT/1m/BTCUSDT-1m-2025-01.zip")
    assert data.default_data_dir().name == ".sa-data"


def test_default_data_dir_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SA_DATA_DIR", str(tmp_path))
    assert data.default_data_dir() == tmp_path.resolve()


def test_download_verifies_checksum_and_caches(tmp_path: Path) -> None:
    fake = FakeBinance(kline_zip(SMALL))
    path = data.download_month("BTCUSDT", "2025-01", tmp_path, fake)
    assert path.exists() and len(fake.urls) == 2  # checksum + zip
    again = data.download_month("BTCUSDT", "2025-01", tmp_path, fake)
    assert again == path and len(fake.urls) == 2  # cached and previously verified: no network
    assert (path.parent / (path.name + ".CHECKSUM")).read_text().startswith(fake.checksum)


def test_corrupt_download_is_rejected(tmp_path: Path) -> None:
    fake = FakeBinance(kline_zip(SMALL), checksum="0" * 64)
    with pytest.raises(data.DataError, match="Delete the file and rerun"):
        data.download_month("BTCUSDT", "2025-01", tmp_path, fake)


def test_tampered_cache_is_detected(tmp_path: Path) -> None:
    fake = FakeBinance(kline_zip(SMALL))
    path = data.download_month("BTCUSDT", "2025-01", tmp_path, fake)
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(data.DataError, match="sha256"):
        data.download_month("BTCUSDT", "2025-01", tmp_path, fake)


def test_bad_checksum_file(tmp_path: Path) -> None:
    with pytest.raises(data.DataError, match="not '<sha256>"):
        data.parse_checksum("oops", "u")
    with pytest.raises(data.DataError, match="not '<sha256>"):
        data.parse_checksum("", "u")


@pytest.mark.parametrize("micro", [False, True])
@pytest.mark.parametrize("header", [False, True])
def test_klines_parse_ms_and_us_timestamps_with_or_without_header(micro: bool, header: bool, tmp_path: Path) -> None:
    p = tmp_path / "m.zip"
    p.write_bytes(kline_zip(SMALL, micro=micro, header=header))
    frame = data.read_month(p)
    assert len(frame) == len(SMALL)
    assert frame.index[0] == SMALL.index[0]  # index is the bar close time
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    pd.testing.assert_series_equal(frame["close"], SMALL["close"], check_freq=False)


def test_malformed_files(tmp_path: Path) -> None:
    import zipfile

    two = tmp_path / "two.zip"
    with zipfile.ZipFile(two, "w") as zf:
        zf.writestr("a.csv", "1")
        zf.writestr("b.csv", "2")
    with pytest.raises(data.DataError, match="exactly one CSV"):
        data.read_month(two)
    with pytest.raises(data.DataError, match="no rows"):
        data.parse_klines_csv(io.BytesIO(b""), "empty")
    rows = b"120000,1,1,1,1,1,0,0,0,0,0,0\n60000,1,1,1,1,1,0,0,0,0,0,0\n"
    with pytest.raises(data.DataError, match="not increasing"):
        data.parse_klines_csv(io.BytesIO(rows), "unordered")


def test_utc_ms_accepts_dates_naive_and_aware() -> None:
    assert data.utc_ms(date(1970, 1, 2)) == 86_400_000
    assert data.utc_ms(datetime(1970, 1, 1, 0, 0, 1)) == 1000
    assert data.utc_ms(datetime(1970, 1, 1, 1, tzinfo=UTC)) == 3_600_000


def test_iter_months(tmp_path: Path) -> None:
    fake = FakeBinance(kline_zip(SMALL))
    months = [m for m, _ in data.iter_months("BTCUSDT", date(2025, 1, 1), date(2025, 2, 1), tmp_path, fake)]
    assert months == ["2025-01", "2025-02"]


def test_ensure_catalog_converts_once_and_loads_bars(tmp_path: Path) -> None:
    fake = FakeBinance(kline_zip(SMALL))
    cat = ensure_catalog("BTCUSDT", date(2025, 1, 1), date(2025, 1, 2), tmp_path, fake)
    n_requests = len(fake.urls)
    bars = load_bars(cat, "BTCUSDT.BINANCE", date(2025, 1, 1), date(2025, 1, 2))
    assert len(bars) == 1440
    assert bars[0].ts_event == int(SMALL.index[0].timestamp() * 1e9)
    ensure_catalog("BTCUSDT", date(2025, 1, 1), date(2025, 1, 2), tmp_path, fake)
    assert len(fake.urls) == n_requests  # marker file: no download, no rewrite
    half = load_bars(cat, "BTCUSDT.BINANCE", datetime(2025, 1, 1, 12), date(2025, 1, 2))
    assert len(half) == 720  # bars open in [12:00, 24:00)


def test_ensure_catalog_offline_error_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(data.DataError, match="offline=True; run once online"):
        ensure_catalog("BTCUSDT", date(2025, 1, 1), date(2025, 1, 2), tmp_path, offline=True)


def test_instruments() -> None:
    assert str(instrument_for("btcusdt").id) == "BTCUSDT.BINANCE"
    assert str(instrument_for("ETHUSDT").id) == "ETHUSDT.BINANCE"
    with pytest.raises(ValueError, match="supported: BTCUSDT, ETHUSDT"):
        instrument_for("DOGEUSDT")
    assert str(minute_bar_type("BTCUSDT.BINANCE")) == "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"


def test_write_minute_bars_is_idempotent_for_the_instrument(tmp_path: Path) -> None:
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    cat = ParquetDataCatalog(tmp_path)
    write_minute_bars(cat, "BTCUSDT", SMALL)
    second = synthetic_minutes(start="2025-01-02", days=1)
    write_minute_bars(cat, "BTCUSDT", second)
    assert len(cat.instruments()) == 1
    assert len(cat.bars()) == 2880


def test_ensure_catalog_end_is_exclusive_so_a_month_end_does_not_need_the_next_archive(tmp_path: Path) -> None:
    """A window ending 2025-02-01T00:00 lies in January: February's archive (not yet published) is not requested."""
    for end in (date(2025, 2, 1), datetime(2025, 2, 1, tzinfo=UTC)):
        fake = FakeBinance(kline_zip(SMALL))
        ensure_catalog("BTCUSDT", date(2025, 1, 30), end, tmp_path / str(type(end).__name__), fake)
        assert all("2025-01" in u for u in fake.urls), fake.urls
    fake = FakeBinance(kline_zip(SMALL))
    ensure_catalog("BTCUSDT", date(2025, 1, 30), datetime(2025, 2, 1, 6, tzinfo=UTC), tmp_path / "six", fake)
    assert any("2025-02" in u for u in fake.urls)  # six hours into February needs it


def test_several_processes_sharing_a_data_dir_download_a_month_once(tmp_path: Path) -> None:
    """A provider, its followers and a backtest start together on a cold cache: one fetch, no half-written sidecar."""
    import threading
    import time

    class Slow(FakeBinance):
        def __call__(self, url: str) -> bytes:
            time.sleep(0.05)
            return super().__call__(url)

    fake = Slow(kline_zip(SMALL))
    seen: list[str] = []

    def reader() -> None:
        sidecar = tmp_path / "raw" / "BTCUSDT" / "1m" / "BTCUSDT-1m-2025-01.zip.CHECKSUM"
        for _ in range(200):  # a concurrent reader must never find the sidecar empty
            if sidecar.exists():
                seen.append(sidecar.read_text())
            time.sleep(0.001)

    watcher = threading.Thread(target=reader)
    watcher.start()
    results: list[Path] = []
    threads = [
        threading.Thread(target=lambda: results.append(data.download_month("BTCUSDT", "2025-01", tmp_path, fake)))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    watcher.join()
    assert len(results) == 6 and len(set(results)) == 1
    assert sum(u.endswith(".zip") for u in fake.urls) == 1  # downloaded once, the rest waited for it
    assert seen and all(s.endswith("  BTCUSDT-1m-2025-01.zip\n") for s in seen)
    again = data.download_month("BTCUSDT", "2025-01", tmp_path, fake)
    assert again == results[0] and sum(u.endswith(".zip") for u in fake.urls) == 1


def test_a_catalog_month_is_converted_once_when_processes_race(tmp_path: Path) -> None:
    import threading

    fake = FakeBinance(kline_zip(SMALL))
    errors: list[BaseException] = []

    def go() -> None:
        try:
            ensure_catalog("BTCUSDT", date(2025, 1, 1), date(2025, 1, 2), tmp_path, fake)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(u.endswith(".zip") for u in fake.urls) == 1
    cat = ensure_catalog("BTCUSDT", date(2025, 1, 1), date(2025, 1, 2), tmp_path, fake)
    assert len(load_bars(cat, "BTCUSDT.BINANCE", date(2025, 1, 1), date(2025, 1, 2))) == 1440


def test_file_lock_timeout_raises_instead_of_waiting_forever(tmp_path: Path) -> None:
    lock = tmp_path / "d" / ".lock"
    with (
        data.file_lock(lock),
        pytest.raises(data.LockTimeout, match="locked by another process"),
        data.file_lock(lock, timeout_s=0.1),
    ):
        raise AssertionError("must not get the lock while it is held")  # pragma: no cover
    with data.file_lock(lock, timeout_s=0.1):  # free again
        pass
