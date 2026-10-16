"""Reading the relay: one anonymous, read-only snapshot of the public event kinds.

The API holds no key and publishes nothing. It connects like any zero-power client, so it sees a signal only once
the slowest public tier would, and never the content of a private signal (only the commitments, later).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sa_core.models import (
    ALLOCATION_KIND,
    ANCHOR_KIND,
    COMMITMENT_KIND,
    REVOCATION_KIND,
    RULESET_KIND,
    SIGNAL_KIND,
    NostrEvent,
)
from sa_core.nostr import NostrError, RelayClient, verify_event

PAGE = 500
MAX_PAGES = 200


@dataclass
class Snapshot:
    """Events by kind from every relay that answered, de-duplicated by id."""

    taken_at: float
    events: dict[str, NostrEvent] = field(default_factory=dict)
    answered: list[str] = field(default_factory=list)
    unreachable: dict[str, str] = field(default_factory=dict)

    def of_kind(self, kind: int) -> list[NostrEvent]:
        return sorted(
            (e for e in self.events.values() if e.kind == kind), key=lambda e: (e.created_at, e.id)
        )


KINDS: tuple[int, ...] = (
    RULESET_KIND,
    ALLOCATION_KIND,
    REVOCATION_KIND,
    SIGNAL_KIND,
    COMMITMENT_KIND,
    ANCHOR_KIND,
)


async def _pages(relay: RelayClient, kind: int) -> list[NostrEvent]:
    """Every stored event of ``kind`` the relay will show us, newest page first (the relay caps one query)."""
    out: dict[str, NostrEvent] = {}
    until: int | None = None
    for _ in range(MAX_PAGES):
        flt: dict[str, Any] = {"kinds": [kind], "limit": PAGE}
        if until is not None:
            flt["until"] = until
        got = await relay.query([flt], timeout=10)
        fresh = [e for e in got if e.id not in out]
        for e in got:
            out[e.id] = e
        if len(got) < PAGE or not fresh:
            break
        until = min(e.created_at for e in got)
    return list(out.values())


async def _read_one(url: str, kinds: Sequence[int]) -> list[NostrEvent]:
    async with RelayClient(url, connect_timeout=4.0) as relay:
        events: list[NostrEvent] = []
        for kind in kinds:
            events.extend(await _pages(relay, kind))
        return events


async def fetch_snapshot(
    relays: Sequence[str], kinds: Sequence[int] = KINDS, clock: Callable[[], float] = time.time
) -> Snapshot:
    """Read ``kinds`` from every relay concurrently. A relay that fails is reported, not fatal."""
    results = await asyncio.gather(*(_read_one(u, kinds) for u in relays), return_exceptions=True)
    snap = Snapshot(taken_at=clock())
    for url, res in zip(relays, results, strict=True):
        if isinstance(res, BaseException):
            if not isinstance(res, NostrError | OSError | TimeoutError):
                raise res
            snap.unreachable[url] = str(res) or type(res).__name__
            continue
        snap.answered.append(url)
        for ev in res:
            try:
                verify_event(ev)
            except NostrError:
                continue  # a relay that serves a forged event does not get it into any record
            snap.events[ev.id] = ev
    return snap


async def relay_ok(url: str) -> bool:
    """True when the relay accepts a websocket connection."""
    try:
        async with RelayClient(url, connect_timeout=2.0):
            return True
    except (NostrError, OSError, TimeoutError):
        return False
