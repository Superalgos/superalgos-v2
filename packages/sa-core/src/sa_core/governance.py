"""Governance: proposals, votes, vote-power delegations and the deterministic tally.

Rules (SEP criteria in brackets):
- A vote counts only if its anchored time is <= the proposal deadline [SEP-3.37] AND it was first
  readable on an official relay by the deadline [SEP-3.13]: an anchor proves a vote existed, not
  that anyone could read it, so a vote revealed late is excluded even with an early anchor.
- Weight = governance share (from the allocation in force at the proposal's snapshot, effective
  D after its anchored time) x power at the snapshot blocks [SEP-3.35]. Power allocated to providers
  never counts [SEP-3.34].
- Delegation (kind 9333): a non-voting delegator hands bps of its governance weight to a delegate
  that voted. One hop. A direct vote overrides the delegation.
- The result lists which official relays answered and is marked incomplete when any is
  unreachable or any balance could not be read [SEP-3.36].
- All arithmetic is integer, and output is canonical JSON: the same events in any order give the
  same bytes.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from . import _evm
from ._canon import canonical_json
from .anchors import Anchor, anchored_time
from .chains import BalanceOracle, OracleError
from .delegation import (
    DelegationError,
    delegation_from_event,
    delegation_tag,
    revocations_from_events,
    verify_delegation,
    verify_event_delegation,
)
from .keys import DeviceKey, SAKey
from .models import (
    ALLOCATION_KIND,
    ANCHOR_KIND,
    BPS,
    GOV_DELEGATION_KIND,
    PROPOSAL_KIND,
    REVOCATION_KIND,
    TOKEN_UNIT,
    VOTE_KIND,
    AnchorProof,
    Delegation,
    GovDelegation,
    NostrEvent,
    Proposal,
    Revocation,
    Vote,
)
from .nostr import NostrError, RelayClient, build_event, verify_event
from .power import (
    DEFAULT_DELAY_D_S,
    PowerError,
    TimedAllocation,
    allocation_in_force,
    governance_power,
    validate_allocation_event,
)


class GovernanceError(Exception):
    """An event is not a valid proposal, vote or delegation. The message names the value."""


# ---------------------------------------------------------------- events


def build_proposal_event(
    key: DeviceKey, proposal: Proposal, delegation: Delegation, created_at: int | None = None
) -> NostrEvent:
    """Kind 9330 from a device key; the `sa-delegation` tag names the proposer's SA address (spam bound)."""
    return build_event(
        key, PROPOSAL_KIND, proposal.model_dump_json(), [delegation_tag(delegation)], created_at
    )


def build_vote_event(
    key: DeviceKey, vote: Vote, delegation: Delegation, created_at: int | None = None
) -> NostrEvent:
    return build_event(
        key,
        VOTE_KIND,
        vote.model_dump_json(),
        [("e", vote.proposal), delegation_tag(delegation)],
        created_at,
    )


def _checked(event: NostrEvent, kind: int, label: str) -> None:
    try:
        verify_event(event)
    except NostrError as exc:
        raise GovernanceError(str(exc)) from exc
    if event.kind != kind:
        raise GovernanceError(
            f"event {event.id[:12]}.. has kind {event.kind}, expected {kind} ({label})"
        )


DEFAULT_MIN_PROPOSER_POWER = (
    1_000  # whole SA tokens an address needs at the snapshot to put a proposal up
)


