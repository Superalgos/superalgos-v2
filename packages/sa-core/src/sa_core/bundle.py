"""Provider history bundles: export, import, completeness (SEP-3.14, SEP-3.15).

A bundle is the provider's signed events (public signals and commitments), the reveals of
committed signals, and the anchor proofs, as one canonical JSON document. `check_bundle` verifies
it with no relay:

- every event id and signature, and that the provider's key signed it;
- the `sa-delegation` tag signature (expiry is judged later against anchored time);
- per strategy, seq runs 1..n with no gap, no duplicate, in order, and each `prev` equals the
  chain reference of the previous item (event id of the signal, or of the commitment for private
  signals), so an omitted, reordered or edited item is flagged;
- each reveal hashes to its commitment and fills its slot.

Limit: nothing inside a bundle can show that signals after the last one were cut off. Pass
`expected_heads` (from relays or from followers) to detect tail truncation; without it the report
says `head_checked: false`.
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ._canon import canonical_json
from .delegation import (
    DelegationError,
    delegation_from_event,
    ensure_not_revoked,
    verify_delegation,
)
from .models import (
    COMMITMENT_KIND,
    SIGNAL_KIND,
    AnchorProof,
    Bundle,
    Commitment,
    Delegation,
    NostrEvent,
    Reveal,
    Revocation,
    Signal,
)
from .nostr import NostrError, verify_event
from .signals import SignalError, check_reveal


class BundleError(Exception):
    """A bundle file cannot be read. The message names the problem and the path."""


def _parse_public_signal(ev: NostrEvent) -> Signal | None:
    try:
        return Signal.model_validate_json(ev.content)
    except ValidationError:
        return None


def _slot(ev: NostrEvent) -> tuple[str, int] | None:
    """(strategy_id, seq) of a signal or commitment event, if its content is readable."""
    if ev.kind == SIGNAL_KIND and (s := _parse_public_signal(ev)) is not None:
        return s.strategy_id, s.seq
    if ev.kind == COMMITMENT_KIND:
        try:
            c = Commitment.model_validate_json(ev.content)
        except ValidationError:
            return None
        return c.strategy_id, c.seq
    return None


def export_bundle(
    provider: str,
    events: Iterable[NostrEvent],
    proofs: Iterable[AnchorProof] = (),
    reveals: Iterable[Reveal] = (),
    exported_at: int = 0,
) -> Bundle:
    """Select the provider's chain events, order them by (strategy, seq), attach their proofs."""
    chosen: dict[str, NostrEvent] = {}
    for ev in events:
        if ev.pubkey != provider or ev.kind not in (SIGNAL_KIND, COMMITMENT_KIND):
            continue
        if _slot(ev) is None:
            continue  # recipient-specific ciphertext or junk: commitments cover private signals
        chosen[ev.id] = ev
    ordered = sorted(chosen.values(), key=lambda e: (*(_slot(e) or ("", 0)), e.id))
    ids = set(chosen)
    commits = {e.id for e in ordered if e.kind == COMMITMENT_KIND}
    return Bundle(
        provider=provider,
        events=tuple(ordered),
        reveals=tuple(
            sorted(
                (r for r in reveals if r.commitment_id in commits), key=lambda r: r.commitment_id
            )
        ),
        proofs=tuple(
            sorted((p for p in proofs if p.digest in ids), key=lambda p: (p.digest, p.kind, p.data))
        ),
        exported_at=exported_at,
    )


def bundle_to_json(bundle: Bundle) -> str:
    """Canonical JSON: identical bundles give identical bytes."""
    return canonical_json(bundle.model_dump(mode="json"))


def bundle_from_json(text: str) -> Bundle:
    try:
        return Bundle.model_validate_json(text)
    except ValueError as exc:
        raise BundleError(f"not a Superalgos bundle: {exc}") from exc


def write_bundle(path: Path, bundle: Bundle) -> None:
    Path(path).write_text(bundle_to_json(bundle) + "\n", encoding="utf-8")


def read_bundle(path: Path) -> Bundle:
    try:
        return bundle_from_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleError(f"bundle {path} not found; export one with `sa bundle export`") from exc


# ---------------------------------------------------------------- completeness


class ChainItem(BaseModel):
    """One slot of a strategy's chain, public signal or commitment (with reveal if any)."""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    seq: int
    ref: str  # event id used as `prev` by the next item
    prev: str | None
    kind: Literal["signal", "commitment"]
    event: NostrEvent
    delegation: Delegation
    signal: Signal | None  # None: a commitment that was never revealed


class BundleCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    complete: bool
    head_checked: bool
    signature_failures: int = (
        0  # events whose id, signature or delegation did not verify (excluded from items)
    )
    items: tuple[ChainItem, ...]
    gaps: tuple[str, ...]  # "strategy:seq" missing from 1..max
    problems: tuple[str, ...]
    unrevealed: tuple[str, ...]  # commitment event ids with no valid reveal


