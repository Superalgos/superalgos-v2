"""SEP-3.17 probe: one small market buy through the executor lands on the Binance spot testnet account.

Needs the network and a Binance spot testnet **Ed25519** API key (https://testnet.binance.vision, a throwaway test
account). Spot execution logs on through Binance's WebSocket API, which refuses HMAC keys. Generate the key pair,
register the public key on the testnet site, then:

    SA_BINANCE_TESTNET_API_KEY=<the API key Binance shows after registering> \\
    SA_BINANCE_TESTNET_API_SECRET="$(cat ed25519-private.pem)" \\
        uv run pytest packages/sa-engine/tests/test_testnet_online.py -m testnet -o addopts="" -s

Skips cleanly when either variable is missing. ``SA_TESTNET_PROBE_FRACTION`` (default 0.002 of the account's USDT
equity, about 20 USDT on a fresh 10 000 USDT testnet account) must clear BTCUSDT's minimum notional.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
from pydantic import SecretStr
from sa_core.keys import ExchangeCredentials

from sa_engine.intent import OrderIntent
from sa_engine.network.executor import Execution, NautilusTestnetExecutor
from sa_engine.testnet import SpotTestnetFollower

pytestmark = [pytest.mark.online, pytest.mark.testnet, pytest.mark.slow]

KEY, SECRET = os.environ.get("SA_BINANCE_TESTNET_API_KEY"), os.environ.get("SA_BINANCE_TESTNET_API_SECRET")
WAIT_S = 90.0


@pytest.mark.skipif(
    not (KEY and SECRET),
    reason="set SA_BINANCE_TESTNET_API_KEY (the API key) and SA_BINANCE_TESTNET_API_SECRET (the Ed25519 private key PEM)",
)
def test_a_market_buy_gets_an_order_id_from_the_binance_testnet() -> None:
    assert KEY and SECRET
    follower = SpotTestnetFollower(
        "BINANCE:BTCUSDT",
        ExchangeCredentials(api_key=SecretStr(KEY), api_secret=SecretStr(SECRET)),
        disaster_stop_pct=0,
        log_level="WARNING",
    )
    executor = NautilusTestnetExecutor(follower)
    intent = OrderIntent(
        instrument="BINANCE:BTCUSDT",
        side="buy",
        action="open",
        size_fraction=float(os.environ.get("SA_TESTNET_PROBE_FRACTION", "0.002")),
        order_type="market",
        timeframe=60,
        ts=int(time.time() * 1000),
    )
    events: list[Execution] = []

    def probe() -> None:
        try:
            first = asyncio.run(executor.execute("probe-1", intent, int(time.time() * 1000)))
            events.append(first)
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                events.extend(asyncio.run(executor.poll(int(time.time() * 1000))))
                if any(e.filled or (not e.placed and e is not first) for e in events):
                    break
                time.sleep(0.5)
        finally:
            follower.session.stop()

    threading.Thread(target=probe, daemon=True).start()
    follower.run(duration_s=WAIT_S + 30)  # Nautilus owns the main thread until the probe stops it
    summary = [(e.placed, e.filled, e.reason, e.venue_order_id) for e in events]
    ids = {e.venue_order_id for e in events if e.venue_order_id}
    assert ids, f"Binance returned no order id; events: {summary}"
    assert all(i.isdigit() for i in ids), f"order ids should be Binance's numeric ids, got {ids}"
    assert any(e.filled for e in events), f"order {ids} was accepted but not filled in {WAIT_S:.0f}s: {summary}"