def validate_proposal_event(
    event: NostrEvent,
    power: PowerFn,
    min_proposer_power: int = DEFAULT_MIN_PROPOSER_POWER,
    revocations: Iterable[Revocation] = (),
) -> Proposal:
    """Signature, schema, and a proposer: a valid `sa-delegation` from an address holding at least
    `min_proposer_power` whole tokens at the proposal's `snapshot_blocks` (judged at the event's `created_at`).
    """
    _checked(event, PROPOSAL_KIND, "proposal")
    try:
        prop = Proposal.model_validate_json(event.content)
    except ValidationError as exc:
        raise GovernanceError(f"proposal content invalid: {exc.errors()[0]['msg']}") from exc
    try:
        proposer = verify_event_delegation(event, event.created_at, None, revocations)
    except DelegationError as exc:
        raise GovernanceError(
            f"proposal {event.id[:12]}.. has no valid sa-delegation from a proposer: {exc}; "
            "publish it from a device key delegated by an SA address"
        ) from exc
    try:
        held = power(proposer, prop.snapshot_blocks)
    except OracleError as exc:
        raise GovernanceError(
            f"cannot read the power of proposer {proposer} for proposal {event.id[:12]}..: {exc}"
        ) from exc
    if held < min_proposer_power * TOKEN_UNIT:
        raise GovernanceError(
            f"proposer {proposer} holds {held / TOKEN_UNIT:g} SA, below the {min_proposer_power} SA "
            f"needed to propose (proposal {event.id[:12]}..)"
        )
    return prop


def validate_vote_event(
    event: NostrEvent, proposal_id: str, n_options: int
) -> tuple[Vote, Delegation]:
    """Signature, schema, proposal and option; the delegation's signature (expiry is judged later)."""
    _checked(event, VOTE_KIND, "vote")
    try:
        vote = Vote.model_validate_json(event.content)
    except ValidationError as exc:
        raise GovernanceError(f"vote content invalid: {exc.errors()[0]['msg']}") from exc
    if vote.proposal != proposal_id:
        raise GovernanceError(
            f"vote is for proposal {vote.proposal[:12]}.., not {proposal_id[:12]}.."
        )
    if vote.option >= n_options:
        raise GovernanceError(f"option {vote.option} but the proposal has {n_options} options")
    try:
        deleg = delegation_from_event(event)
        verify_delegation(deleg, event.pubkey, 0)
    except DelegationError as exc:
        raise GovernanceError(f"vote delegation invalid: {exc}") from exc
    return vote, deleg


def gov_delegation_message(address: str, nonce: int, to: Mapping[str, int]) -> str:
    body = ",".join(f"{a}={to[a]}" for a in sorted(to))
    return f"sa-govdelegation:{address.lower()}:{nonce}:{body}"


def sign_gov_delegation(sa: SAKey, nonce: int, to: Mapping[str, int]) -> GovDelegation:
    norm = {a.lower(): v for a, v in to.items()}
    return GovDelegation(
        address=sa.address,
        nonce=nonce,
        to=norm,
        sig=sa.sign_message(gov_delegation_message(sa.address, nonce, norm)),
    )


def build_gov_delegation_event(
    key: DeviceKey, d: GovDelegation, created_at: int | None = None
) -> NostrEvent:
    return build_event(key, GOV_DELEGATION_KIND, d.model_dump_json(), [], created_at)


def validate_gov_delegation_event(event: NostrEvent) -> GovDelegation:
    _checked(event, GOV_DELEGATION_KIND, "governance delegation")
    try:
        d = GovDelegation.model_validate_json(event.content)
    except ValidationError as exc:
        raise GovernanceError(f"delegation content invalid: {exc.errors()[0]['msg']}") from exc
    try:
        _evm.require_signer(gov_delegation_message(d.address, d.nonce, d.to), d.sig, d.address)
    except _evm.SignatureError as exc:
        raise GovernanceError(f"delegation not signed by {d.address}: {exc}") from exc
    return d


def build_anchor_attestation(
    key: DeviceKey, proof: AnchorProof, created_at: int | None = None
) -> NostrEvent:
    """Kind 1040 (NIP-03 style): publishes `proof` for the event whose id is `proof.digest`."""
    return build_event(
        key, ANCHOR_KIND, proof.data, [("e", proof.digest), ("sa-anchor", proof.kind)], created_at
    )


def proof_from_attestation(event: NostrEvent) -> AnchorProof:
    _checked(event, ANCHOR_KIND, "anchor attestation")
    e_tags, kinds = event.tag_values("e"), event.tag_values("sa-anchor")
    if len(e_tags) != 1 or len(kinds) != 1 or len(e_tags[0]) < 2 or len(kinds[0]) < 2:
        raise GovernanceError(
            f"attestation {event.id[:12]}.. needs one 'e' tag and one 'sa-anchor' tag"
        )
    try:
        return AnchorProof(kind=kinds[0][1], digest=e_tags[0][1], data=event.content)
    except ValidationError as exc:
        raise GovernanceError(
            f"attestation {event.id[:12]}.. invalid: {exc.errors()[0]['msg']}"
        ) from exc


