"""F7 market endpoints: symbols, candles (local catalog first, Binance otherwise), indicators from the engine's own code."""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from market_support import (
    DAY_END,
    DAY_START,
    FakeBinance,
    candles,
    expected,
    minute_frame,
    recorded,
)
from sa_api import market
from sa_api.app import create_app
from sa_api.config import ApiConfig
from sa_api.market import MarketService
from sa_api.service import Service
from sa_engine import data, dsl
from sa_engine.indicators import OHLCV, IndicatorBank

LAST = DAY_END - 1  # inside the last candle of the recorded day
SPEC_ALL = "ema(20),sma(10),rsi(14),bbands(20,2),atr(14),donchian(20)"


class Rig:
    def __init__(self, tmp_path: Path, **cfg: Any) -> None:
        self.binance = FakeBinance()
        self.cfg = ApiConfig(data_dir=tmp_path / "empty-data", **cfg)
        self.market = MarketService(self.cfg, http=self.binance.client())
        self.client = TestClient(create_app(self.cfg, Service(self.cfg), market=self.market))

    def get(self, path: str, **params: Any) -> Any:
        res = self.client.get(path, params=params)
        assert res.status_code == 200, res.text
        return res.json()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def error(rig: Rig, path: str, status: int, **params: Any) -> str:
    res = rig.client.get(path, params=params)
    assert res.status_code == status, res.text
    msg: str = res.json()["error"]
    return msg


# ---------------------------------------------------------------- symbols


def test_symbols_search_ranks_exact_then_prefix_and_hides_non_spot_and_broken(rig: Rig) -> None:
    out = rig.get("/api/market/symbols", q="eth")
    assert [s["instrument"] for s in out] == [
        "ETHUSDT",  # prefix of the symbol, USDT first
        "ETHBTC",
        "ETHUSDC",
        "WBETHETH",  # contains
    ]
    assert out[0] == {"instrument": "ETHUSDT", "base": "ETH", "quote": "USDT"}
    assert [s["instrument"] for s in rig.get("/api/market/symbols", q="ETH/USDT")] == ["ETHUSDT"]
    assert [s["instrument"] for s in rig.get("/api/market/symbols", q="binance:btcusdt")] == [
        "BTCUSDT"
    ]
    everything = {s["instrument"] for s in rig.get("/api/market/symbols")}
    assert "LUNAUSDT" not in everything and "ETHUPUSDT" not in everything
    assert rig.get("/api/market/symbols", q="zzzz") == []


def test_symbols_are_bounded_and_the_list_is_cached(rig: Rig) -> None:
    assert len(rig.get("/api/market/symbols", limit=2)) == 2
    assert "1..100" in error(rig, "/api/market/symbols", 400, limit=101)
    assert "not an integer" in error(rig, "/api/market/symbols", 400, limit="many")
    assert "at most 20" in error(rig, "/api/market/symbols", 400, q="x" * 21)
    assert "q must be" in error(rig, "/api/market/symbols", 400, q="a b;")
    assert rig.binance.count("/api/v3/exchangeInfo") == 1


def test_symbol_list_refreshes_after_its_ttl_and_survives_a_failed_refresh(tmp_path: Path) -> None:
    rig = Rig(tmp_path, market_symbols_ttl_s=0.0)
    assert rig.get("/api/market/symbols", q="btc")
    rig.binance.fail = httpx.ConnectError("down")
    assert rig.get("/api/market/symbols", q="btc")  # stale list beats no list
    assert rig.binance.count("/api/v3/exchangeInfo") == 2
    fresh = Rig(tmp_path / "x")
    fresh.binance.fail = httpx.ConnectError("down")
    msg = error(fresh, "/api/market/symbols", 502, q="btc")
    assert "cannot reach Binance" in msg and "retry" in msg


