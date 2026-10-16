"""Who may read a private provider's signals: followers whose allocated power meets the threshold.

Read from the relay (kind 9332 allocations, which are public) and the balance oracle, with the same rules
the relay applies when it gates delivery: the allocation in force (highest nonce, smallest event id on a
tie, effective ``delay_d`` after it was published) times token power times the provider's share, compared
with the rule set's private threshold (``sa_core.power.resolve_tier``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sa_core.chains import BalanceOracle, OracleError
from sa_core.delegation import DelegationError, revocations_from_events, verify_event_delegation
from sa_core.models import (
    ALLOCATION_KIND,
    REVOCATION_KIND,
    NostrEvent,
    PowerAllocation,
    ProviderRuleSet,
    Revocation,
)
from sa_core.nostr import RelayClient
from sa_core.power import (
    PowerError,
    TimedAllocation,
    allocation_in_force,
    provider_power,
    resolve_tier,
    validate_allocation_event,
)

log = logging.getLogger("sa.entitlement")


class Querier(Protocol):
    async def query(self, filters: Sequence[dict[str, Any]], timeout: float = ...) -> list[NostrEvent]: ...


class UrlQuerier:
    """Queries a relay over a short-lived connection, so a refresh never shares a socket with publishing."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def query(self, filters: Sequence[dict[str, Any]], timeout: float = 5.0) -> list[NostrEvent]:
        async with RelayClient(self.url, connect_timeout=timeout) as client:
            return await client.query(filters, timeout=timeout)


@dataclass(frozen=True)
class Entitled:
    pubkey: str  # the device key that published the allocation: the recipient of the NIP-44 ciphertext
    address: str
    power: int  # 18-decimal units allocated to this provider


class RelayEntitlements:
    def __init__(
        self,
        relay: Querier,
        provider: str,
        rules: ProviderRuleSet,
        oracle: BalanceOracle,
        *,
        delay_d: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if rules.private is None:
            raise ValueError("entitlements only exist for a private provider; this rule set has public tiers")
        self.relay, self.provider, self.rules, self.oracle = relay, provider, rules, oracle
        self.delay_d, self.clock = delay_d, clock

    async def refresh(self) -> list[Entitled]:
        events = await self.relay.query([{"kinds": [ALLOCATION_KIND]}, {"kinds": [REVOCATION_KIND]}])
        revocations = revocations_from_events(events)
        history: dict[str, list[TimedAllocation]] = defaultdict(list)
        publisher: dict[tuple[str, int], list[NostrEvent]] = defaultdict(list)
        for ev in events:
            if ev.kind != ALLOCATION_KIND:
                continue
            try:
                alloc = validate_allocation_event(ev, revocations)
            except PowerError as exc:
                log.warning("ignoring invalid allocation %s: %s", ev.id[:12], exc)
                continue
            history[alloc.address].append(TimedAllocation(alloc, ev.id, ev.created_at))
            publisher[(alloc.address, alloc.nonce)].append(ev)
        now = int(self.clock())
        out: list[Entitled] = []
        for address in sorted(history):
            in_force = allocation_in_force(history[address], now, self.delay_d)
            if in_force is None or self.provider not in in_force.providers:
                continue
            alloc = in_force
            power = await self._power(address, alloc)
            if power is not None and resolve_tier(self.rules, power).entitled:
                recipient = self._recipient(publisher[(address, alloc.nonce)], address, now, revocations)
                if recipient is not None:
                    out.append(Entitled(recipient.pubkey, address, power))
        return out

    @staticmethod
    def _recipient(
        events: Sequence[NostrEvent], address: str, now: int, revocations: Sequence[Revocation]
    ) -> NostrEvent | None:
        """The NIP-44 recipient: the smallest-id publisher of the in-force allocation whose device key is
        delegated by ``address`` *now* (the tag was checked when the allocation was validated; expiry and
        revocation are judged at delivery time). A key that does not speak for the address never receives."""
        for ev in sorted(events, key=lambda e: e.id):
            try:
                verify_event_delegation(ev, now, address, revocations)
            except DelegationError as exc:
                log.warning("device key %s no longer speaks for %s: %s", ev.pubkey[:12], address, exc)
                continue
            return ev
        return None

    async def _power(self, address: str, alloc: PowerAllocation) -> int | None:
        try:
            total = (await asyncio.to_thread(self.oracle.power, address)).total
        except OracleError as exc:
            log.warning("cannot read the balance of %s, treating it as not entitled: %s", address, exc)
            return None
        return provider_power(alloc, self.provider, total)