def anchored_map(
    events: Iterable[NostrEvent],
    proofs: Iterable[AnchorProof],
    verifiers: Mapping[str, Anchor],
) -> dict[str, int | None]:
    """event id -> anchored time (None when no proof verifies)."""
    plist = list(proofs)
    return {e.id: anchored_time(bytes.fromhex(e.id), plist, verifiers) for e in events}


# ---------------------------------------------------------------- relay coverage (SEP-3.36)


class RelayCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    official: tuple[str, ...]
    answered: tuple[str, ...]
    unreachable: dict[str, str]

    @property
    def complete(self) -> bool:
        return set(self.answered) >= set(self.official) and not self.unreachable


@dataclass
class Collected:
    events: dict[str, NostrEvent] = field(default_factory=dict)
    first_seen: dict[str, int] = field(default_factory=dict)
    coverage: RelayCoverage = field(
        default_factory=lambda: RelayCoverage(official=(), answered=(), unreachable={})
    )


async def collect(
    relays: Sequence[str],
    filters: Sequence[dict[str, Any]],
    *,
    relay_timeout: float = 5.0,
    clock: Callable[[], float] = time.time,
    prior_first_seen: Mapping[str, int] | None = None,
) -> Collected:
    """Query every official relay; report which answered. Invalid events are dropped.

    `first_seen[id]` is the earliest of `prior_first_seen` (from earlier polls) and now: a poller
    that runs until the deadline gives SEP-3.13 its evidence.
    """

    async def one(url: str) -> list[NostrEvent]:
        async with RelayClient(url, connect_timeout=relay_timeout) as relay:
            return await relay.query(filters, timeout=relay_timeout)

    results = await asyncio.gather(*(one(u) for u in relays), return_exceptions=True)
    now = int(clock())
    out = Collected()
    answered: list[str] = []
    unreachable: dict[str, str] = {}
    out.first_seen.update(prior_first_seen or {})
    for url, res in zip(relays, results, strict=True):
        if isinstance(res, BaseException):
            if not isinstance(res, NostrError | OSError | TimeoutError):
                raise res
            unreachable[url] = str(res) or type(res).__name__
            continue
        answered.append(url)
        for ev in res:
            try:
                verify_event(ev)
            except NostrError:
                continue
            out.events[ev.id] = ev
            out.first_seen.setdefault(ev.id, now)
    out.coverage = RelayCoverage(
        official=tuple(relays), answered=tuple(answered), unreachable=unreachable
    )
    return out


# ---------------------------------------------------------------- tally


class VoterLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    address: str
    option: int
    own: str  # 18-decimal units as decimal string
    delegated_in: str
    weight: str


class TallyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str
    title: str
    options: tuple[str, ...]
    totals: tuple[str, ...]
    voters: tuple[VoterLine, ...]
    rejected: dict[str, int]
    unused_delegated: str
    complete: bool
    relays_answered: tuple[str, ...]
    relays_unreachable: dict[str, str]
    missing_power: tuple[str, ...]
    snapshot_time: int
    deadline: int
    delay_d: int

    def canonical_json(self) -> str:
        return canonical_json(self.model_dump(mode="json"))


PowerFn = Callable[[str, Mapping[int, int]], int]


def oracle_power(oracle: BalanceOracle) -> PowerFn:
    """Adapt a `BalanceOracle` to the tally's power function (18-decimal units)."""
    return lambda address, blocks: oracle.power(address, blocks).total


@dataclass(frozen=True)
class _Version:
    nonce: int
    event_id: str
    value: Any


def _best(versions: Iterable[_Version]) -> _Version | None:
    return min(versions, key=lambda v: (-v.nonce, v.event_id), default=None)


