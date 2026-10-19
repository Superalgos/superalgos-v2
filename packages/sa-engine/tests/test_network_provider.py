"""Provider bot against an in-process relay: chain persistence, fail-closed signing, private mode, anchors."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sa_core.bundle import check_bundle
from sa_core.governance import proof_from_attestation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import ANCHOR_KIND, COMMITMENT_KIND, RULESET_KIND, SIGNAL_KIND, ProviderRuleSet
from sa_core.power import validate_ruleset_event
from sa_core.signals import check_bar, check_reveal, open_private_signal, verify_commitment_event, verify_signal_event

from sa_engine.network.entitlement import Entitled
from sa_engine.network.journal import ProviderStore
from sa_engine.network.provider import EntitlementSource, ProviderBot, ProviderError, Stamper, replay_run_id
from sa_engine.network.replay import ReplaySpec
from tests.fake_relay import FakeRelay
from tests.net_helpers import FakeStamper, Identity, intent


class Fixed:
    def __init__(self, who: list[Entitled]) -> None:
        self.who = who
        self.calls = 0

    async def refresh(self) -> list[Entitled]:
        self.calls += 1
        return self.who


def bot(
    ident: Identity,
    relay: FakeRelay,
    tmp: Path,
    *,
    rules: ProviderRuleSet | None = None,
    entitlements: EntitlementSource | None = None,
    stamper: Stamper | None = None,
    entitlement_refresh_s: float = 5.0,
    retry_backoff_s: float = 0.5,
) -> ProviderBot:
    return ProviderBot(
        device=ident.device, delegation=ident.delegation, rules=rules or ident.rules(), strategy_id="strat",
        store=ProviderStore(tmp / "state"), relay_url=relay.url, entitlements=entitlements, stamper=stamper,
        entitlement_refresh_s=entitlement_refresh_s, retry_backoff_s=retry_backoff_s,
    )  # fmt: skip


async def settle(b: ProviderBot) -> None:
    await b.drain(anchor_timeout_s=5)


@pytest.fixture
async def relay() -> AsyncIterator[FakeRelay]:
    r = FakeRelay()
    await r.start()
    yield r
    await r.stop()


async def test_registers_then_publishes_a_verifiable_chain(relay: FakeRelay, tmp_path: Path) -> None:
    ident = Identity()
    async with bot(ident, relay, tmp_path) as b:
        await b.publish_ruleset()
        for i in range(3):
            b.enqueue(b.build_events(intent(ts=1_700_000_000_000 + i)))
        await settle(b)
    ruleset = next(e for e in relay.events.values() if e.kind == RULESET_KIND)
    assert validate_ruleset_event(ruleset, int(time.time())).provider == ident.pubkey
    signals = sorted((e for e in relay.events.values() if e.kind == SIGNAL_KIND), key=lambda e: e.created_at)
    parsed = [verify_signal_event(e, int(time.time()), ident.sa.address) for e in signals]
    assert sorted(p.signal.seq for p in parsed) == [1, 2, 3]
    by_seq = {p.signal.seq: p for p in parsed}
    assert by_seq[1].signal.prev is None
    assert by_seq[2].signal.prev == by_seq[1].event.id and by_seq[3].signal.prev == by_seq[2].event.id
    assert b.stats.sent == 3 and not b.stats.rejected
    assert relay.auth_events and relay.auth_events[0].tag_values("sa-delegation")  # AUTH carries the delegation


async def test_chain_survives_a_restart(relay: FakeRelay, tmp_path: Path) -> None:
    ident = Identity()
    async with bot(ident, relay, tmp_path) as b:
        b.enqueue(b.build_events(intent()))
        b.enqueue(b.build_events(intent()))
        await settle(b)
        last = b.store.head("strat")
    assert last is not None and last.seq == 2
    async with bot(ident, relay, tmp_path) as again:  # new process, same state directory
        events = again.build_events(intent(action="close", side="sell", size=1.0))
        again.enqueue(events)
        await settle(again)
    signal = verify_signal_event(events[0], int(time.time()))
    assert signal.signal.seq == 3 and signal.signal.prev == last.ref


async def test_fail_closed_no_signal_means_no_order(
    relay: FakeRelay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = Identity()
    async with bot(ident, relay, tmp_path) as b:
        with pytest.raises(ProviderError, match="binds 3600s"):
            b.build_events(intent(timeframe=900))
        assert b.store.head("strat") is None  # nothing journaled for the refused intent

        def boom(*_: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(b.store, "append", boom)
        with pytest.raises(ProviderError, match="disk full"):
            b.live_hook()(intent())
        monkeypatch.undo()
        stale_intent = intent(order_type="market")
        object.__setattr__(stale_intent, "size_fraction", 7.0)  # an intent the signal model refuses
        with pytest.raises(ProviderError, match="not a valid signal"):
            b.build_events(stale_intent)


async def test_constructor_refuses_mismatched_keys(relay: FakeRelay, tmp_path: Path) -> None:
    ident, other = Identity(), Identity()
    with pytest.raises(ProviderError, match="register with the device key"):
        bot(ident, relay, tmp_path, rules=other.rules())
    with pytest.raises(ProviderError, match="create a delegation for this key"):
        ProviderBot(
            device=ident.device, delegation=other.delegation, rules=ident.rules(), strategy_id="s",
            store=ProviderStore(tmp_path / "x"), relay_url=relay.url,
        )  # fmt: skip
    with pytest.raises(ProviderError, match="entitlement source"):
        bot(ident, relay, tmp_path, rules=ident.private_rules())
    b = bot(ident, relay, tmp_path)
    with pytest.raises(ProviderError, match="start"):
        await b.publish_ruleset()
    with pytest.raises(ProviderError, match="start"):
        b.enqueue([])


async def test_ruleset_refusal_is_an_error(tmp_path: Path) -> None:
    refusing = FakeRelay(reject=lambda ev: "blocked: no" if ev.kind == RULESET_KIND else None)
    await refusing.start()
    try:
        async with bot(Identity(), refusing, tmp_path) as b:
            with pytest.raises(ProviderError, match="refused the rule set: blocked: no"):
                await b.publish_ruleset()
    finally:
        await refusing.stop()


async def test_rejected_events_stay_unacked_and_go_out_on_the_next_start(tmp_path: Path) -> None:
    ident = Identity()
    closed = True
    relay = FakeRelay(reject=lambda ev: "invalid: relay maintenance" if closed else None)
    await relay.start()
    try:
        async with bot(ident, relay, tmp_path) as b:
            b.enqueue(b.build_events(intent()))
            await settle(b)
            assert len(b.stats.rejected) == 1 and "maintenance" in b.stats.rejected[0][1] and b.stats.sent == 0
        closed = False
        async with bot(ident, relay, tmp_path) as again:
            await settle(again)
            assert again.stats.sent == 1 and again.store.unacked() == []
        assert len([e for e in relay.events.values() if e.kind == SIGNAL_KIND]) == 1
    finally:
        await relay.stop()


async def test_rate_limited_events_are_retried(tmp_path: Path) -> None:
    n = 0

    def limit(ev: object) -> str | None:
        nonlocal n
        n += 1
        return "rate-limited: slow down" if n == 1 else None

    relay = FakeRelay(reject=limit)
    await relay.start()
    try:
        async with bot(Identity(), relay, tmp_path) as b:
            b.enqueue(b.build_events(intent()))
            await settle(b)
            assert b.stats.sent == 1 and not b.stats.rejected
    finally:
        await relay.stop()


async def test_reconnects_when_the_relay_drops_the_connection(relay: FakeRelay, tmp_path: Path) -> None:
    async with bot(Identity(), relay, tmp_path) as b:
        b.enqueue(b.build_events(intent()))
        await settle(b)
        await relay.drop_connections()
        await asyncio.sleep(0.1)
        b.enqueue(b.build_events(intent()))
        await settle(b)
        assert b.stats.sent == 2 and not b.stats.rejected


async def test_unreachable_relay_is_reported_not_swallowed(tmp_path: Path) -> None:
    relay = FakeRelay()
    await relay.start()
    ident = Identity()
    b = bot(ident, relay, tmp_path, retry_backoff_s=0.01)
    await b.start()
    await relay.stop()  # relay goes away for good
    await relay.drop_connections()
    b.enqueue(b.build_events(intent()))
    assert b._outbox is not None
    await asyncio.wait_for(b._outbox.join(), 30)
    assert b.stats.sent == 0 and "unreachable" in b.stats.rejected[0][1]
    await b.close()


async def test_private_mode_commits_publicly_and_encrypts_for_each_entitled_follower(
    relay: FakeRelay, tmp_path: Path
) -> None:
    ident, f1, f2 = Identity(), Identity(), Identity()
    who = [Entitled(f1.pubkey, f1.sa.address, 1), Entitled(f2.pubkey, f2.sa.address, 1)]
    async with bot(
        ident, relay, tmp_path, rules=ident.private_rules(), entitlements=Fixed(who), stamper=FakeStamper()
    ) as b:
        for i in range(2):
            b.enqueue(b.build_events(intent(ts=1_700_000_000_000 + i)))
        await settle(b)
        full, bare = b.export(reveal=True), b.export(reveal=False)
    commits = sorted((e for e in relay.events.values() if e.kind == COMMITMENT_KIND), key=lambda e: e.created_at)
    ciphers = [e for e in relay.events.values() if e.kind == SIGNAL_KIND]
    assert len(commits) == 2 and len(ciphers) == 4
    assert all("size_fraction" not in e.content for e in ciphers)  # nothing readable on the relay
    c1, _ = verify_commitment_event(commits[0], int(time.time()))
    for follower in (f1, f2):
        mine = [e for e in ciphers if ("p", follower.pubkey) in e.tags]
        assert len(mine) == 2
        opened = [open_private_signal(e, follower.device, int(time.time())) for e in mine]
        assert sorted(o.signal.seq for o in opened) == [1, 2]
    assert c1.seq == 1
    # the chain of a private provider runs over commitment ids; reveal turns commitments into signals
    assert check_bundle(full).complete and not check_bundle(full).unrevealed
    assert len(check_bundle(bare).unrevealed) == 2
    for rev in full.reveals:
        commit = next(e for e in full.events if e.id == rev.commitment_id)
        check_reveal(verify_commitment_event(commit, int(time.time()))[0], rev.plaintext)


async def test_private_mode_with_nobody_entitled_still_commits(relay: FakeRelay, tmp_path: Path) -> None:
    ident = Identity()
    ent = Fixed([])
    async with bot(
        ident, relay, tmp_path, rules=ident.private_rules(), entitlements=ent, entitlement_refresh_s=0.05
    ) as b:
        b.enqueue(b.build_events(intent()))
        await settle(b)
        await asyncio.sleep(0.2)  # the refresh loop runs
    assert ent.calls >= 2
    assert [e.kind for e in relay.events.values()] == [COMMITMENT_KIND]


async def test_live_hook_signs_synchronously_from_another_thread(relay: FakeRelay, tmp_path: Path) -> None:
    async with bot(Identity(), relay, tmp_path) as b:
        hook = b.live_hook()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                hook(intent())
                hook(intent(action="close", side="sell", size=1.0))
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        await asyncio.to_thread(t.join)
        assert not errors
        await settle(b)
        head = b.store.head("strat")
        assert head is not None and head.seq == 2 and b.stats.sent == 2


async def test_every_chain_event_is_anchored_and_attested(relay: FakeRelay, tmp_path: Path) -> None:
    stamper = FakeStamper(fail_first=1)
    async with bot(Identity(), relay, tmp_path, stamper=stamper) as b:
        events = b.build_events(intent()) + b.build_events(intent())
        b.enqueue(events)
        await settle(b)
        proofs = b.store.proofs()
    assert stamper.calls and len(stamper.calls[0]) == 2  # one batch holds both signals
    assert len(stamper.calls) == 2 and b.stats.anchor_errors == ["fake TSA is down"] and b.stats.anchored == 2
    assert {p.digest for p in proofs} == {e.id for e in events}
    attestations = [e for e in relay.events.values() if e.kind == ANCHOR_KIND]
    assert {proof_from_attestation(a).digest for a in attestations} == {e.id for e in events}
    assert b.export().proofs == tuple(sorted(proofs, key=lambda p: (p.digest, p.kind, p.data)))


async def test_unanchored_signals_are_anchored_after_a_restart(relay: FakeRelay, tmp_path: Path) -> None:
    ident = Identity()
    async with bot(ident, relay, tmp_path) as b:  # no stamper: signed and sent, never anchored
        b.enqueue(b.build_events(intent()))
        await settle(b)
    stamper = FakeStamper()
    async with bot(ident, relay, tmp_path, stamper=stamper) as again:
        await settle(again)
    assert len(stamper.calls) == 1 and len(again.store.proofs()) == 1


async def test_stamper_waits_its_turn(relay: FakeRelay, tmp_path: Path) -> None:
    stamper = FakeStamper()
    stamper.wait = 60.0  # a TSA asked us to wait
    async with bot(Identity(), relay, tmp_path, stamper=stamper) as b:
        b.enqueue(b.build_events(intent()))
        await asyncio.sleep(0.6)
        assert stamper.calls == []
        stamper.wait = 0.0
        await settle(b)
    assert len(stamper.calls) == 1


async def test_replay_publishes_on_the_replay_clock_and_resumes(relay: FakeRelay, tmp_path: Path) -> None:
    ident = Identity()
    spec = ReplaySpec(
        instrument="BTCUSDT", start="2023-11-14", end="2023-11-15", speed=10, wall0_ms=int(time.time() * 1000) + 100
    )
    intents = [intent(ts=spec.clock.hist0_ms + i * 1000) for i in range(4)]  # 1 s of market time apart = 100 ms
    run = replay_run_id("{}", spec)
    async with bot(ident, relay, tmp_path) as b:
        t0 = time.time()
        await b.run_replay(intents[:2], spec, run)
        await settle(b)
        assert time.time() - t0 >= 0.1  # waited for wall0, then 100 ms for the second intent
        assert b.store.progress(run) == 2
    async with bot(ident, relay, tmp_path) as again:
        used = await again.run_replay(intents, spec, run)  # restart: skips the two already signed
        await settle(again)
    assert used.wall0_ms != spec.wall0_ms  # the clock was re-anchored so the next intent was due at once
    seqs = sorted(
        verify_signal_event(e, int(time.time())).signal.seq for e in relay.events.values() if e.kind == SIGNAL_KIND
    )
    assert seqs == [1, 2, 3, 4]
    for e in relay.events.values():
        if e.kind == SIGNAL_KIND:  # SEP-3.18: replayed signals name their bar on the wall clock, like created_ms
            check_bar(verify_signal_event(e, int(time.time())).signal, 3600, 10_000, 10_000)
    assert replay_run_id("{}", spec) == run != replay_run_id("{x}", spec)


async def test_private_signals_are_not_sent_to_the_wrong_key(relay: FakeRelay, tmp_path: Path) -> None:
    ident, f1, stranger = Identity(), Identity(), Identity()
    async with bot(
        ident, relay, tmp_path, rules=ident.private_rules(), entitlements=Fixed([Entitled(f1.pubkey, f1.sa.address, 1)])
    ) as b:
        b.enqueue(b.build_events(intent()))
        await settle(b)
    cipher = next(e for e in relay.events.values() if e.kind == SIGNAL_KIND)
    from sa_core.signals import SignalError

    with pytest.raises(SignalError, match="cannot decrypt"):
        open_private_signal(cipher, stranger.device, int(time.time()))


def test_throwaway_keys_only() -> None:
    assert SAKey.generate().address != SAKey.generate().address
    assert DeviceKey.generate().pubkey_hex != DeviceKey.generate().pubkey_hex


def _offline_bot(tmp: Path, ident: Identity) -> ProviderBot:
    return ProviderBot(
        device=ident.device, delegation=ident.delegation, rules=ident.rules(), strategy_id="strat",
        store=ProviderStore(tmp / "state"), relay_url="ws://unused",
    )  # fmt: skip


def test_sep_3_18_a_signal_names_the_bar_its_intent_was_decided_on(tmp_path: Path) -> None:
    ident = Identity()
    b = _offline_bot(tmp_path, ident)
    close = 1_700_000_000_000
    (ev,) = b.build_events(intent(ts=close), now_ms=close + 250)
    signal = verify_signal_event(ev, close // 1000 + 1).signal
    assert signal.v == 2 and signal.created_ms == close + 250
    assert signal.bar_open_ms == close - 3_600_000  # the intent's ts is the bar close; the signal names the open
    check_bar(signal, 3600, 10_000, 10_000)


def test_sep_3_18_a_replayed_bar_close_is_mapped_like_created_ms(tmp_path: Path) -> None:
    ident = Identity()
    b = _offline_bot(tmp_path, ident)
    wall_close = 1_800_000_000_000  # the intent's market-time close, mapped onto the wall clock
    (ev,) = b.build_events(intent(ts=1_700_000_000_000), now_ms=wall_close, bar_close_ms=wall_close)
    signal = verify_signal_event(ev, wall_close // 1000 + 1).signal
    assert signal.bar_open_ms == wall_close - 3_600_000
    check_bar(signal, 3600, 0, 0)  # created exactly at the close: a replayed signal passes the follower's check


def test_sep_3_18_an_intent_with_no_room_for_its_bar_is_refused_with_the_value(tmp_path: Path) -> None:
    b = _offline_bot(tmp_path, Identity())
    with pytest.raises(ProviderError, match=r"closes at 1000 ms"):
        b.build_events(intent(ts=1000))
    assert b.store.head("strat") is None
