"""Edge paths of the network package: REST klines, replay tails, the relay querier, defensive branches."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import httpx
import pytest
from sa_core.delegation import delegation_tag
from sa_core.models import COMMITMENT_KIND, Commitment
from sa_core.nostr import NostrError, build_event
from sa_core.signals import (
    build_commitment_event,
    build_private_signal_event,
    build_signal_event,
    commitment_digest,
)

from sa_engine.data import DataError
from sa_engine.network import replay
from sa_engine.network.entitlement import Entitled, UrlQuerier
from sa_engine.network.journal import ProviderStore
from sa_engine.network.provider import ProviderBot
from tests.conftest import kline_zip, synthetic_minutes
from tests.fake_relay import FakeRelay
from tests.net_helpers import Identity, intent
from tests.test_data import FakeBinance
from tests.test_network_follower import event, follower, signal

# ---------------------------------------------------------------- replay


def klines(start_ms: int, n: int) -> list[list[object]]:
    return [[start_ms + i * 60_000, "100", "101", "99", "100.5", "1.5", 0, 0, 0, 0, 0, 0] for i in range(n)]


def test_rest_klines_are_paged_and_parsed() -> None:
    pages: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        pages.append(params)
        start = int(params["startTime"])
        n = 1000 if len(pages) == 1 else 250
        return httpx.Response(200, json=klines(start, n))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = replay.fetch_rest_candles("BTCUSDT", 0, 1250 * 60_000, client=client)
    assert len(got) == 1250 and got[0].open_time_ms == 0 and got[-1].open_time_ms == 1249 * 60_000
    assert len(pages) == 2 and pages[1]["startTime"] == str(1000 * 60_000) and pages[0]["symbol"] == "BTCUSDT"


def test_rest_klines_errors_say_what_to_do() -> None:
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(replay.ReplayError, match="check the network"):
        replay.fetch_rest_candles("BTCUSDT", 0, 60_000, client=down)
    empty = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(replay.ReplayError, match="no BTCUSDT candles"):
        replay.fetch_rest_candles("BTCUSDT", 0, 60_000, client=empty)


def test_replay_candles_carry_a_tail_when_the_archive_has_it(tmp_path: Path) -> None:
    fake = FakeBinance(kline_zip(synthetic_minutes(start="2025-01-01", days=5)))
    spec = replay.ReplaySpec(instrument="BTCUSDT", start="2025-01-01", end="2025-01-02", speed=3600, wall0_ms=10**12)
    assert spec.tail_days() == 6  # (60 + 60) s x 3600 = 5 days of market time, +1
    window = replay.load_candles("BTCUSDT", date(2025, 1, 1), date(2025, 1, 2), data_dir=tmp_path, fetch=fake)
    assert len(window) == 1440
    got = replay.load_replay_candles(spec, data_dir=tmp_path)  # the cache now holds the month
    assert len(got) == 5 * 1440  # the archive ends on Jan 5: the tail is whatever exists after the window
    assert got[0].open_time_ms == spec.wall0_ms
    assert all(b.open_time_ms > a.open_time_ms for a, b in itertools.pairwise(got))


def test_replay_tail_falls_back_to_the_window_when_the_archive_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replay.ReplaySpec(instrument="BTCUSDT", start="2025-01-01", end="2025-01-02", speed=3600, wall0_ms=10**12)
    calls: list[date] = []

    def fake_load(symbol: str, start: date, end: date, **kw: object) -> list[object]:
        calls.append(end)
        if end > date(2025, 1, 2):
            raise DataError("no such month")
        return []

    monkeypatch.setattr(replay, "load_candles", fake_load)
    monkeypatch.setattr(replay, "wall_candles", lambda raw, clock: raw)
    assert replay.load_replay_candles(spec, data_dir=tmp_path) == []
    assert calls == [date(2025, 1, 8), date(2025, 1, 2)]


# ---------------------------------------------------------------- relay querier


async def test_url_querier_opens_a_short_lived_connection() -> None:
    relay = FakeRelay()
    await relay.start()
    try:
        ident = Identity()
        await relay.inject(build_event(ident.device, 1, "hello", [], int(time.time())))
        got = await UrlQuerier(relay.url).query([{"kinds": [1]}])
        assert [e.content for e in got] == ["hello"]
    finally:
        await relay.stop()
    with pytest.raises(NostrError, match="cannot connect"):
        await UrlQuerier("ws://127.0.0.1:1").query([{"kinds": [1]}], timeout=0.5)


# ---------------------------------------------------------------- provider edges


@pytest.fixture
async def relay() -> AsyncIterator[FakeRelay]:
    r = FakeRelay()
    await r.start()
    yield r
    await r.stop()


def make_bot(ident: Identity, relay: FakeRelay, tmp: Path, **kw: object) -> ProviderBot:
    return ProviderBot(
        device=ident.device, delegation=ident.delegation, rules=kw.pop("rules", None) or ident.rules(),  # type: ignore[arg-type]
        strategy_id="strat", store=ProviderStore(tmp / "state"), relay_url=relay.url, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


async def test_drain_before_start_is_a_no_op_and_public_bots_have_no_recipients(
    relay: FakeRelay, tmp_path: Path
) -> None:
    bot = make_bot(Identity(), relay, tmp_path)
    await bot.drain()
    assert await bot.refresh_recipients() == []
    assert bot.store.replay_path.name == "replay.json"


async def test_a_failing_entitlement_refresh_keeps_the_last_list(relay: FakeRelay, tmp_path: Path) -> None:
    ident, f = Identity(), Identity()
    calls = 0

    class Flaky:
        async def refresh(self) -> list[Entitled]:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise NostrError("relay down")
            return [Entitled(f.pubkey, f.sa.address, 1)]

    async with make_bot(
        ident, relay, tmp_path, rules=ident.private_rules(), entitlements=Flaky(), entitlement_refresh_s=0.05
    ) as bot:
        await asyncio.sleep(0.3)
        events = bot.build_events(intent())
        assert len(events) == 2 and calls >= 2  # commitment + one ciphertext, from the list that survived


async def test_the_sender_survives_a_crash_on_one_event(
    relay: FakeRelay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with make_bot(Identity(), relay, tmp_path) as bot:
        real = bot._send
        boom = True

        async def flaky(ev: object) -> None:
            nonlocal boom
            if boom:
                boom = False
                raise RuntimeError("encoder exploded")
            await real(ev)  # type: ignore[arg-type]

        monkeypatch.setattr(bot, "_send", flaky)
        bot.enqueue(bot.build_events(intent()))
        bot.enqueue(bot.build_events(intent()))
        await bot.drain()
        assert (
            bot.stats.sent == 1 and len(bot.store.unacked()) == 1
        )  # the crashed event stays unacked for the next start


# ---------------------------------------------------------------- follower edges


async def test_one_bad_event_does_not_stop_the_stream(
    relay: FakeRelay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sa_core.power import ruleset_event

    p = Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url, clock_s=time.time())
    bot.clock = time.time
    seen: list[str] = []
    real = bot.ingest

    async def flaky(ev: object, source: str = "relay") -> object:
        seen.append("x")
        if len(seen) == 1:
            raise RuntimeError("bug in ingest")
        return await real(ev, source)  # type: ignore[arg-type]

    monkeypatch.setattr(bot, "ingest", flaky)
    task = asyncio.create_task(bot.run(asyncio.Event(), duration_s=2.0))
    await asyncio.wait_for(bot.ready.wait(), 5)
    now_ms = int(time.time() * 1000)
    await relay.inject(build_signal_event(p.device, signal(p, 1, None, at_ms=now_ms), p.delegation))
    second = build_signal_event(p.device, signal(p, 1, None, at_ms=now_ms + 1), p.delegation)
    await relay.inject(second)
    await task
    assert len(seen) == 2 and bot.ledger.get(second.id) is not None


async def test_file_signals_may_bring_their_own_rule_set(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, rules=None)
    row = await bot.ingest_text(event(p, 1, None).model_dump_json(), provider=p.pubkey, rules=p.rules())
    assert row.status == "accepted" and bot.rules[p.pubkey] == p.rules()


async def test_a_commitment_for_another_slot_is_refused(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    bot = follower(tmp_path, p, rules=p.private_rules(), me=me)
    sig = signal(p, 1, None)
    forged = Commitment(strategy_id="strat", seq=2, prev="a" * 64, digest=commitment_digest(sig.model_dump_json()))
    await bot.ingest(
        build_event(p.device, COMMITMENT_KIND, forged.model_dump_json(), [delegation_tag(p.delegation)], 1_800_000_000)
    )
    row = await bot.ingest(build_private_signal_event(p.device, sig, p.delegation, me.pubkey, 1_800_000_000))
    assert row and row.status == "rejected" and "does not match this signal's slot" in row.reason


async def test_a_republished_private_signal_is_not_executed_twice(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    bot = follower(tmp_path, p, rules=p.private_rules(), me=me)
    sig = signal(p, 1, None)
    await bot.ingest(build_commitment_event(p.device, sig, p.delegation, 1_800_000_000))
    first = await bot.ingest(build_private_signal_event(p.device, sig, p.delegation, me.pubkey, 1_800_000_000))
    again = await bot.ingest(build_private_signal_event(p.device, sig, p.delegation, me.pubkey, 1_800_000_000))
    assert first and again and first.status == "accepted"
    assert again.event_id == first.event_id  # same commitment = same signal: the second ciphertext is a duplicate
    assert len(bot.ledger.rows()) == 1 and bot.summary()["filled"] == 1


async def test_held_signals_wait_for_the_missing_one_only(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, reorder_wait_s=5.0)
    e1 = event(p, 1, None)
    e2 = event(p, 2, e1.id)
    e4 = event(p, 4, "b" * 64)
    await bot.ingest(e4)
    await bot.ingest(e1)  # seq 2 is still missing: seq 4 stays held
    rows = {r.seq: r.status for r in bot.ledger.rows()}
    assert rows == {1: "accepted", 4: "pending"}
    await bot.ingest(e2)
    assert {r.seq: r.status for r in bot.ledger.rows()}[2] == "accepted"
    assert json.loads(json.dumps(bot.summary()))["pending"] == 1