def tally(
    proposal_event: NostrEvent,
    events: Iterable[NostrEvent],
    *,
    anchored: Mapping[str, int | None],
    power: PowerFn,
    first_seen: Mapping[str, int] | None = None,
    collected_at: int | None = None,
    delay_d: int = DEFAULT_DELAY_D_S,
    coverage: RelayCoverage | None = None,
    default_governance_bps: int = BPS,
    revocations: Iterable[Revocation] = (),
    min_proposer_power: int = DEFAULT_MIN_PROPOSER_POWER,
) -> TallyResult:
    """Count a proposal. `events` may mix votes, allocations, delegations; others are ignored.

    `anchored`: event id -> anchored time (missing or None = unanchored, never counts).
    `first_seen`: event id -> first time readable on an official relay; an event with no entry is
    taken as first seen at `collected_at` (default: now), which is after the deadline for any
    tally run late, so unobserved votes are excluded rather than trusted.
    """
    event_list = list(events)
    revs = [
        *revocations,
        *revocations_from_events(event_list),
    ]  # kind 9334 events among `events` count too
    prop = validate_proposal_event(proposal_event, power, min_proposer_power, revs)
    pid = proposal_event.id
    seen = dict(first_seen or {})
    collected = int(time.time()) if collected_at is None else collected_at
    rejected: Counter[str] = Counter()
    n_opt = len(prop.options)

    votes: dict[str, list[_Version]] = {}
    allocs: dict[str, list[TimedAllocation]] = {}
    dels: dict[str, list[_Version]] = {}

    for ev in sorted(event_list, key=lambda e: e.id):
        if ev.kind == VOTE_KIND:
            try:
                vote, deleg = validate_vote_event(ev, pid, n_opt)
            except GovernanceError:
                rejected["invalid-vote"] += 1
                continue
            t = anchored.get(ev.id)
            if t is None:
                rejected["unanchored"] += 1
            elif t > prop.deadline:
                rejected["anchored-after-deadline"] += 1
            elif seen.get(ev.id, collected) > prop.deadline:
                rejected["first-seen-after-deadline"] += 1
            else:
                try:
                    voter = verify_delegation(deleg, ev.pubkey, t, revs)
                except DelegationError:
                    rejected["delegation-invalid-at-anchor"] += 1
                    continue
                votes.setdefault(voter, []).append(_Version(vote.nonce, ev.id, vote))
        elif ev.kind == ALLOCATION_KIND:
            try:
                alloc = validate_allocation_event(ev, revs)
            except PowerError:
                rejected["invalid-allocation"] += 1
                continue
            allocs.setdefault(alloc.address, []).append(
                TimedAllocation(alloc, ev.id, anchored.get(ev.id))
            )
        elif ev.kind == GOV_DELEGATION_KIND:
            try:
                gd = validate_gov_delegation_event(ev)
            except GovernanceError:
                rejected["invalid-delegation"] += 1
                continue
            t = anchored.get(ev.id)
            if t is None:
                rejected["delegation-unanchored"] += 1
            elif t > prop.snapshot_time:
                rejected["delegation-after-snapshot"] += 1
            else:
                dels.setdefault(gd.address, []).append(_Version(gd.nonce, ev.id, gd))

    chosen_votes: dict[str, _Version] = {}
    for addr, vs in votes.items():
        best = _best(vs)
        if best is not None:
            chosen_votes[addr] = best
    chosen_dels: dict[str, GovDelegation] = {}
    for addr, vs in dels.items():
        best = _best(vs)
        if best is not None:
            chosen_dels[addr] = best.value

    missing: set[str] = set()
    cache: dict[str, int | None] = {}
    floor = prop.dust_floor * TOKEN_UNIT

    def token_power(addr: str) -> int | None:
        if addr not in cache:
            try:
                p = power(addr, prop.snapshot_blocks)
            except OracleError:
                missing.add(addr)
                cache[addr] = None
            else:
                cache[addr] = None if p < floor else p
        return cache[addr]

    def gov_weight(addr: str) -> int:
        p = token_power(addr)
        if p is None:
            return 0
        alloc = allocation_in_force(allocs.get(addr, []), prop.snapshot_time, delay_d)
        return governance_power(alloc, p, default_governance_bps)

    delegated_in: dict[str, int] = {}
    unused = 0
    for d_addr in sorted(chosen_dels):
        if d_addr in chosen_votes:
            continue  # a direct vote overrides the delegation
        w = gov_weight(d_addr)
        for target, bps in sorted(chosen_dels[d_addr].to.items()):
            amount = w * bps // BPS
            if target in chosen_votes:
                delegated_in[target] = delegated_in.get(target, 0) + amount
            else:
                unused += amount

    totals = [0] * n_opt
    lines: list[VoterLine] = []
    for addr in sorted(chosen_votes):
        if token_power(addr) is None:
            if addr not in missing:
                rejected["below-dust-floor"] += 1
            continue
        option = chosen_votes[addr].value.option
        own = gov_weight(addr)
        extra = delegated_in.get(addr, 0)
        totals[option] += own + extra
        lines.append(
            VoterLine(
                address=addr,
                option=option,
                own=str(own),
                delegated_in=str(extra),
                weight=str(own + extra),
            )
        )

    return TallyResult(
        proposal_id=pid,
        title=prop.title,
        options=prop.options,
        totals=tuple(str(t) for t in totals),
        voters=tuple(lines),
        rejected=dict(sorted(rejected.items())),
        unused_delegated=str(unused),
        complete=(coverage is not None and coverage.complete) and not missing,
        relays_answered=() if coverage is None else tuple(sorted(coverage.answered)),
        relays_unreachable={} if coverage is None else dict(sorted(coverage.unreachable.items())),
        missing_power=tuple(sorted(missing)),
        snapshot_time=prop.snapshot_time,
        deadline=prop.deadline,
        delay_d=delay_d,
    )