def check_bundle(
    bundle: Bundle,
    *,
    expected_heads: Mapping[str, tuple[int, str]] | None = None,
    revocations: Iterable[Revocation] = (),
) -> BundleCheck:
    """Verify a bundle offline. `expected_heads`: strategy_id -> (last seq, last ref)."""
    problems: list[str] = []
    revs = list(revocations)
    reveal_by_commit = {r.commitment_id: r for r in bundle.reveals}
    slots: dict[str, list[ChainItem]] = defaultdict(list)
    order_in_bundle: dict[str, list[int]] = defaultdict(list)
    unrevealed: list[str] = []
    seen_commits: set[str] = set()
    bad_signatures = 0

    for ev in bundle.events:
        try:
            verify_event(ev)
        except NostrError as exc:
            problems.append(f"invalid event: {exc}")
            bad_signatures += 1
            continue
        if ev.pubkey != bundle.provider:
            problems.append(
                f"event {ev.id[:12]}.. is signed by {ev.pubkey[:12]}.., not the provider"
            )
            bad_signatures += 1
            continue
        try:
            deleg = delegation_from_event(ev)
            verify_delegation(
                deleg, ev.pubkey, 0, revs
            )  # authenticity only; expiry vs anchored time later
            ensure_not_revoked(
                deleg, ev.created_at, revs
            )  # a revoked key does not speak from effective_at
        except DelegationError as exc:
            problems.append(f"event {ev.id[:12]}.. delegation invalid: {exc}")
            bad_signatures += 1
            continue
        if ev.kind == SIGNAL_KIND:
            sig = _parse_public_signal(ev)
            if sig is None:
                problems.append(f"signal event {ev.id[:12]}.. content is not a valid signal")
                continue
            item = ChainItem(
                strategy_id=sig.strategy_id, seq=sig.seq, ref=ev.id, prev=sig.prev, kind="signal",
                event=ev, delegation=deleg, signal=sig,
            )  # fmt: skip
        elif ev.kind == COMMITMENT_KIND:
            try:
                com = Commitment.model_validate_json(ev.content)
            except ValidationError as exc:
                problems.append(
                    f"commitment {ev.id[:12]}.. content invalid: {exc.errors()[0]['msg']}"
                )
                continue
            seen_commits.add(ev.id)
            revealed: Signal | None = None
            rev = reveal_by_commit.get(ev.id)
            if rev is not None:
                try:
                    revealed = check_reveal(com, rev.plaintext)
                except SignalError as exc:
                    problems.append(f"reveal of commitment {ev.id[:12]}.. rejected: {exc}")
            if revealed is None:
                unrevealed.append(ev.id)
            item = ChainItem(
                strategy_id=com.strategy_id, seq=com.seq, ref=ev.id, prev=com.prev, kind="commitment",
                event=ev, delegation=deleg, signal=revealed,
            )  # fmt: skip
        else:
            problems.append(
                f"event {ev.id[:12]}.. has kind {ev.kind}, which a bundle does not carry"
            )
            continue
        order_in_bundle[item.strategy_id].append(item.seq)
        slots[item.strategy_id].append(item)

    for rid in reveal_by_commit:
        if rid not in seen_commits:
            problems.append(f"reveal names commitment {rid[:12]}.. that is not in the bundle")

    gaps: list[str] = []
    items: list[ChainItem] = []
    for sid in sorted(slots):
        seqs = order_in_bundle[sid]
        if seqs != sorted(seqs):
            problems.append(f"strategy {sid}: items are not in seq order {seqs}")
        by_seq: dict[int, list[ChainItem]] = defaultdict(list)
        for it in slots[sid]:
            by_seq[it.seq].append(it)
        for seq, group in sorted(by_seq.items()):
            if len(group) > 1:
                problems.append(
                    f"strategy {sid}: seq {seq} appears {len(group)} times (fork or duplicate)"
                )
        top = max(by_seq)
        gaps.extend(f"{sid}:{n}" for n in range(1, top + 1) if n not in by_seq)
        chain = [min(by_seq[n], key=lambda i: i.event.id) for n in sorted(by_seq)]
        for a, b in itertools.pairwise(chain):
            if b.seq == a.seq + 1 and b.prev != a.ref:
                problems.append(
                    f"strategy {sid}: seq {b.seq} prev {str(b.prev)[:12]}.. is not the id of seq {a.seq} ({a.ref[:12]}..)"
                )
        items.extend(chain)

    head_checked = expected_heads is not None and set(expected_heads) >= set(slots)
    if expected_heads is not None:
        for sid, (seq, ref) in sorted(expected_heads.items()):
            have = [i for i in items if i.strategy_id == sid]
            last = have[-1] if have else None
            if last is None or last.seq < seq:
                problems.append(
                    f"strategy {sid}: bundle ends at seq {last.seq if last else 0}, expected head seq {seq} (tail truncated)"
                )
            elif last.seq == seq and last.ref != ref:
                problems.append(
                    f"strategy {sid}: head ref {last.ref[:12]}.. differs from expected {ref[:12]}.."
                )
            elif last.seq > seq:
                problems.append(
                    f"strategy {sid}: bundle is ahead of expected head (seq {last.seq} > {seq})"
                )

    return BundleCheck(
        complete=not gaps and not problems,
        head_checked=head_checked,
        signature_failures=bad_signatures,
        items=tuple(items),
        gaps=tuple(gaps),
        problems=tuple(sorted(problems)),
        unrevealed=tuple(sorted(unrevealed)),
    )


def dump_check(check: BundleCheck) -> str:
    """Compact JSON summary (no items) for logs and reports."""
    return json.dumps(
        {"complete": check.complete, "head_checked": check.head_checked,
         "gaps": list(check.gaps), "problems": list(check.problems),
         "unrevealed": list(check.unrevealed)},
        sort_keys=True,
    )  # fmt: skip
