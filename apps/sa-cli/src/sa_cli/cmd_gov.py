"""``sa gov propose | vote | tally``: token-weighted proposals over the same relay."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path

from sa_cli.cmd_identity import oracle_for
from sa_cli.home import CliError, Home
from sa_cli.relay_util import publish_event
from sa_core.anchors import Anchor, BlockstreamHeaders, OtsAnchor, verifier_map
from sa_core.chains import OracleError
from sa_core.governance import (
    GovernanceError,
    PowerFn,
    TallyResult,
    build_anchor_attestation,
    build_proposal_event,
    build_vote_event,
    collect,
    oracle_power,
    tally,
    tally_from_relays,
)
from sa_core.models import (
    ALLOCATION_KIND,
    ANCHOR_KIND,
    GOV_DELEGATION_KIND,
    REVOCATION_KIND,
    VOTE_KIND,
    Proposal,
    Vote,
)
from sa_core.nostr import NostrError

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    m = _DURATION.match(text)
    if not m:
        raise CliError(f"duration {text!r} must look like 90s, 30m, 12h or 3d")
    return int(float(m.group(1)) * _UNIT[m.group(2)])


def propose(args: argparse.Namespace, home: Home) -> int:
    device = home.load_device()
    delegation = home.load_delegation(device)
    now = int(time.time())
    try:
        proposal = Proposal(
            title=args.title, options=tuple(args.option), deadline=now + parse_duration(args.deadline_in),
            snapshot_time=now, relays=tuple(args.relay),
        )  # fmt: skip
    except ValueError as exc:
        raise CliError(f"invalid proposal: {exc}") from exc
    event = build_proposal_event(device, proposal, delegation, now)
    asyncio.run(publish_event(args.relay[0], device, delegation, event))
    print(
        f"proposal {event.id}: {proposal.title!r}, {len(proposal.options)} options, voting ends in {args.deadline_in}"
    )
    return 0


def vote(args: argparse.Namespace, home: Home) -> int:
    device = home.load_device()
    delegation = home.load_delegation(device)
    try:
        v = Vote(proposal=args.proposal, option=args.option, nonce=int(time.time()))
    except ValueError as exc:
        raise CliError(f"invalid vote: {exc}") from exc
    event = build_vote_event(device, v, delegation)

    async def send() -> None:
        await publish_event(args.relay, device, delegation, event)
        if args.stamp_ots:
            try:
                proof = await asyncio.to_thread(OtsAnchor().stamp, bytes.fromhex(event.id))
            except Exception as exc:
                raise CliError(
                    f"vote published, but OpenTimestamps stamping failed: {exc}"
                ) from exc
            await publish_event(
                args.relay, device, delegation, build_anchor_attestation(device, proof)
            )
            print("OpenTimestamps proof submitted (pending until Bitcoin confirms, usually hours)")

    asyncio.run(send())
    print(f"vote {event.id} for option {args.option} of proposal {args.proposal[:12]}.. published")
    return 0


def tally_cmd(args: argparse.Namespace, home: Home) -> int:
    oracle = oracle_for(args.balances)
    power = oracle_power(oracle)
    try:
        if args.demo_trust_created_at:
            result = asyncio.run(_demo_tally(args, power))
        else:
            verifiers: dict[str, Anchor] = verifier_map(OtsAnchor(BlockstreamHeaders()))
            result = asyncio.run(
                tally_from_relays(
                    args.proposal,
                    args.relay,
                    power=power,
                    verifiers=verifiers,
                    delay_d=args.delay_d,
                    min_proposer_power=args.min_proposer_power,
                )
            )
    except (GovernanceError, NostrError, OracleError) as exc:
        raise CliError(str(exc)) from exc
    text = result.canonical_json()
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"tally written: {args.output}")
    else:
        print(text)
    if not result.complete:
        print(
            "note: the result is INCOMPLETE (a relay did not answer or a balance could not be read)"
        )
    return 0


async def _demo_tally(args: argparse.Namespace, power: PowerFn) -> TallyResult:
    """DEMO ONLY: treats each event's own created_at as its anchored time. Not tamper-proof."""
    first = await collect(args.relay, [{"ids": [args.proposal]}])
    if args.proposal not in first.events:
        raise GovernanceError(f"proposal {args.proposal[:12]}.. not found on {args.relay}")
    prop_event = first.events[args.proposal]
    got = await collect(
        args.relay,
        [
            {"kinds": [VOTE_KIND], "#e": [args.proposal]},
            {"kinds": [ALLOCATION_KIND]},
            {"kinds": [GOV_DELEGATION_KIND]},
            {"kinds": [REVOCATION_KIND]},
        ],
    )
    events = [e for e in got.events.values() if e.kind != ANCHOR_KIND]
    print("DEMO tally: created_at is trusted as the anchored time; do not use for a binding result")
    return tally(
        prop_event, events, anchored={e.id: e.created_at for e in events}, power=power,
        first_seen=got.first_seen, delay_d=args.delay_d, coverage=got.coverage,
        min_proposer_power=args.min_proposer_power,
    )  # fmt: skip
