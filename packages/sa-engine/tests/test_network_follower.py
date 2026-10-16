"""Follower bot: every check of the verification pipeline, file signals, anchors, the relay session."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sa_core.anchors import verifier_map
from sa_core.delegation import revocation_event, sign_delegation, sign_revocation
from sa_core.governance import build_anchor_attestation
from sa_core.keys import SAKey
from sa_core.models import ALLOCATION_KIND, AnchorProof, NostrEvent, ProviderRuleSet, Signal
from sa_core.nostr import build_event
from sa_core.power import allocation_event, ruleset_event, sign_allocation, validate_allocation_event
from sa_core.signals import build_commitment_event, build_private_signal_event, build_signal_event

from sa_engine.network.executor import SimulatedPaperExecutor
from sa_engine.network.follower import FollowerBot, FollowerError
from sa_engine.network.ledger import Ledger
from tests.fake_relay import FakeRelay
from tests.net_helpers import FakeAnchor, Identity, flat_feed

NOW = 1_800_000_000.0
NOW_MS = int(NOW * 1000)


def signal(p: Identity, seq: int, prev: str | None, *, at_ms: int = NOW_MS, **kw: Any) -> Signal:
    base: dict[str, Any] = {
        "strategy_id": "strat", "seq": seq, "prev": prev, "instrument": "BINANCE:BTCUSDT", "side": "buy",
        "action": "open", "size_fraction": 0.5, "order_type": "market", "timeframe": 3600, "created_ms": at_ms,
    }  # fmt: skip
    return Signal.model_validate({**base, **kw})


def event(p: Identity, seq: int, prev: str | None, **kw: Any) -> NostrEvent:
    return build_signal_event(p.device, signal(p, seq, prev, **kw), p.delegation)


def follower(
    tmp: Path,
    provider: Identity,
    *,
    rules: ProviderRuleSet | Literal["own"] | None = "own",
    verifiers: dict[str, Any] | None = None,
    relay_url: str = "ws://127.0.0.1:1",
    clock_s: float = NOW,
    me: Identity | None = None,
    reorder_wait_s: float = 0.0,
    **kw: Any,
) -> FollowerBot:
    me = me or Identity()
    rule_map = {provider.pubkey: provider.rules()} if rules == "own" else ({provider.pubkey: rules} if rules else {})
    t_ms = int(clock_s * 1000)
    return FollowerBot(
        device=me.device, delegation=me.delegation, relay_url=relay_url, providers=[provider.pubkey],
        executor=SimulatedPaperExecutor("BINANCE:BTCUSDT", 10_000.0, flat_feed(50_000.0, t_ms - 1000)),
        ledger=Ledger(tmp / "ledger.sqlite"), verifiers=verifiers, rules=rule_map, clock=lambda: clock_s, reorder_wait_s=reorder_wait_s, **kw,
    )  # fmt: skip


async def test_accepts_a_valid_chain_and_places_paper_orders_sized_by_the_contract(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    e1 = event(p, 1, None)
    e2 = event(p, 2, e1.id, action="close", side="sell", size_fraction=1.0)
    r1, r2 = await bot.ingest(e1), await bot.ingest(e2)
    assert r1 and r1.status == "accepted" and r1.placed and r1.filled
    assert r1.qty == "0.1" and r1.equity_before == 10_000.0 and r1.price == 50_000.0  # 0.5 x equity / price
    assert r1.reason == "chain baseline at seq 1" and r1.received_ms == NOW_MS
    assert r2 and r2.status == "accepted" and r2.qty == "0.1"  # close = fraction of the own position
    assert (await bot.ingest(e1)).event_id == e1.id  # type: ignore[union-attr]  # redelivery is not executed twice
    assert len(bot.ledger.rows()) == 2
    s = bot.summary()
    assert s["accepted"] == 2 and s["filled"] == 2 and s["rejected"] == 0


async def test_unknown_providers_and_other_kinds_are_ignored(tmp_path: Path) -> None:
    p, other = Identity(), Identity()
    bot = follower(tmp_path, p)
    assert await bot.ingest(event(other, 1, None)) is None
    assert await bot.ingest(build_event(p.device, 1, "a note", [], int(NOW))) is None
    assert bot.ledger.rows() == []


@pytest.mark.parametrize(
    ("label", "mutate", "expect"),
    [
        (
            "tampered",
            lambda e: e.model_copy(update={"content": e.content.replace("0.5", "0.9")}),
            "verification failed",
        ),
        ("bad signature", lambda e: e.model_copy(update={"sig": "0" * 128}), "verification failed"),
    ],
)
async def test_sep_3_4_altered_events_are_rejected_and_never_executed(
    tmp_path: Path, label: str, mutate: Any, expect: str
) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    row = await bot.ingest(mutate(event(p, 1, None)))
    assert row and row.status == "rejected" and expect in row.reason and not row.placed, label
    assert bot.executor.snapshot(NOW_MS)["fills"] == 0


async def test_expired_and_foreign_delegations_are_rejected(tmp_path: Path) -> None:
    p = Identity(expires_at=int(NOW) - 10)
    bot = follower(tmp_path, p, rules=p.rules())
    row = await bot.ingest(event(p, 1, None))
    assert row and row.status == "rejected" and "delegation" in row.reason
    q, imposter = Identity(), Identity()
    bot2 = follower(tmp_path / "b", q, rules=q.rules())
    forged = build_signal_event(
        q.device, signal(q, 1, None), imposter.delegation.model_copy(update={"nostr_pubkey": q.pubkey})
    )
    row2 = await bot2.ingest(forged)
    assert row2 and row2.status == "rejected" and "verification failed" in row2.reason
    # a delegation from another SA identity than the rule set names
    other_sa = Identity()
    mismatch = build_signal_event(q.device, signal(q, 1, None), sign_delegation(other_sa.sa, q.pubkey, 4_000_000_000))
    row3 = await bot2.ingest(mismatch)
    assert row3 and row3.status == "rejected" and "verification failed" in row3.reason


async def test_sep_3_40_timeframe_must_match_the_rule_set(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    row = await bot.ingest(event(p, 1, None, timeframe=60))
    assert row and row.status == "rejected" and "timeframe" in row.reason and "3600" in row.reason


async def test_stale_and_future_signals_are_rejected(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    old = await bot.ingest(event(p, 1, None, at_ms=NOW_MS - 4 * 3600 * 1000))
    assert old and old.status == "rejected" and old.reason.startswith("stale")
    future = await bot.ingest(event(p, 1, None, at_ms=NOW_MS + 60_000))
    assert future and future.status == "rejected" and future.reason.startswith("from the future")
    lenient = follower(tmp_path / "x", p, max_age_s=10 * 3600)
    ok = await lenient.ingest(event(p, 1, None, at_ms=NOW_MS - 4 * 3600 * 1000))
    assert ok and ok.status == "accepted"


async def test_chain_gap_fork_and_replay(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    e1 = event(p, 1, None)
    await bot.ingest(e1)
    e3 = event(p, 3, "a" * 64)
    gap = await bot.ingest(e3)
    assert gap and gap.status == "rejected" and gap.reason == "gap: seq 2..2 missing before seq 3" and not gap.placed
    e4 = event(p, 4, e3.id)  # the gap moved the head: the next one verifies against the signal after the gap
    nxt = await bot.ingest(e4)
    assert nxt and nxt.status == "accepted"
    fork = await bot.ingest(event(p, 5, "b" * 64))
    assert fork and fork.status == "rejected" and "fork" in fork.reason
    again = await bot.ingest(event(p, 2, e1.id))
    assert again and again.status == "rejected" and "does not follow" in again.reason
    # allow_gaps executes after a gap and says so
    loose = follower(tmp_path / "l", p, allow_gaps=True)
    await loose.ingest(e1)
    ok = await loose.ingest(e3)
    assert ok and ok.status == "accepted" and ok.reason.endswith("(allowed)")


async def test_signals_that_arrive_out_of_order_are_executed_in_chain_order(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, reorder_wait_s=5.0)
    e1 = event(p, 1, None)
    e2 = event(p, 2, e1.id, action="close", side="sell", size_fraction=1.0)
    e3 = event(p, 3, e2.id)
    early = await bot.ingest(e2)  # the relay delivered seq 2 before seq 1 (their timers fired together)
    assert early and early.status == "pending" and "out of order" in early.reason and not early.placed
    assert (await bot.ingest(e3)).status == "pending"  # type: ignore[union-attr]
    await bot.ingest(e1)
    rows = {r.seq: r for r in bot.ledger.rows()}
    assert [rows[i].status for i in (1, 2, 3)] == ["accepted"] * 3
    assert rows[1].reason == "chain baseline at seq 1"
    assert isinstance(bot.executor, SimulatedPaperExecutor)
    assert [f.ref for f in bot.executor.fills] == [e1.id, e2.id, e3.id]  # executed in chain order, not arrival order
    assert rows[2].received_ms == NOW_MS  # the ledger keeps the real receive time of the early arrival
    assert not bot._held and not bot._timers


async def test_a_missing_predecessor_becomes_a_gap_after_the_wait(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, reorder_wait_s=0.1)
    e1 = event(p, 1, None)
    await bot.ingest(e1)
    e3 = event(p, 3, "d" * 64)
    e4 = event(p, 4, e3.id)
    assert (await bot.ingest(e3)).status == "pending"  # type: ignore[union-attr]
    assert (await bot.ingest(e4)).status == "pending"  # type: ignore[union-attr]
    await asyncio.sleep(0.4)
    rows = {r.seq: r for r in bot.ledger.rows()}
    assert rows[3].status == "rejected" and rows[3].reason.startswith("gap:")
    assert rows[4].status == "accepted"  # the gap moved the head, so seq 4 verifies against seq 3


async def test_joining_mid_chain_waits_once_then_takes_the_first_signal_as_the_baseline(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, reorder_wait_s=0.1)
    e57 = event(p, 57, "e" * 64)
    assert (await bot.ingest(e57)).status == "pending"  # type: ignore[union-attr]
    await asyncio.sleep(0.4)
    row = bot.ledger.get(e57.id)
    assert row and row.status == "accepted" and row.reason == "chain baseline at seq 57"


async def test_signal_files_never_wait_for_predecessors(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, reorder_wait_s=5.0)
    row = await bot.ingest_text(event(p, 9, "f" * 64).model_dump_json())
    assert row.status == "accepted" and row.reason == "chain baseline at seq 9"


async def test_chain_head_survives_a_restart(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    e1 = event(p, 1, None)
    await bot.ingest(e1)
    bot.ledger.close()
    revived = follower(tmp_path, p)  # same ledger file
    bad = await revived.ingest(event(p, 2, "c" * 64))
    assert bad and bad.status == "rejected" and "fork" in bad.reason
    good = await revived.ingest(event(p, 2, e1.id))
    assert good and good.status == "accepted"


async def test_a_signal_the_paper_account_cannot_execute_is_rejected(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    row = await bot.ingest(event(p, 1, None, limit_price=50_000.0))  # a market order cannot carry a limit price
    assert row and row.status == "rejected" and "cannot be executed" in row.reason
    sell = await bot.ingest(event(p, 1, None, side="sell"))  # accepted, but a short open is refused by the account
    assert sell and sell.status == "accepted" and sell.placed is False and "short selling" in sell.exec_reason


async def test_no_rule_set_for_a_relayed_signal_is_rejected_but_files_may_lack_one(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, rules=None)
    row = await bot.ingest(event(p, 1, None))
    assert row and row.status == "rejected" and "no rule set" in row.reason
    text = event(p, 1, None, at_ms=NOW_MS + 1).model_dump_json()
    fileres = await bot.ingest_text(text, provider=p.pubkey)
    assert fileres.status == "accepted" and fileres.source == "file"


# ---------------------------------------------------------------- off-grid files (SEP-3.29)


async def test_sep_3_30_a_valid_signal_file_is_executed(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    row = await bot.ingest_text(event(p, 1, None).model_dump_json(), provider=p.pubkey)
    assert row.status == "accepted" and row.filled and row.source == "file"


async def test_sep_3_31_tampered_or_wrongly_delegated_files_are_rejected(tmp_path: Path) -> None:
    p, other = Identity(), Identity()
    bot = follower(tmp_path, p)
    good = event(p, 1, None)
    tampered = good.model_copy(update={"content": good.content.replace('"buy"', '"sell"')})
    assert (await bot.ingest_text(tampered.model_dump_json())).status == "rejected"
    wrong = build_signal_event(
        p.device, signal(p, 2, good.id), other.delegation.model_copy(update={"nostr_pubkey": p.pubkey})
    )
    assert (await bot.ingest_text(wrong.model_dump_json())).status == "rejected"
    with pytest.raises(FollowerError, match="not a Nostr event"):
        await bot.ingest_text("hello")
    with pytest.raises(FollowerError, match="pinned provider"):
        await bot.ingest_text(good.model_dump_json(), provider=other.pubkey)
    with pytest.raises(FollowerError, match=r"kind 9317"):
        await bot.ingest_text(build_event(p.device, 1, "x", [], int(NOW)).model_dump_json())
    assert bot.executor.snapshot(NOW_MS)["fills"] == 0


# ---------------------------------------------------------------- private signals


async def test_sep_3_26_private_signal_waits_for_its_commitment_then_executes(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    rules = p.private_rules()
    bot = follower(tmp_path, p, rules=rules, me=me)
    sig = signal(p, 1, None)
    commit = build_commitment_event(p.device, sig, p.delegation, int(NOW))
    cipher = build_private_signal_event(p.device, sig, p.delegation, me.pubkey, int(NOW))
    pending = await bot.ingest(cipher)
    assert pending and pending.status == "pending" and not pending.placed
    done = await bot.ingest(commit)  # the commitment arrives: the waiting signal is processed
    assert done and done.status == "accepted" and done.filled and done.ref == commit.id
    sig2 = signal(p, 2, commit.id)
    commit2 = build_commitment_event(p.device, sig2, p.delegation, int(NOW))
    await bot.ingest(commit2)  # commitment first this time
    nxt = await bot.ingest(build_private_signal_event(p.device, sig2, p.delegation, me.pubkey, int(NOW)))
    assert nxt and nxt.status == "accepted" and nxt.prev == commit.id and nxt.ref == commit2.id


async def test_sep_3_27_a_private_signal_for_someone_else_cannot_be_opened(tmp_path: Path) -> None:
    p, me, other = Identity(), Identity(), Identity()
    bot = follower(tmp_path, p, rules=p.private_rules(), me=me)
    cipher = build_private_signal_event(p.device, signal(p, 1, None), p.delegation, other.pubkey, int(NOW))
    row = await bot.ingest(cipher)
    assert row and row.status == "rejected" and "cannot decrypt" in row.reason


async def test_a_commitment_that_does_not_match_is_not_trusted(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    bot = follower(tmp_path, p, rules=p.private_rules(), me=me)
    sig = signal(p, 1, None)
    liar = build_commitment_event(p.device, signal(p, 1, None, size_fraction=0.9), p.delegation, int(NOW))
    await bot.ingest(liar)
    row = await bot.ingest(build_private_signal_event(p.device, sig, p.delegation, me.pubkey, int(NOW)))
    assert row and row.status == "pending"  # the digest differs: still waiting for the real commitment
    junk = build_event(p.device, 9318, "not json", [], int(NOW))
    assert await bot.ingest(junk) is None
    stranger = Identity()
    assert await bot.ingest(build_commitment_event(stranger.device, sig, stranger.delegation, int(NOW))) is None


async def test_private_file_signals_skip_the_commitment_chain_check(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    bot = follower(tmp_path, p, rules=None, me=me)
    cipher = build_private_signal_event(p.device, signal(p, 1, None), p.delegation, me.pubkey, int(NOW))
    row = await bot.ingest_text(cipher.model_dump_json(), provider=p.pubkey)
    assert row.status == "accepted" and "not checked" in row.reason


# ---------------------------------------------------------------- anchors


async def test_anchors_are_checked_when_they_arrive(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, verifiers=verifier_map(FakeAnchor(not_after=int(NOW) + 5)))
    e1 = event(p, 1, None)
    row = await bot.ingest(e1)
    assert row and row.anchor_status == "pending"
    await bot.ingest(build_anchor_attestation(p.device, AnchorProof(kind="fake", digest=e1.id, data="{}"), int(NOW)))
    got = bot.ledger.get(e1.id)
    assert got and got.anchor_status == "ok" and got.anchored_s == int(NOW) + 5
    e2 = event(p, 2, e1.id)
    await bot.ingest(build_anchor_attestation(p.device, AnchorProof(kind="fake", digest=e2.id, data="{}"), int(NOW)))
    await bot.ingest(e2)  # proof first, signal second
    assert (bot.ledger.get(e2.id) or row).anchor_status == "ok"


async def test_late_and_forged_anchors_are_recorded_not_trusted(tmp_path: Path) -> None:
    p = Identity()
    late = follower(tmp_path / "a", p, verifiers=verifier_map(FakeAnchor(not_after=int(NOW) + 600)))
    e = event(p, 1, None)
    await late.ingest(e)
    await late.ingest(build_anchor_attestation(p.device, AnchorProof(kind="fake", digest=e.id, data="{}")))
    assert (late.ledger.get(e.id) or None) and late.ledger.get(e.id).anchor_status == "late"  # type: ignore[union-attr]
    forged = follower(tmp_path / "b", p, verifiers=verifier_map(FakeAnchor()))
    await forged.ingest(e)
    await forged.ingest(build_anchor_attestation(p.device, AnchorProof(kind="fake", digest=e.id, data="forged")))
    assert forged.ledger.get(e.id).anchor_status.startswith("invalid")  # type: ignore[union-attr]
    nobody = follower(tmp_path / "c", p)
    await nobody.ingest(e)
    await nobody.ingest(build_anchor_attestation(p.device, AnchorProof(kind="fake", digest=e.id, data="{}")))
    assert "no verifier" in nobody.ledger.get(e.id).anchor_status  # type: ignore[union-attr]
    junk = build_event(p.device, 1040, "{}", [("e", e.id)], int(NOW))  # missing the sa-anchor tag
    await nobody.ingest(junk)


# ---------------------------------------------------------------- the relay session


@pytest.fixture
async def relay() -> AsyncIterator[FakeRelay]:
    r = FakeRelay()
    await r.start()
    yield r
    await r.stop()


async def run_briefly(bot: FollowerBot, seconds: float) -> None:
    await bot.run(asyncio.Event(), duration_s=seconds)


async def test_session_loads_rules_subscribes_and_executes(relay: FakeRelay, tmp_path: Path) -> None:
    p = Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    now = time.time()
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url, clock_s=now)
    bot.executor = SimulatedPaperExecutor("BINANCE:BTCUSDT", 10_000.0, flat_feed(50_000.0, int(now * 1000) - 1000))
    bot.clock = time.time
    task = asyncio.create_task(run_briefly(bot, 2.0))
    await asyncio.wait_for(bot.ready.wait(), 5)
    e1 = build_signal_event(p.device, signal(p, 1, None, at_ms=int(time.time() * 1000)), p.delegation)
    await relay.inject(e1)
    await task
    assert bot.authenticated and relay.auth_events and relay.auth_events[0].tag_values("sa-delegation")
    row = bot.ledger.get(e1.id)
    assert row and row.status == "accepted" and row.filled


async def test_session_publishes_the_allocation_once(relay: FakeRelay, tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    alloc = sign_allocation(me.sa, 1, 0, {p.pubkey: 10_000})
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url, me=me, allocation=alloc, clock_s=time.time())
    await run_briefly(bot, 0.6)
    await run_briefly(bot, 0.6)  # a second start must not publish a second event with the same nonce
    allocs = [e for e in relay.events.values() if e.kind == ALLOCATION_KIND]
    assert len(allocs) == 1 and allocs[0].pubkey == me.pubkey
    assert allocation_event(me.device, alloc, me.delegation).kind == ALLOCATION_KIND
    assert validate_allocation_event(allocs[0]) == alloc  # it carries the follower's sa-delegation tag (H1)


async def test_a_refused_allocation_stops_the_follower_with_the_reason(tmp_path: Path) -> None:
    refusing = FakeRelay(
        reject=lambda ev: "invalid: nonce 1 is below the highest accepted" if ev.kind == ALLOCATION_KIND else None
    )
    await refusing.start()
    try:
        p, me = Identity(), Identity()
        await refusing.inject(ruleset_event(p.device, p.rules(), p.delegation))
        bot = follower(
            tmp_path,
            p,
            rules=None,
            relay_url=refusing.url,
            me=me,
            allocation=sign_allocation(me.sa, 1, 0, {p.pubkey: 1}),
        )
        with pytest.raises(FollowerError, match="refused the allocation"):
            await run_briefly(bot, 1.0)
    finally:
        await refusing.stop()


async def test_missing_or_forged_rule_set_is_a_clear_error(relay: FakeRelay, tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url)
    with pytest.raises(FollowerError, match="has no rule set"):
        await run_briefly(bot, 1.0)
    forged = ruleset_event(p.device, p.rules().model_copy(update={"timeframe": 60}), p.delegation)
    await relay.inject(forged)
    bot2 = follower(tmp_path / "2", p, rules=None, relay_url=relay.url)
    with pytest.raises(FollowerError, match="invalid"):
        await run_briefly(bot2, 1.0)


async def test_follower_without_auth_still_runs(tmp_path: Path) -> None:
    open_relay = FakeRelay(challenge=None)
    await open_relay.start()
    try:
        p = Identity()
        await open_relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
        bot = follower(tmp_path, p, rules=None, relay_url=open_relay.url, clock_s=time.time())
        await run_briefly(bot, 6.5)  # AUTH wait (5 s) then zero-power service
        assert not bot.authenticated and bot.ready.is_set()
    finally:
        await open_relay.stop()


async def test_session_reconnects_after_the_relay_drops_it(relay: FakeRelay, tmp_path: Path) -> None:
    p = Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url, clock_s=time.time())
    bot.clock = time.time
    task = asyncio.create_task(run_briefly(bot, 5.0))
    await asyncio.wait_for(bot.ready.wait(), 5)
    await asyncio.sleep(0.2)
    await relay.drop_connections()
    await asyncio.sleep(2.0)  # reconnect after 1 s
    bot.executor = SimulatedPaperExecutor(
        "BINANCE:BTCUSDT", 10_000.0, flat_feed(50_000.0, int(time.time() * 1000) - 1000)
    )
    e1 = build_signal_event(p.device, signal(p, 1, None, at_ms=int(time.time() * 1000)), p.delegation)
    await relay.inject(e1)
    await task
    row = bot.ledger.get(e1.id)
    assert row and row.status == "accepted"


async def test_stop_event_ends_the_run(relay: FakeRelay, tmp_path: Path) -> None:
    p = Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url)
    stop = asyncio.Event()
    task = asyncio.create_task(bot.run(stop))
    await asyncio.wait_for(bot.ready.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_working_orders_fill_through_the_poll_loop(relay: FakeRelay, tmp_path: Path) -> None:
    p = Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    t0 = int(time.time() * 1000)
    from sa_engine.network.replay import PriceFeed
    from tests.net_helpers import candles

    feed = PriceFeed(candles([100.0, 100.0, 90.0], t0, step_ms=700, spread=0.5))
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url, poll_s=0.1)
    bot.clock = time.time
    bot.executor = SimulatedPaperExecutor("BINANCE:BTCUSDT", 1000.0, feed)
    task = asyncio.create_task(run_briefly(bot, 3.0))
    await asyncio.wait_for(bot.ready.wait(), 5)
    resting = build_signal_event(
        p.device, signal(p, 1, None, at_ms=int(time.time() * 1000), order_type="limit", limit_price=95.0), p.delegation
    )
    await relay.inject(resting)
    await task
    row = bot.ledger.get(resting.id)
    assert row and row.placed and row.filled and row.price == 95.0


def test_constructor_rejects_a_delegation_for_another_key(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    other = SAKey.generate()
    with pytest.raises(FollowerError, match="create a delegation for this key"):
        FollowerBot(
            device=me.device, delegation=sign_delegation(other, p.pubkey, 4_000_000_000), relay_url="ws://x",
            providers=[p.pubkey], executor=SimulatedPaperExecutor("BINANCE:BTCUSDT", 1.0, flat_feed()),
            ledger=Ledger(tmp_path / "l.sqlite"),
        )  # fmt: skip


# ---------------------------------------------------------------- properties


@settings(deadline=None, max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(order=st.permutations(range(6)))
def test_property_delivery_order_inside_the_wait_never_changes_what_is_executed(
    order: list[int], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Whatever order a relay delivers a chain in, the follower executes it in seq order, once each."""
    p = Identity()
    events = [event(p, 1, None)]
    for seq in range(2, 7):
        events.append(event(p, seq, events[-1].id, at_ms=NOW_MS + seq))

    async def go() -> list[str]:
        bot = follower(tmp_path_factory.mktemp("perm"), p, reorder_wait_s=30.0)
        for i in order:
            await bot.ingest(events[i])
        assert isinstance(bot.executor, SimulatedPaperExecutor)
        assert all(r.status == "accepted" for r in bot.ledger.rows())
        return [f.ref for f in bot.executor.fills]

    executed = asyncio.run(go())
    assert executed == [e.id for e in events if e.id in executed]  # chain order
    assert len(executed) == len(set(executed))


@settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(position=st.floats(0, 1), replacement=st.sampled_from(list('0123456789abcdefxyz{}[]":,')))
def test_sep_3_31_property_a_signal_file_with_any_character_changed_is_never_executed(
    position: float, replacement: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    p = Identity()
    text = event(p, 1, None).model_dump_json()
    i = min(int(position * len(text)), len(text) - 1)
    if text[i] == replacement:
        return  # not a mutation

    async def go() -> str:
        bot = follower(tmp_path_factory.mktemp("mut"), p, reorder_wait_s=0.0)
        try:
            row = await bot.ingest_text(text[:i] + replacement + text[i + 1 :])
        except FollowerError:
            return "unparsable"
        assert isinstance(bot.executor, SimulatedPaperExecutor)
        assert bot.executor.fills == []
        return row.status

    assert asyncio.run(go()) in {"unparsable", "rejected"}


# ---------------------------------------------------------------- security review M3: revoked device keys


def revoke(owner: Identity, device_pubkey: str, effective_at: int) -> NostrEvent:
    """Anyone may publish it: the event is signed by a throwaway key, the authority is the owner's EIP-191 signature."""
    from sa_core.keys import DeviceKey

    return revocation_event(DeviceKey.generate(), sign_revocation(owner.sa, device_pubkey, effective_at), int(NOW))


async def test_m3_signals_from_a_revoked_provider_device_key_are_rejected(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    e1 = event(p, 1, None)
    ok = await bot.ingest(e1)
    assert ok and ok.status == "accepted"
    assert await bot.ingest(revoke(p, p.pubkey, int(NOW) - 5)) is None
    row = await bot.ingest(event(p, 2, e1.id))
    assert row and row.status == "rejected" and "revoked" in row.reason and not row.placed
    assert bot.executor.snapshot(NOW_MS)["fills"] == 1  # nothing executed after the revocation


async def test_m3_a_revocation_only_counts_from_its_effective_time(tmp_path: Path) -> None:
    p = Identity()
    bot = follower(tmp_path, p)
    await bot.ingest(revoke(p, p.pubkey, int(NOW) + 3600))  # not effective yet
    row = await bot.ingest(event(p, 1, None))
    assert row and row.status == "accepted"


async def test_m3_forged_and_unrelated_revocations_are_ignored(tmp_path: Path) -> None:
    p, stranger, unrelated = Identity(), Identity(), Identity()
    bot = follower(tmp_path, p)
    # signed by a stranger's SA key for p's device key: it does not revoke p's delegation
    await bot.ingest(revoke(stranger, p.pubkey, 0))
    # a revocation of a key this follower does not depend on is not even kept
    await bot.ingest(revoke(unrelated, unrelated.pubkey, 0))
    assert len(bot.revocations) == 1
    forged = revoke(p, p.pubkey, 0).model_copy(update={"content": "{}"})
    await bot.ingest(forged)
    assert len(bot.revocations) == 1
    row = await bot.ingest(event(p, 1, None))
    assert row and row.status == "accepted"  # the stranger's revocation changed nothing


async def test_m3_private_signals_from_a_revoked_key_are_rejected(tmp_path: Path) -> None:
    p, me = Identity(), Identity()
    rules = p.private_rules()
    bot = follower(tmp_path, p, rules=rules, me=me)
    await bot.ingest(revoke(p, p.pubkey, int(NOW) - 1))
    ev = build_private_signal_event(p.device, signal(p, 1, None), p.delegation, me.pubkey)
    row = await bot.ingest(ev)
    assert row and row.status == "rejected" and "revoked" in row.reason


async def test_m3_the_session_reads_revocations_at_start_and_live(relay: FakeRelay, tmp_path: Path) -> None:
    p = Identity()
    await relay.inject(revoke(p, p.pubkey, 0))  # already on the relay when the follower starts
    now = time.time()
    bot = follower(tmp_path, p, relay_url=relay.url, clock_s=now)  # rule set preloaded
    bot.clock = time.time
    task = asyncio.create_task(run_briefly(bot, 1.5))
    await asyncio.wait_for(bot.ready.wait(), 5)
    assert len(bot.revocations) == 1
    await relay.inject(build_signal_event(p.device, signal(p, 1, None, at_ms=int(time.time() * 1000)), p.delegation))
    await task
    rows = bot.ledger.rows()
    assert len(rows) == 1 and rows[0].status == "rejected" and "revoked" in rows[0].reason


async def test_m3_a_revoked_provider_key_cannot_even_publish_the_rule_set_the_follower_loads(
    relay: FakeRelay, tmp_path: Path
) -> None:
    p = Identity()
    await relay.inject(ruleset_event(p.device, p.rules(), p.delegation))
    await relay.inject(revoke(p, p.pubkey, 0))
    bot = follower(tmp_path, p, rules=None, relay_url=relay.url, clock_s=time.time())
    with pytest.raises(FollowerError, match=r"rule set of provider .* is invalid: .*revoked"):
        await run_briefly(bot, 1.0)


async def test_m3_a_revocation_that_arrives_on_the_live_subscription_is_applied(
    relay: FakeRelay, tmp_path: Path
) -> None:
    p = Identity()
    now = time.time()
    bot = follower(tmp_path, p, relay_url=relay.url, clock_s=now)
    bot.clock = time.time
    task = asyncio.create_task(run_briefly(bot, 1.5))
    await asyncio.wait_for(bot.ready.wait(), 5)
    await relay.inject(revoke(p, p.pubkey, int(now) - 1))
    await asyncio.sleep(0.3)
    assert len(bot.revocations) == 1
    await task
