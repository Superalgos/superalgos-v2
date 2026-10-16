"""A recorded day of Binance BTCUSDT klines (2026-09-01 UTC, every timeframe) served through an httpx mock transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from sa_api.market import Candle

FIXTURE = Path(__file__).resolve().parent / "data" / "binance_btcusdt_20260901.json"
DAY_START = 1788220800000  # 2026-09-01T00:00:00Z
DAY_END = DAY_START + 86_400_000

EXCHANGE_INFO: dict[str, Any] = {
    "timezone": "UTC",
    "symbols": [
        {"symbol": s, "status": st, "baseAsset": b, "quoteAsset": q, "isSpotTradingAllowed": spot}
        for s, st, b, q, spot in [
            ("BTCUSDT", "TRADING", "BTC", "USDT", True),
            ("ETHUSDT", "TRADING", "ETH", "USDT", True),
            ("ETHBTC", "TRADING", "ETH", "BTC", True),
            ("ETHUSDC", "TRADING", "ETH", "USDC", True),
            ("WBETHETH", "TRADING", "WBETH", "ETH", True),
            ("BNBUSDT", "TRADING", "BNB", "USDT", True),
            ("LUNAUSDT", "BREAK", "LUNA", "USDT", True),
            ("ETHUPUSDT", "TRADING", "ETHUP", "USDT", False),
        ]
    ],
}


def recorded() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def expected(tf: str) -> list[dict[str, float | int]]:
    """The recorded Binance klines of ``tf`` as the API must return them."""
    return [
        {
            "t": r[0],
            "o": float(r[1]),
            "h": float(r[2]),
            "l": float(r[3]),
            "c": float(r[4]),
            "v": float(r[5]),
        }
        for r in recorded()["klines"][tf]
    ]


def candles(tf: str) -> list[Candle]:
    return [Candle(r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in expected(tf)]  # type: ignore[arg-type]


def minute_frame() -> pd.DataFrame:
    """The recorded 1m klines as the close-time-indexed frame the engine's catalog writer takes."""
    rows = recorded()["klines"]["1m"]
    frame = pd.DataFrame(
        {
            name: [float(r[i]) for r in rows]
            for i, name in enumerate(["open", "high", "low", "close", "volume"], 1)
        },
        index=pd.DatetimeIndex(
            pd.to_datetime([r[0] + 60_000 for r in rows], unit="ms", utc=True), name="timestamp"
        ),
    )
    return frame


class FakeBinance:
    """Answers /api/v3/klines and /api/v3/exchangeInfo like Binance does, from the recording; counts requests."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.fail: httpx.Response | Exception | None = None
        self.data = recorded()["klines"]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.fail is not None:
            if isinstance(self.fail, Exception):
                raise self.fail
            return self.fail
        q = req.url.params
        if req.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=EXCHANGE_INFO)
        assert req.url.path == "/api/v3/klines", req.url
        if q["symbol"] != "BTCUSDT":
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        rows = [r for r in self.data[q["interval"]] if int(r[0]) <= int(q.get("endTime", 10**15))]
        return httpx.Response(
            200, json=[[*r, 0, "0", 0, "0", "0", "0"] for r in rows[-int(q["limit"]) :]]
        )

    def count(self, path: str) -> int:
        return sum(1 for c in self.calls if c.url.path == path)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))