async def tally_from_relays(
    proposal_id: str,
    relays: Sequence[str],
    *,
    power: PowerFn,
    verifiers: Mapping[str, Anchor],
    extra_proofs: Iterable[AnchorProof] = (),
    prior_first_seen: Mapping[str, int] | None = None,
    delay_d: int = DEFAULT_DELAY_D_S,
    relay_timeout: float = 5.0,
    clock: Callable[[], float] = time.time,
    min_proposer_power: int = DEFAULT_MIN_PROPOSER_POWER,
) -> TallyResult:
    """Fetch from the official relays, verify anchors, tally. Reports relay coverage (SEP-3.36)."""
    first = await collect(
        relays, [{"ids": [proposal_id]}], relay_timeout=relay_timeout, clock=clock
    )
    if proposal_id not in first.events:
        raise GovernanceError(
            f"proposal {proposal_id[:12]}.. not found on any of {len(relays)} relays "
            f"(unreachable: {first.coverage.unreachable or 'none'}); check the id and relays"
        )
    prop_event = first.events[proposal_id]
    prop = validate_proposal_event(prop_event, power, min_proposer_power)
    official = list(dict.fromkeys([*relays, *prop.relays]))
    got = await collect(
        official,
        [
            {"kinds": [VOTE_KIND], "#e": [proposal_id]},
            {"kinds": [ALLOCATION_KIND]},
            {"kinds": [GOV_DELEGATION_KIND]},
            {"kinds": [REVOCATION_KIND]},
            {"kinds": [ANCHOR_KIND]},
        ],
        relay_timeout=relay_timeout,
        clock=clock,
        prior_first_seen=prior_first_seen,
    )
    proofs = list(extra_proofs)
    for ev in got.events.values():
        if ev.kind == ANCHOR_KIND:
            try:
                proofs.append(proof_from_attestation(ev))
            except GovernanceError:
                continue
    evs = [e for e in got.events.values() if e.kind != ANCHOR_KIND]
    return tally(
        prop_event,
        evs,
        anchored=anchored_map(evs, proofs, verifiers),
        power=power,
        first_seen=got.first_seen,
        collected_at=int(clock()),
        delay_d=delay_d,
        coverage=got.coverage,
        min_proposer_power=min_proposer_power,
    )
