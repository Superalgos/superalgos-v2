"""Governance tally: SEP-3.34, SEP-3.36, SEP-3.37, SEP-3.35, SEP-3.13 plus delegation, determinism, relay collection."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core_fake_relay import FakeRelay
from sa_core.anchors import BscCommitAnchor, SimChain
from sa_core.chains import BalanceOracle, ChainRegistry, FixtureBackend, OracleError
from sa_core.delegation import (
    delegation_tag,
    revocation_event,
    sign_delegation,
    sign_revocation,
)
from sa_core.governance import (
    GovernanceError,
    RelayCoverage,
    TallyResult,
    anchored_map,
    build_anchor_attestation,
    build_gov_delegation_event,
    build_proposal_event,
    build_vote_event,
    collect,
    oracle_power,
    proof_from_attestation,
    sign_gov_delegation,
    tally,
    tally_from_relays,
    validate_gov_delegation_event,
    validate_proposal_event,
    validate_vote_event,
)
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import TOKEN_UNIT, NostrEvent, Proposal, Vote
from sa_core.nostr import build_event
from sa_core.power import (
    TimedAllocation,
    allocation_event,
    allocation_in_force,
    provider_power,
    sign_allocation,
)

DAY = 86_400
SNAP, DEADLINE = 1_000_000, 1_000_000 + 7 * DAY
D = 7 * DAY  # >= the voting period (SEP-3.35)
BLOCKS = {56: 100}
PROV = "c" * 64
PROPOSER = DeviceKey.generate()
PROPOSER_SA = SAKey.generate()
PROPOSER_DELEG = sign_delegation(PROPOSER_SA, PROPOSER.pubkey_hex, 10**10)
PROPOSER_TOKENS = 5_000  # whole SA tokens: above DEFAULT_MIN_PROPOSER_POWER


@dataclass
class Voter:
    tokens: int
    sa: SAKey = field(default_factory=SAKey.generate)
    device: DeviceKey = field(default_factory=DeviceKey.generate)

    def __post_init__(self) -> None:
        self.delegation = sign_delegation(self.sa, self.device.pubkey_hex, 10**10)

    @property
    def addr(self) -> str:
        return self.sa.address

    def vote(self, pid: str, option: int, nonce: int = 0, delegation: Any = None) -> NostrEvent:
        return build_vote_event(
            self.device,
            Vote(proposal=pid, option=option, nonce=nonce),
            delegation or self.delegation,
            created_at=SNAP + 5,
        )

    def alloc(self, nonce: int, gov: int, providers: dict[str, int] | None = None) -> NostrEvent:
        return allocation_event(
            self.device,
            sign_allocation(self.sa, nonce, gov, providers or {}),
            self.delegation,
            created_at=SNAP - 5,
        )

    def gov_delegate(self, nonce: int, to: dict[str, int]) -> NostrEvent:
        return build_gov_delegation_event(
            self.device, sign_gov_delegation(self.sa, nonce, to), created_at=SNAP - 5
        )


def make_proposal(
    relays: tuple[str, ...] = ("ws://a", "ws://b", "ws://c"), dust: int = 0
) -> NostrEvent:
    p = Proposal(
        title="Adopt v2",
        options=("yes", "no"),
        deadline=DEADLINE,
        snapshot_time=SNAP,
        snapshot_blocks=BLOCKS,
        relays=relays,
        dust_floor=dust,
    )
    return build_proposal_event(PROPOSER, p, PROPOSER_DELEG, created_at=SNAP - DAY)


def power_for(voters: list[Voter], extra: dict[str, int] | None = None) -> Any:
    backend = FixtureBackend()
    backend.set_balance(56, PROPOSER_SA.address, PROPOSER_TOKENS * TOKEN_UNIT)
    for v in voters:
        backend.set_balance(56, v.addr, v.tokens * TOKEN_UNIT)
    for addr, tokens in (extra or {}).items():
        backend.set_balance(56, addr, tokens * TOKEN_UNIT)
    return oracle_power(BalanceOracle(ChainRegistry.default(), backend))


FULL = RelayCoverage(
    official=("ws://a", "ws://b", "ws://c"), answered=("ws://a", "ws://b", "ws://c"), unreachable={}
)


def run(
    prop: NostrEvent,
    events: list[NostrEvent],
    voters: list[Voter],
    *,
    anchored: Mapping[str, int | None] | None = None,
    seen: int = SNAP + 10,
    coverage: RelayCoverage | None = FULL,
    **kw: Any,
) -> TallyResult:
    anchored = (
        anchored
        if anchored is not None
        else {e.id: SNAP - 10 if e.kind != 9331 else SNAP + 10 for e in events}
    )
    return tally(
        prop,
        events,
        anchored=anchored,
        power=power_for(voters, kw.pop("extra", None)),
        first_seen=kw.pop("first_seen", {e.id: seen for e in events}),
        collected_at=DEADLINE + 1,
        delay_d=kw.pop("delay_d", D),
        coverage=coverage,
        **kw,
    )


def totals(r: Any) -> list[int]:
    return [int(t) for t in r.totals]


def test_basic_tally_weights_are_balances_and_result_is_order_independent() -> None:
    prop = make_proposal()
    a, b, c = Voter(100), Voter(300), Voter(50)
    events = [a.vote(prop.id, 0), b.vote(prop.id, 1), c.vote(prop.id, 1)]
    r = run(prop, events, [a, b, c])
    assert totals(r) == [100 * TOKEN_UNIT, 350 * TOKEN_UNIT] and r.complete
    assert [v.address for v in r.voters] == sorted(v.addr for v in (a, b, c))
    outs = set()
    for seed in range(5):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        outs.add(run(prop, shuffled, [a, b, c]).canonical_json())
    assert len(outs) == 1
    assert json.loads(next(iter(outs)))["title"] == "Adopt v2"


def test_highest_nonce_wins_among_eligible_votes_and_late_revotes_are_ignored() -> None:
    prop = make_proposal()
    a = Voter(100)
    v0, v1, v2 = a.vote(prop.id, 0, 0), a.vote(prop.id, 1, 1), a.vote(prop.id, 0, 2)
    anchored: dict[str, int | None] = {
        v0.id: SNAP + 1,
        v1.id: SNAP + 2,
        v2.id: DEADLINE + 1,
    }  # v2 anchored too late
    r = run(prop, [v0, v1, v2], [a], anchored=anchored)
    assert totals(r) == [0, 100 * TOKEN_UNIT]
    assert r.rejected == {"anchored-after-deadline": 1}


# ---------------------------------------------------------------- SEP-3.37 / SEP-3.13


@given(late=st.lists(st.integers(1, 10**7), min_size=1, max_size=4), pick=st.integers(0, 1))
def test_sep_3_37_votes_and_allocations_anchored_after_the_deadline_never_change_the_result(
    late: list[int], pick: int
) -> None:
    prop = make_proposal()
    a, b = Voter(100), Voter(300)
    base_events = [a.vote(prop.id, 0), b.vote(prop.id, 1)]
    base = run(prop, base_events, [a, b])
    c = Voter(10_000)
    extra: list[NostrEvent] = []
    anchored = {e.id: SNAP + 10 for e in base_events}
    for k, delay in enumerate(late):
        ev = (
            c.vote(prop.id, pick, nonce=k)
            if k % 2 == 0
            else a.alloc(nonce=k + 1, gov=0, providers={PROV: 10_000})
        )
        ev = ev.model_copy(update={"created_at": SNAP})  # backdated created_at must not matter
        extra.append(ev)
        anchored[ev.id] = DEADLINE + delay
    after = run(prop, [*base_events, *extra], [a, b, c], anchored=anchored)
    assert after.totals == base.totals and after.voters == base.voters


def test_sep_3_37_a_backdated_created_at_without_a_proof_is_unanchored() -> None:
    prop = make_proposal()
    a = Voter(100)
    ev = a.vote(prop.id, 0)  # created_at = SNAP + 5, before the deadline, but no proof
    r = run(prop, [ev], [a], anchored={})
    assert totals(r) == [0, 0] and r.rejected == {"unanchored": 1}


def test_sep_3_13_a_vote_first_readable_after_the_deadline_is_excluded_despite_an_early_anchor() -> (
    None
):
    prop = make_proposal()
    a, b = Voter(100), Voter(300)
    va, vb = a.vote(prop.id, 0), b.vote(prop.id, 1)
    anchored = {va.id: SNAP + 1, vb.id: SNAP + 1}  # both anchored well before the deadline
    seen = {va.id: SNAP + 100, vb.id: DEADLINE + 1}  # b's vote only became readable after it
    r = run(prop, [va, vb], [a, b], anchored=anchored, first_seen=seen)
    assert totals(r) == [100 * TOKEN_UNIT, 0]
    assert r.rejected == {"first-seen-after-deadline": 1}
    # at the deadline exactly is still in time
    seen[vb.id] = DEADLINE
    assert totals(run(prop, [va, vb], [a, b], anchored=anchored, first_seen=seen)) == [
        100 * TOKEN_UNIT,
        300 * TOKEN_UNIT,
    ]


def test_sep_3_13_a_vote_nobody_observed_is_not_trusted_when_the_tally_runs_late() -> None:
    prop = make_proposal()
    a = Voter(100)
    va = a.vote(prop.id, 0)
    r = tally(
        prop,
        [va],
        anchored={va.id: SNAP + 1},
        power=power_for([a]),
        collected_at=DEADLINE + 5,
        delay_d=D,
        coverage=FULL,
    )
    assert totals(r) == [0, 0] and r.rejected == {"first-seen-after-deadline": 1}
    early = tally(
        prop,
        [va],
        anchored={va.id: SNAP + 1},
        power=power_for([a]),
        collected_at=DEADLINE - 5,
        delay_d=D,
        coverage=FULL,
    )
    assert totals(early) == [100 * TOKEN_UNIT, 0]


# ---------------------------------------------------------------- SEP-3.34 / SEP-3.35


def test_sep_3_34_power_allocated_to_a_provider_does_not_vote() -> None:
    prop = make_proposal()
    a = Voter(1000)
    alloc = a.alloc(1, gov=3000, providers={PROV: 7000})
    v = a.vote(prop.id, 0)
    r = run(prop, [v, alloc], [a], anchored={alloc.id: SNAP - D - 1, v.id: SNAP + 1})
    assert totals(r) == [300 * TOKEN_UNIT, 0]  # 30% governance; 70% serves the provider's tier


def test_sep_3_35_an_allocation_in_force_at_the_snapshot_fixes_the_weight() -> None:
    prop = make_proposal()
    a = Voter(1000)
    v = a.vote(prop.id, 0)
    old = a.alloc(1, gov=10_000)
    swap = a.alloc(
        2, gov=0, providers={PROV: 10_000}
    )  # moves everything to a provider, published after the snapshot
    anchored = {v.id: SNAP + 1, old.id: SNAP - D - 1, swap.id: SNAP + 100}
    r = run(prop, [v, old, swap], [a], anchored=anchored)
    assert totals(r) == [1000 * TOKEN_UNIT, 0]  # the vote still counts the old allocation


def test_sep_3_35_an_allocation_published_before_the_snapshot_but_younger_than_d_is_not_yet_in_force() -> (
    None
):
    prop = make_proposal()
    a = Voter(1000)
    v = a.vote(prop.id, 0)
    old = a.alloc(1, gov=10_000)
    recent = a.alloc(2, gov=2000, providers={PROV: 8000})
    anchored = {
        v.id: SNAP + 1,
        old.id: SNAP - 2 * D,
        recent.id: SNAP - D + 1,
    }  # effective one second after the snapshot
    r = run(prop, [v, old, recent], [a], anchored=anchored)
    assert totals(r) == [1000 * TOKEN_UNIT, 0]
    anchored[recent.id] = SNAP - D  # effective exactly at the snapshot
    assert totals(run(prop, [v, old, recent], [a], anchored=anchored)) == [200 * TOKEN_UNIT, 0]


@given(published=st.integers(1, 7 * 86_400), probe_offset=st.integers(0, 14 * 86_400))
def test_sep_3_35_reallocating_during_an_open_vote_changes_neither_the_vote_nor_a_tier_before_d(
    published: int, probe_offset: int
) -> None:
    """governance -> provider during the vote: weight unchanged, provider tier unchanged until D elapses."""
    a = Voter(1000)
    old = sign_allocation(a.sa, 1, 10_000)
    new = sign_allocation(a.sa, 2, 0, {PROV: 10_000})
    anchored_new = SNAP + published  # inside the voting window (snapshot .. deadline]
    hist = [TimedAllocation(old, "old", 0), TimedAllocation(new, "new", anchored_new)]
    total = 1000 * TOKEN_UNIT
    # vote side: snapshot allocation is the old one
    assert allocation_in_force(hist, SNAP, D) == old
    # tier side: any time up to and including the deadline still sees the old split
    probe = SNAP + probe_offset
    in_force = allocation_in_force(hist, probe, D)
    if probe < anchored_new + D:
        assert in_force == old and provider_power(in_force, PROV, total) == 0
    else:
        assert in_force == new and provider_power(in_force, PROV, total) == total
    if probe <= DEADLINE:
        assert (
            provider_power(in_force, PROV, total) == 0
        )  # D >= voting period: no double counting while open


# ---------------------------------------------------------------- delegation of vote power


def test_delegated_weight_flows_to_a_voting_delegate_and_a_direct_vote_overrides() -> None:
    prop = make_proposal()
    delegate, delegator, other = Voter(100), Voter(400), Voter(400)
    gd = delegator.gov_delegate(1, {delegate.addr: 5000})
    gd_other = other.gov_delegate(1, {delegate.addr: 10_000})
    vd, vo = (
        delegate.vote(prop.id, 0),
        other.vote(prop.id, 1),
    )  # `other` votes directly: delegation ignored
    anchored = {gd.id: SNAP - 1, gd_other.id: SNAP - 1, vd.id: SNAP + 1, vo.id: SNAP + 1}
    r = run(prop, [gd, gd_other, vd, vo], [delegate, delegator, other], anchored=anchored)
    assert totals(r) == [(100 + 200) * TOKEN_UNIT, 400 * TOKEN_UNIT]
    line = next(line for line in r.voters if line.address == delegate.addr)
    assert line.own == str(100 * TOKEN_UNIT) and line.delegated_in == str(200 * TOKEN_UNIT)
    assert r.unused_delegated == "0"


def test_delegation_to_a_non_voter_is_unused_and_late_or_unanchored_delegations_are_ignored() -> (
    None
):
    prop = make_proposal()
    delegate, delegator = Voter(100), Voter(400)
    gd = delegator.gov_delegate(1, {delegate.addr: 5000})
    r = run(prop, [gd], [delegate, delegator], anchored={gd.id: SNAP - 1})
    assert r.unused_delegated == str(200 * TOKEN_UNIT)
    vd = delegate.vote(prop.id, 0)
    late = run(prop, [gd, vd], [delegate, delegator], anchored={gd.id: SNAP + 1, vd.id: SNAP + 1})
    assert totals(late) == [100 * TOKEN_UNIT, 0] and late.rejected == {
        "delegation-after-snapshot": 1
    }
    none = run(prop, [gd, vd], [delegate, delegator], anchored={vd.id: SNAP + 1})
    assert none.rejected == {"delegation-unanchored": 1}


# ---------------------------------------------------------------- rejection reasons, completeness


def test_invalid_votes_and_delegations_are_counted_by_reason() -> None:
    prop = make_proposal()
    a = Voter(100)
    other_prop = make_proposal(relays=("ws://z",))
    cases = {
        "bad sig": a.vote(prop.id, 0).model_copy(update={"content": '{"v":1}'}),
        "wrong proposal": a.vote(other_prop.id, 0),
        "bad option": a.vote(prop.id, 5),
        "no tag": build_event(
            a.device, 9331, Vote(proposal=prop.id, option=0, nonce=0).model_dump_json(), []
        ),
    }
    events = list(cases.values())
    r = run(prop, events, [a], anchored={e.id: SNAP + 1 for e in events})
    assert r.rejected == {"invalid-vote": 4} and totals(r) == [0, 0]
    expired = Voter(100)
    short = sign_delegation(expired.sa, expired.device.pubkey_hex, SNAP + 3)
    ev = expired.vote(prop.id, 0, delegation=short)
    assert run(prop, [ev], [expired], anchored={ev.id: SNAP + 3}).rejected == {
        "delegation-invalid-at-anchor": 1
    }
    bad_alloc = build_event(a.device, 9332, "{}", [])
    bad_gd = build_event(a.device, 9333, "{}", [])
    assert run(prop, [bad_alloc, bad_gd], [a]).rejected == {
        "invalid-allocation": 1,
        "invalid-delegation": 1,
    }
    ignored = build_event(a.device, 1, "hello")
    assert run(prop, [ignored], [a]).rejected == {}


def test_dust_floor_and_unreadable_balances() -> None:
    prop = make_proposal(dust=50)
    small, big = Voter(10), Voter(500)
    r = run(prop, [small.vote(prop.id, 0), big.vote(prop.id, 1)], [small, big])
    assert totals(r) == [0, 500 * TOKEN_UNIT] and r.rejected == {"below-dust-floor": 1}

    def broken(addr: str, blocks: Any) -> int:
        if addr == big.addr:
            raise OracleError("rpc down")
        return 10**24

    prop2 = make_proposal()
    vs = [small.vote(prop2.id, 0), big.vote(prop2.id, 1)]
    inc = tally(
        prop2,
        vs,
        anchored={e.id: SNAP + 1 for e in vs},
        power=broken,
        first_seen={e.id: SNAP + 2 for e in vs},
        delay_d=D,
        coverage=FULL,
    )
    assert not inc.complete and inc.missing_power == (big.addr,)
    assert totals(inc) == [
        10**24,
        0,
    ]  # the unreadable balance is "unknown", never counted as zero-and-complete


# ---------------------------------------------------------------- SEP-3.36


def test_sep_3_36_result_lists_answering_relays_and_is_incomplete_when_one_is_down() -> None:
    prop = make_proposal()
    a = Voter(100)
    v = a.vote(prop.id, 0)
    down = RelayCoverage(
        official=("ws://a", "ws://b", "ws://c"),
        answered=("ws://b", "ws://a"),
        unreachable={"ws://c": "connection refused"},
    )
    r = run(prop, [v], [a], anchored={v.id: SNAP + 1}, coverage=down)
    assert (
        not r.complete
        and r.relays_answered == ("ws://a", "ws://b")
        and r.relays_unreachable == {"ws://c": "connection refused"}
    )
    assert run(prop, [v], [a], anchored={v.id: SNAP + 1}).complete
    assert not run(prop, [v], [a], anchored={v.id: SNAP + 1}, coverage=None).complete
    assert not down.complete and FULL.complete


async def test_sep_3_36_end_to_end_with_one_of_three_relays_stopped() -> None:
    relays = [FakeRelay() for _ in range(3)]
    urls = [await r.start() for r in relays]
    try:
        prop = make_proposal(relays=tuple(urls))
        clock = [float(SNAP)]
        chain = SimChain(clock=lambda: clock[0], finality_depth=1)
        bsc = BscCommitAnchor(chain, chain, min_depth=2)
        a, b = Voter(100), Voter(300)
        va, vb = a.vote(prop.id, 0), b.vote(prop.id, 1)
        proofs = bsc.stamp_batch([bytes.fromhex(va.id), bytes.fromhex(vb.id)])
        chain.mine(SNAP + 2)
        chain.mine_many(3, start=SNAP + 3)
        att = [build_anchor_attestation(PROPOSER, p) for p in proofs]
        for i, r in enumerate(relays):
            for e in (prop, va, vb, *att):
                if i != 0 or e is not vb:  # relay 0 never got b's vote
                    r.events[e.id] = e
        power = power_for([a, b])
        res = await tally_from_relays(
            prop.id,
            urls,
            power=power,
            verifiers={"bsc-commit": bsc},
            delay_d=D,
            relay_timeout=1.0,
            clock=lambda: SNAP + 10,
        )
        assert totals(res) == [100 * TOKEN_UNIT, 300 * TOKEN_UNIT] and res.complete
        assert set(res.relays_answered) == set(urls)
        await relays[2].stop()
        res2 = await tally_from_relays(
            prop.id,
            urls,
            power=power,
            verifiers={"bsc-commit": bsc},
            delay_d=D,
            relay_timeout=1.0,
            clock=lambda: SNAP + 10,
        )
        assert (
            not res2.complete
            and urls[2] in res2.relays_unreachable
            and set(res2.relays_answered) == set(urls[:2])
        )
        assert totals(res2) == totals(res)  # same votes still reachable on the remaining relays
        with pytest.raises(GovernanceError, match="not found on any of 1 relays"):
            await tally_from_relays(
                "d" * 64, [urls[2]], power=power, verifiers={}, relay_timeout=0.5
            )
    finally:
        for r in relays[:2]:
            await r.stop()


async def test_collect_records_first_seen_and_keeps_earlier_observations() -> None:
    relay = FakeRelay()
    url = await relay.start()
    try:
        ev = build_event(PROPOSER, 1, "x")
        relay.events[ev.id] = ev
        got = await collect(
            [url, "ws://127.0.0.1:9"],
            [{"kinds": [1]}],
            relay_timeout=0.5,
            clock=lambda: 500.0,
            prior_first_seen={ev.id: 100},
        )
        assert got.first_seen[ev.id] == 100  # the earlier observation is kept
        assert got.coverage.answered == (url,) and "ws://127.0.0.1:9" in got.coverage.unreachable
        bad = ev.model_copy(update={"content": "tampered", "id": "f" * 64})
        relay.events[bad.id] = bad
        again = await collect([url], [{"kinds": [1]}], relay_timeout=0.5)
        assert bad.id not in again.events and ev.id in again.events
    finally:
        await relay.stop()


# ---------------------------------------------------------------- event helpers


def test_event_validation_helpers() -> None:
    prop = make_proposal()
    power = power_for([])
    assert validate_proposal_event(prop, power).title == "Adopt v2"
    with pytest.raises(GovernanceError, match="proposal content invalid"):
        validate_proposal_event(build_event(PROPOSER, 9330, "{}"), power)
    with pytest.raises(GovernanceError, match="expected 9330"):
        validate_proposal_event(build_event(PROPOSER, 1, "{}"), power)
    a = Voter(1)
    vote, deleg = validate_vote_event(a.vote(prop.id, 1), prop.id, 2)
    assert vote.option == 1 and deleg.evm_address == a.addr
    bad_tag = ("sa-delegation", a.addr, "10", "0x" + "00" * 65)
    ev = build_event(
        a.device, 9331, Vote(proposal=prop.id, option=0, nonce=0).model_dump_json(), [bad_tag]
    )
    with pytest.raises(GovernanceError, match="delegation invalid"):
        validate_vote_event(ev, prop.id, 2)
    gd = a.gov_delegate(1, {"0x" + "2" * 40: 100})
    assert validate_gov_delegation_event(gd).nonce == 1
    forged = build_event(
        a.device,
        9333,
        sign_gov_delegation(SAKey.generate(), 1, {"0x" + "2" * 40: 100})
        .model_copy(update={"address": a.addr})
        .model_dump_json(),
    )
    with pytest.raises(GovernanceError, match="not signed by"):
        validate_gov_delegation_event(forged)
    with pytest.raises(GovernanceError, match="delegation content invalid"):
        validate_gov_delegation_event(build_event(a.device, 9333, "{}"))


def test_anchor_attestation_round_trip_and_map() -> None:
    chain = SimChain()
    bsc = BscCommitAnchor(chain, chain, min_depth=2)
    ev = build_event(PROPOSER, 1, "x")
    (proof,) = bsc.stamp_batch([bytes.fromhex(ev.id)])
    att = build_anchor_attestation(PROPOSER, proof)
    assert proof_from_attestation(att) == proof
    chain.mine(100)
    chain.mine_many(3, start=101)
    assert anchored_map([ev], [proof], {"bsc-commit": bsc})[ev.id] is not None
    assert anchored_map([ev], [], {"bsc-commit": bsc}) == {ev.id: None}
    with pytest.raises(GovernanceError, match="needs one 'e' tag"):
        proof_from_attestation(build_event(PROPOSER, 1040, "x", []))
    with pytest.raises(GovernanceError, match="invalid"):
        proof_from_attestation(
            build_event(PROPOSER, 1040, "x", [("e", "zz"), ("sa-anchor", "ots")])
        )


# ---------------------------------------------------------------- security review M2 / H1 / M3


def _proposal_by(device: DeviceKey, delegation: Any | None, *, at: int | None = None) -> NostrEvent:
    p = Proposal(
        title="Adopt v2",
        options=("yes", "no"),
        deadline=DEADLINE,
        snapshot_time=SNAP,
        snapshot_blocks=BLOCKS,
        relays=("ws://a", "ws://b", "ws://c"),
    )
    tags = [] if delegation is None else [delegation_tag(delegation)]
    return build_event(device, 9330, p.model_dump_json(), tags, SNAP - DAY if at is None else at)


def test_m2_a_proposal_without_a_delegation_tag_is_rejected_everywhere() -> None:
    ev = _proposal_by(PROPOSER, None)
    with pytest.raises(GovernanceError, match="no valid sa-delegation from a proposer"):
        validate_proposal_event(ev, power_for([]))
    with pytest.raises(GovernanceError, match="no valid sa-delegation from a proposer"):
        run(ev, [], [])


def test_m2_a_proposal_from_an_address_below_min_proposer_power_is_rejected() -> None:
    poor_sa, poor_dev = SAKey.generate(), DeviceKey.generate()
    deleg = sign_delegation(poor_sa, poor_dev.pubkey_hex, 10**10)
    ev = _proposal_by(poor_dev, deleg)
    power = power_for([], {poor_sa.address: 999})
    with pytest.raises(GovernanceError, match="holds 999 SA, below the 1000 SA needed to propose"):
        validate_proposal_event(ev, power)
    assert validate_proposal_event(ev, power, min_proposer_power=999).title == "Adopt v2"
    assert validate_proposal_event(ev, power_for([], {poor_sa.address: 1000})).title == "Adopt v2"
    with pytest.raises(GovernanceError, match="below the 1000 SA"):
        run(ev, [], [], extra={poor_sa.address: 999})  # tally() applies the same rule
    assert run(ev, [], [], extra={poor_sa.address: 1000}).title == "Adopt v2"


def test_m2_proposer_delegation_must_be_valid_at_publication_and_not_revoked() -> None:
    sa, dev = SAKey.generate(), DeviceKey.generate()
    short = sign_delegation(sa, dev.pubkey_hex, SNAP - DAY + 5)
    power = power_for([], {sa.address: 5000})
    assert validate_proposal_event(_proposal_by(dev, short), power).title == "Adopt v2"
    with pytest.raises(GovernanceError, match="expired"):
        validate_proposal_event(_proposal_by(dev, short, at=SNAP - DAY + 5), power)
    forged = sign_delegation(
        SAKey.generate(), dev.pubkey_hex, 10**10
    )  # not signed by who it names? it is, but powerless
    with pytest.raises(GovernanceError, match="below the 1000 SA"):
        validate_proposal_event(_proposal_by(dev, forged), power)
    rev = sign_revocation(sa, dev.pubkey_hex, SNAP - DAY)
    with pytest.raises(GovernanceError, match="revoked"):
        validate_proposal_event(_proposal_by(dev, short), power, revocations=[rev])


def test_m2_an_unreadable_proposer_balance_is_an_error_not_a_pass() -> None:
    def broken(_a: str, _b: Any) -> int:
        raise OracleError("rpc down")

    with pytest.raises(GovernanceError, match="cannot read the power of proposer"):
        validate_proposal_event(make_proposal(), broken)


def test_h1_a_re_wrapped_allocation_does_not_count_in_a_tally() -> None:
    prop = make_proposal()
    a = Voter(100)
    alloc = sign_allocation(
        a.sa, 1, 0, {PROV: 10_000}
    )  # all power to a provider: governance weight 0
    stranger = DeviceKey.generate()
    rewrap = build_event(stranger, 9332, alloc.model_dump_json(), [("p", PROV)], SNAP - 5)
    r = run(prop, [a.vote(prop.id, 0), rewrap], [a])
    assert r.rejected == {"invalid-allocation": 1}
    assert totals(r) == [
        100 * TOKEN_UNIT,
        0,
    ]  # no allocation in force: the default is all-governance


def test_m3_an_allocation_from_a_revoked_device_key_is_ignored_in_a_tally() -> None:
    prop = make_proposal()
    a = Voter(100)
    dev2 = DeviceKey.generate()  # a second device key of the same address publishes the allocation
    deleg2 = sign_delegation(a.sa, dev2.pubkey_hex, 10**10)
    alloc = allocation_event(
        dev2, sign_allocation(a.sa, 1, 0, {PROV: 10_000}), deleg2, created_at=SNAP - 5
    )
    vote = a.vote(prop.id, 0)

    def go(*extra: NostrEvent) -> TallyResult:
        evs = [vote, alloc, *extra]
        anchored = {
            e.id: (SNAP + 10 if e is vote else SNAP - D - 10) for e in evs
        }  # the allocation is in force
        return run(prop, evs, [a], anchored=anchored)

    assert totals(go()) == [0, 0]  # all power allocated away from governance
    revoke = revocation_event(
        DeviceKey.generate(), sign_revocation(a.sa, dev2.pubkey_hex, SNAP - 6)
    )
    r = go(revoke)
    assert r.rejected == {"invalid-allocation": 1}
    assert totals(r) == [
        100 * TOKEN_UNIT,
        0,
    ]  # no valid allocation: the whole balance defaults to governance
    late = revocation_event(DeviceKey.generate(), sign_revocation(a.sa, dev2.pubkey_hex, SNAP - 4))
    assert totals(go(late)) == [0, 0]  # revoked after it spoke: it stands


def test_m3_a_vote_from_a_revoked_device_key_does_not_count() -> None:
    prop = make_proposal()
    a, b = Voter(100), Voter(300)
    revoke = revocation_event(
        DeviceKey.generate(), sign_revocation(a.sa, a.device.pubkey_hex, SNAP + 1)
    )  # votes are anchored at SNAP + 10
    r = run(prop, [a.vote(prop.id, 0), b.vote(prop.id, 1), revoke], [a, b])
    assert totals(r) == [0, 300 * TOKEN_UNIT]
    assert r.rejected == {"delegation-invalid-at-anchor": 1}
    # a revocation from some other address changes nothing
    other = revocation_event(
        DeviceKey.generate(), sign_revocation(SAKey.generate(), a.device.pubkey_hex, 0)
    )
    assert totals(run(prop, [a.vote(prop.id, 0), other], [a])) == [100 * TOKEN_UNIT, 0]