def test_a_binance_rate_limit_is_a_503_with_retry_after(rig: Rig) -> None:
    rig.binance.fail = httpx.Response(429)
    res = rig.client.get("/api/market/symbols")
    assert res.status_code == 503 and res.headers["Retry-After"] == "60"
    rig.binance.fail = httpx.Response(500)
    assert "HTTP 500" in error(rig, "/api/market/candles", 502, instrument="BTCUSDT", tf="1h")
    rig.binance.fail = httpx.Response(200, text="<html>")
    assert "not JSON" in error(rig, "/api/market/candles", 502, instrument="BTCUSDT", tf="1h")


# ---------------------------------------------------------------- candles from Binance


@pytest.mark.parametrize("tf", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_candles_equal_the_recorded_binance_klines(rig: Rig, tf: str) -> None:
    want = expected(tf)[-1000:]  # the 1m recording has 1440; a request is capped at 1000
    got = rig.get("/api/market/candles", instrument="BTCUSDT", tf=tf, end_ms=LAST, limit=1000)
    assert got == want


def test_candles_window_end_ms_and_limit(rig: Rig) -> None:
    want = expected("1h")
    got = rig.get(
        "/api/market/candles", instrument="BINANCE:btcusdt", tf="1h", end_ms=want[9]["t"], limit=3
    )
    assert got == want[7:10]  # opening at or before end_ms, ascending
    assert [c["t"] for c in got] == sorted(c["t"] for c in got)
    assert set(got[0]) == {"t", "o", "h", "l", "c", "v"}
    req = rig.binance.calls[-1].url.params
    assert (req["symbol"], req["interval"], req["limit"]) == ("BTCUSDT", "1h", "3")


def test_candles_without_end_ms_ask_for_the_latest(rig: Rig) -> None:
    rig.get("/api/market/candles", instrument="BTCUSDT", tf="1h")
    req = rig.binance.calls[-1].url.params
    assert "endTime" not in req and req["limit"] == "500"


def test_candles_reject_bad_parameters_with_the_value_and_what_to_pass(rig: Rig) -> None:
    path = "/api/market/candles"
    good: dict[str, Any] = {"instrument": "BTCUSDT", "tf": "1h"}
    assert "'2h' is not one of 1m, 5m, 15m, 1h, 4h, 1d" in error(
        rig, path, 400, **{**good, "tf": "2h"}
    )
    assert "tf" in error(rig, path, 400, instrument="BTCUSDT")
    assert "limit=1001 is out of range; pass 1..1000" in error(rig, path, 400, **good, limit=1001)
    assert "limit=0" in error(rig, path, 400, **good, limit=0)
    assert "'abc' is not an integer" in error(rig, path, 400, **good, limit="abc")
    assert "end_ms=-5 is negative" in error(rig, path, 400, **good, end_ms=-5)
    assert "'x' is not an integer" in error(rig, path, 400, **good, end_ms="x")
    assert "instrument is required" in error(rig, path, 400, tf="1h")
    assert "'BTC USDT!'" in error(rig, path, 400, instrument="BTC USDT!", tf="1h")
    assert "NOPEUSDT" in error(rig, path, 404, instrument="NOPEUSDT", tf="1h")
    assert rig.binance.calls[-1].url.params["symbol"] == "NOPEUSDT"


def test_candles_are_cached(rig: Rig) -> None:
    for _ in range(3):
        rig.get("/api/market/candles", instrument="BTCUSDT", tf="1h", end_ms=LAST, limit=24)
    assert rig.binance.count("/api/v3/klines") == 1
    res = rig.client.get(
        "/api/market/candles", params={"instrument": "BTCUSDT", "tf": "1h", "end_ms": LAST}
    )
    assert res.headers["Cache-Control"] == "public, max-age=600"  # closed history
    res = rig.client.get("/api/market/candles", params={"instrument": "BTCUSDT", "tf": "1h"})
    assert (
        res.headers["Cache-Control"] == "public, max-age=2"
        or res.headers["Cache-Control"] == "no-cache"
    )


def test_the_cache_is_bounded() -> None:
    cache = market.TtlCache[int](3)
    for i in range(10):
        cache.put(i, i, 60)
    assert [cache.get(i) for i in range(10)] == [None] * 7 + [7, 8, 9]
    cache.put("never", 1, 0)
    assert cache.get("never") is None


def test_the_cache_expires_entries() -> None:
    now = [0.0]
    cache = market.TtlCache[int](3, clock=lambda: now[0])
    cache.put("a", 1, 5)
    assert cache.get("a") == 1
    now[0] = 5.0
    assert cache.get("a") is None


# ---------------------------------------------------------------- candles from the local catalog


@pytest.fixture
def local(tmp_path: Path) -> Rig:
    """A data dir whose catalog holds the recorded day of 1m bars, converted the way ``ensure_catalog`` does."""
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    from sa_engine.catalog import write_minute_bars

    catalog_dir = tmp_path / "data" / "catalog"
    catalog_dir.mkdir(parents=True)
    write_minute_bars(ParquetDataCatalog(catalog_dir), "BTCUSDT", minute_frame())
    gc.collect()  # Nautilus' write session dies on this thread, not on the reader's
    (catalog_dir / ".converted").mkdir()
    (catalog_dir / ".converted" / "BTCUSDT-2026-09").write_text("ok\n")
    rig = Rig(tmp_path, market_lock_wait_s=0.2)
    rig.market.cfg = rig.cfg.model_copy(update={"data_dir": tmp_path / "data"})
    return rig


@pytest.mark.parametrize("tf", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_local_catalog_candles_equal_binance_klines_without_touching_the_network(
    local: Rig, tf: str
) -> None:
    want = expected(tf)
    got = local.get(
        "/api/market/candles", instrument="BTCUSDT", tf=tf, end_ms=LAST, limit=min(1000, len(want))
    )
    assert got == want[-1000:]
    assert local.binance.calls == []


def test_local_catalog_honours_end_ms_and_limit(local: Rig) -> None:
    want = expected("15m")
    got = local.get(
        "/api/market/candles", instrument="BTCUSDT", tf="15m", end_ms=want[40]["t"] + 5, limit=7
    )
    assert got == want[34:41]
    assert local.binance.calls == []


def test_a_window_the_catalog_lacks_goes_to_binance(local: Rig) -> None:
    # August is not converted (no marker): the whole window is asked from Binance, which here has the day only
    got = local.get(
        "/api/market/candles", instrument="BTCUSDT", tf="1h", end_ms=DAY_START - 1, limit=3
    )
    assert got == []
    assert local.binance.count("/api/v3/klines") == 1
    local.get(
        "/api/market/candles", instrument="BTCUSDT", tf="1h", end_ms=LAST, limit=2
    )  # served locally, no new call
    assert local.binance.count("/api/v3/klines") == 1
    # a symbol that is not in the catalog goes to Binance (which here only knows BTCUSDT)
    error(local, "/api/market/candles", 404, instrument="ETHUSDT", tf="1h", end_ms=LAST, limit=2)
    assert local.binance.count("/api/v3/klines") == 2


def test_the_forming_candle_is_never_built_from_the_catalog(local: Rig) -> None:
    local.market._clock = lambda: DAY_START / 1000 + 3600 * 5
    local.get(
        "/api/market/candles",
        instrument="BTCUSDT",
        tf="1h",
        end_ms=DAY_START + 3600 * 5 + 5,
        limit=2,
    )
    assert local.binance.count("/api/v3/klines") == 1


def test_a_busy_data_dir_lock_sends_the_request_to_binance(local: Rig, tmp_path: Path) -> None:
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with data.file_lock(tmp_path / "data" / "catalog" / ".lock"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        got = local.get("/api/market/candles", instrument="BTCUSDT", tf="1h", end_ms=LAST, limit=24)
        assert time.monotonic() - started < 3  # waited ~0.2 s, not for the conversion
        assert got == expected("1h") and local.binance.count("/api/v3/klines") == 1
    finally:
        release.set()
        t.join(5)


def test_a_broken_catalog_falls_back_to_binance(local: Rig, tmp_path: Path) -> None:
    for f in (tmp_path / "data" / "catalog" / "data").rglob("*.parquet"):
        f.write_bytes(b"not parquet")
    got = local.get("/api/market/candles", instrument="BTCUSDT", tf="1h", end_ms=LAST, limit=24)
    assert got == expected("1h") and local.binance.count("/api/v3/klines") == 1


def test_aggregate_skips_missing_minutes_and_aligns_to_the_grid() -> None:
    c = [market.Candle(60_000 * i, 1 + i, 5 + i, i, 2 + i, 0.1) for i in (0, 1, 2, 5, 6, 7)]
    out = market.aggregate(c, 5 * 60_000)
    assert [x.t for x in out] == [0, 300_000]
    assert (out[0].o, out[0].h, out[0].l, out[0].c, out[0].v) == (1, 7, 0, 4, 0.3)
    assert market.aggregate([], 60_000) == []


# ---------------------------------------------------------------- indicators == the engine's indicators (SEP-8.2)


def engine_series(
    specs: list[dsl.IndicatorSpec], tf: str, last: int
) -> dict[dsl.IndicatorSpec, list[float | None]]:
    """What a strategy sees: the last ``last`` recorded candles through ``IndicatorBank``, bar by bar."""
    bank = IndicatorBank(set(specs))
    out: dict[dsl.IndicatorSpec, list[float | None]] = {s: [] for s in specs}
    for c in candles(tf)[-last:]:
        bank.update(OHLCV(c.t + market.TF_MS[tf], c.o, c.h, c.l, c.c, c.v))
        for s in specs:
            out[s].append(bank.value(s, 0))
    return out


def spec(text: str) -> dsl.IndicatorSpec:
    node = dsl.parse(text)
    assert isinstance(node, dsl.Call)
    found = dsl.indicator_spec(node)
    assert found is not None
    return found


SINGLE = {
    "ema(20)": "ema(close, 20)",
    "sma(10)": "sma(close, 10)",
    "rsi(14)": "rsi(close, 14)",
    "atr(14)": "atr(14)",
}
BANDS = {
    "bbands(20,2)": {"upper": "bb_upper(close, 20, 2)", "mid": "bb_middle(close, 20, 2)", "lower": "bb_lower(close, 20, 2)"},
    "donchian(20)": {"upper": "donchian_upper(20)", "mid": "donchian_mid(20)", "lower": "donchian_lower(20)"},
}  # fmt: skip


@pytest.mark.parametrize(("tf", "limit"), [("1h", 24), ("1m", 300), ("15m", 50)])
def test_api_indicators_equal_the_engine_for_ema_sma_rsi_bollinger_atr_donchian(
    rig: Rig, tf: str, limit: int
) -> None:
    """SEP-8.2: the API's series are the engine's ``IndicatorBank`` values on the same candles. The engine is fed the window
    plus the warm-up candles the API computes first, and compared on the window."""
    got = rig.get(
        "/api/market/indicators",
        instrument="BTCUSDT",
        tf=tf,
        end_ms=LAST,
        limit=limit,
        spec=SPEC_ALL,
    )
    assert list(got) == ["ema(20)", "sma(10)", "rsi(14)", "bbands(20,2)", "atr(14)", "donchian(20)"]
    warm = market.warmup(market.parse_spec(SPEC_ALL))
    have = min(len(candles(tf)), limit + warm)
    window = [c.t for c in candles(tf)[-limit:]]
    every = [spec(t) for t in SINGLE.values()] + [
        spec(t) for b in BANDS.values() for t in b.values()
    ]
    eng = engine_series(every, tf, limit + warm)
    assert len(eng[every[0]]) == have
    for key, text in SINGLE.items():
        want = [
            {"t": t, "value": v}
            for t, v in zip(window, eng[spec(text)][-limit:], strict=True)
            if v is not None
        ]
        assert got[key] == want and len(want) > 0, key
    for key, parts in BANDS.items():
        cols = [eng[spec(parts[f])][-limit:] for f in ("upper", "mid", "lower")]
        want = [
            {"t": t, "upper": u, "mid": mi, "lower": lo}
            for t, u, mi, lo in zip(window, *cols, strict=True)
            if u is not None and mi is not None and lo is not None
        ]
        assert got[key] == want and len(want) > 0, key
    if tf in (
        "1m",
        "15m",
    ):  # enough history before the window: every series has a value on the first window candle
        assert all(series[0]["t"] == window[0] and len(series) == limit for series in got.values())


def test_indicators_are_warmed_up_on_candles_before_the_window(rig: Rig) -> None:
    got = rig.get(
        "/api/market/indicators",
        instrument="BTCUSDT",
        tf="1m",
        end_ms=LAST,
        limit=10,
        spec="rsi(14),ema(30)",
    )
    assert [len(v) for v in got.values()] == [10, 10]
    assert got["rsi(14)"][0]["t"] == expected("1m")[-10]["t"]
    assert [c.url.params["limit"] for c in rig.binance.calls] == [
        "100"
    ]  # window 10 + warm-up max(31, 3 x 30)
    big = rig.get(
        "/api/market/indicators",
        instrument="BTCUSDT",
        tf="1m",
        end_ms=LAST,
        limit=1000,
        spec="ema(20)",
    )
    assert len(big["ema(20)"]) == 1000
    assert [c.url.params["limit"] for c in rig.binance.calls[1:]] == [
        "1000",
        "60",
    ]  # paged: 1000 + 60 warm-up candles


def test_warmup_covers_the_longest_indicator() -> None:
    items = market.parse_spec("ema(5),rsi(14),bbands(200,2)")
    assert market.warmup(items) == 600
    assert market.warmup(market.parse_spec("sma(1000)")) == 1001
    assert market.warmup(market.parse_spec("sma(2)")) == 6


def test_indicator_series_use_the_window_and_start_after_warm_up_when_history_is_short(
    rig: Rig,
) -> None:
    got = rig.get(
        "/api/market/indicators",
        instrument="BTCUSDT",
        tf="1h",
        end_ms=LAST,
        limit=24,
        spec="ema(10),rsi(14)",
    )
    assert (
        len(got["ema(10)"]) == 24 - 9 and len(got["rsi(14)"]) == 24 - 14
    )  # the recording starts at the window
    assert [p["t"] for p in got["ema(10)"]] == [c["t"] for c in expected("1h")[9:]]


def test_spec_forms_aliases_and_duplicates(rig: Rig) -> None:
    got = rig.get(
        "/api/market/indicators", instrument="BTCUSDT", tf="1h", end_ms=LAST, limit=24,
        spec=" ema( high , 5 ) , ema(high,5), bbands(10), sma(close,3) ",
    )  # fmt: skip
    assert list(got) == ["ema(high,5)", "bbands(10)", "sma(close,3)"]
    default_k = rig.get(
        "/api/market/indicators",
        instrument="BTCUSDT",
        tf="1h",
        end_ms=LAST,
        limit=24,
        spec="bbands(10,2)",
    )
    assert got["bbands(10)"] == default_k["bbands(10,2)"]
    eng = engine_series([spec("ema(high, 5)")], "1h", 24)[spec("ema(high, 5)")]
    assert [p["value"] for p in got["ema(high,5)"]] == [v for v in eng if v is not None]


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (None, "spec is required"),
        ("", "spec is required"),
        ("x" * 201, "201 characters; at most 200"),
        (",".join(f"ema({n})" for n in range(2, 11)), "more than 8 indicators"),
        ("macd(12,26)", "not a known indicator"),
        ("ema(close)", "not a known indicator"),
        ("ema(20,30)", "not a known indicator"),
        ("ema(0)", "integer in 1..1000"),
        ("ema(5000)", "integer in 1..1000"),
        ("ema(2.5)", "integer"),
        ("ema(abc)", "not a number"),
        ("ema(20", "unmatched '('"),
        ("ema)20(", "unmatched ')'"),
        ("ema", "not name(args)"),
        ("__import__('os')", "not name(args)"),
    ],
)
def test_bad_specs_are_refused_with_what_to_pass(rig: Rig, bad: str | None, needle: str) -> None:
    params: dict[str, Any] = {"instrument": "BTCUSDT", "tf": "1h", "end_ms": LAST, "limit": 24}
    if bad is not None:
        params["spec"] = bad
    msg = error(rig, "/api/market/indicators", 400, **params)
    assert needle in msg
    assert rig.binance.calls == [], "a bad spec must be refused before any data is fetched"


def test_indicators_validate_the_candle_parameters_too(rig: Rig) -> None:
    assert "tf" in error(
        rig, "/api/market/indicators", 400, instrument="BTCUSDT", tf="3m", spec="ema(5)"
    )


# ---------------------------------------------------------------- rate limit, read-only


def test_market_endpoints_are_rate_limited_per_client_and_others_are_not(tmp_path: Path) -> None:
    rig = Rig(tmp_path, market_rate_per_min=3)
    for _ in range(3):
        assert rig.client.get("/api/market/symbols").status_code == 200
    res = rig.client.get("/api/market/candles", params={"instrument": "BTCUSDT", "tf": "1h"})
    assert res.status_code == 429 and int(res.headers["Retry-After"]) >= 1
    assert "more than 3 market requests in a minute" in res.json()["error"]
    assert rig.client.get("/api/network").status_code != 429


def test_rate_limiter_window_slides_and_is_bounded() -> None:
    now = [0.0]
    lim = market.RateLimiter(2, clock=lambda: now[0])
    lim.check("a")
    lim.check("a")
    lim.check("b")
    with pytest.raises(market.MarketError) as exc:
        lim.check("a")
    assert exc.value.status == 429
    now[0] = 61.0
    lim.check("a")
    for i in range(market.MAX_RATE_KEYS - 5):  # many clients
        lim.check(f"c{i}")
    now[0] += 61  # all of them idle for a minute: their slots are reclaimed for new clients
    for i in range(5):
        lim.check(f"new{i}")
    assert len(lim._hits) <= market.MAX_RATE_KEYS


def test_rate_limiter_refuses_new_clients_when_every_slot_is_live() -> None:
    lim = market.RateLimiter(5, clock=lambda: 1.0)
    for i in range(market.MAX_RATE_KEYS):
        lim.check(f"c{i}")
    with pytest.raises(market.MarketError, match="too many clients"):
        lim.check("one-more")


def test_market_routes_are_get_only(rig: Rig) -> None:
    for path in ("/api/market/symbols", "/api/market/candles", "/api/market/indicators"):
        for verb in ("post", "put", "patch", "delete"):
            assert getattr(rig.client, verb)(path).status_code in (404, 405), f"{verb} {path}"


def test_recording_is_what_the_tests_think_it_is() -> None:
    d = recorded()
    assert (d["symbol"], d["start_ms"], d["end_ms"]) == ("BTCUSDT", DAY_START, DAY_END)
    assert {tf: len(v) for tf, v in d["klines"].items()} == {
        "1m": 1440,
        "5m": 288,
        "15m": 96,
        "1h": 24,
        "4h": 6,
        "1d": 1,
    }


@pytest.mark.online
def test_live_binance_candles_and_symbols_match_a_direct_klines_call(tmp_path: Path) -> None:
    """Against the real Binance public API (no key): our candles equal a direct klines request for the same window."""
    cfg = ApiConfig(data_dir=tmp_path / "none")
    with TestClient(create_app(cfg, Service(cfg), market=MarketService(cfg))) as c:
        end = int(time.time() * 1000) - 3 * 3_600_000
        ours = c.get(
            "/api/market/candles",
            params={"instrument": "ETHUSDT", "tf": "1h", "end_ms": end, "limit": 48},
        )
        assert ours.status_code == 200, ours.text
        direct = httpx.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "ETHUSDT", "interval": "1h", "endTime": end, "limit": 48},
            timeout=20,
        ).json()
        want = [
            {
                "t": r[0],
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "v": float(r[5]),
            }
            for r in direct
        ]
        assert ours.json() == want and len(want) == 48
        syms = c.get("/api/market/symbols", params={"q": "ethusdt"}).json()
        assert syms[0] == {"instrument": "ETHUSDT", "base": "ETH", "quote": "USDT"}
        assert len(c.get("/api/market/symbols").json()) == market.DEFAULT_SYMBOLS
