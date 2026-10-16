"""Private-provider entitlement: allocations on the relay + balances + the private threshold."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sa_core.chains import BalanceOracle
from sa_core.delegation import delegation_tag, revocation_event, sign_delegation, sign_revocation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import ALLOCATION_KIND, NostrEvent
from sa_core.nostr import RelayClient, build_event
from sa_core.power import allocation_event, sign_allocation

from sa_engine.network import fixture
from sa_engine.network.entitlement import RelayEntitlements
from tests.fake_relay import FakeRelay
from tests.net_helpers import Identity

PROVIDER = Identity()


@pytest.fixture
async def relay() -> AsyncIterator[FakeRelay]:
    r = FakeRelay()
    await r.start()
    yield r
    await r.stop()


async def allocate(
    relay: FakeRelay, who: Identity, nonce: int, bps: int, provider: str | None = None, at: int | None = None
) -> None:
    alloc = sign_allocation(who.sa, nonce, 0, {provider or PROVIDER.pubkey: bps})
    await relay.inject(allocation_event(who.device, alloc, who.delegation, at))


async def entitled(relay: FakeRelay, oracle: BalanceOracle, *, delay_d: int = 0, min_power: int = 1000) -> list[str]:
    async with RelayClient(relay.url) as client:
        src = RelayEntitlements(
            client, PROVIDER.pubkey, PROVIDER.private_rules(min_power=min_power), oracle, delay_d=delay_d
        )
        return sorted(e.pubkey for e in await src.refresh())


async def test_only_followers_whose_allocated_power_meets_the_threshold_are_entitled(
    relay: FakeRelay, tmp_path: Path
) -> None:
    rich, half, poor, elsewhere = Identity(), Identity(), Identity(), Identity()
    balances = tmp_path / "b.json"
    for who, tokens in ((rich, 5000), (half, 1500), (poor, 10), (elsewhere, 9000)):
        fixture.set_balance(balances, who.sa.address, tokens)
    await allocate(relay, rich, 1, 10_000)  # 5000 allocated
    await allocate(relay, half, 1, 5_000)  # 750 allocated: below 1000
    await allocate(relay, poor, 1, 10_000)  # 10 allocated
    await allocate(relay, elsewhere, 1, 10_000, provider=Identity().pubkey)  # power goes to another provider
    got = await entitled(relay, fixture.fixture_oracle(balances))
    assert got == [rich.pubkey]


async def test_the_newest_allocation_wins_and_can_withdraw_entitlement(relay: FakeRelay, tmp_path: Path) -> None:
    f = Identity()
    balances = tmp_path / "b.json"
    fixture.set_balance(balances, f.sa.address, 5000)
    await allocate(relay, f, 1, 10_000)
    assert await entitled(relay, fixture.fixture_oracle(balances)) == [f.pubkey]
    await allocate(relay, f, 2, 100)  # 50 allocated now
    assert await entitled(relay, fixture.fixture_oracle(balances)) == []
    fixture.set_balance(balances, f.sa.address, 0)
    await allocate(relay, f, 3, 10_000)
    assert await entitled(relay, fixture.fixture_oracle(balances)) == []  # allocated all of nothing


async def test_the_allocation_delay_d_postpones_entitlement(relay: FakeRelay, tmp_path: Path) -> None:
    f = Identity()
    balances = tmp_path / "b.json"
    fixture.set_balance(balances, f.sa.address, 5000)
    await allocate(relay, f, 1, 10_000, at=int(time.time()))
    assert await entitled(relay, fixture.fixture_oracle(balances), delay_d=3600) == []
    await allocate(relay, f, 2, 10_000, at=int(time.time()) - 7200)
    assert await entitled(relay, fixture.fixture_oracle(balances), delay_d=3600) == [f.pubkey]  # nonce 2 is old enough


async def test_invalid_allocations_and_unreadable_balances_never_grant_access(relay: FakeRelay, tmp_path: Path) -> None:
    f = Identity()
    balances = tmp_path / "b.json"
    fixture.set_balance(balances, f.sa.address, 5000)
    forged = sign_allocation(Identity().sa, 1, 0, {PROVIDER.pubkey: 10_000}).model_copy(
        update={"address": f.sa.address}
    )
    await relay.inject(allocation_event(f.device, forged, f.delegation))  # signed by someone else's SA key
    assert await entitled(relay, fixture.fixture_oracle(balances)) == []
    await allocate(relay, f, 1, 10_000)
    balances.write_text("{broken")
    assert await entitled(relay, fixture.fixture_oracle(balances)) == []  # unknown is not zero, and not entitled


def test_a_public_rule_set_has_no_entitlements() -> None:
    with pytest.raises(ValueError, match="private provider"):
        RelayEntitlements(None, PROVIDER.pubkey, PROVIDER.rules(), None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- security review H1: 9332 needs a delegation


def _rewrap(victim_event: NostrEvent, tags: list[tuple[str, ...]]) -> NostrEvent:
    """A stranger republishes the victim's allocation (valid EIP-191 content) under a device key whose event id
    sorts below the victim's, which used to make the stranger the NIP-44 recipient."""
    while True:
        stranger = DeviceKey.generate()
        ev = build_event(stranger, ALLOCATION_KIND, victim_event.content, tags, victim_event.created_at)
        if ev.id < victim_event.id:
            return ev


async def test_a_stranger_rewrapping_an_allocation_cannot_become_the_private_recipient(
    relay: FakeRelay, tmp_path: Path
) -> None:
    victim = Identity()
    balances = tmp_path / "b.json"
    fixture.set_balance(balances, victim.sa.address, 5000)
    alloc = sign_allocation(victim.sa, 1, 0, {PROVIDER.pubkey: 10_000})
    legit = build_event(  # built by hand: the same wire event on the old and the new code
        victim.device,
        ALLOCATION_KIND,
        alloc.model_dump_json(),
        [("p", PROVIDER.pubkey), delegation_tag(victim.delegation)],
        int(time.time()),
    )
    await relay.inject(legit)
    foreign = sign_delegation(SAKey.generate(), DeviceKey.generate().pubkey_hex, 10**10)  # someone else's tag
    variants: list[list[tuple[str, ...]]] = [
        [("p", PROVIDER.pubkey)],
        [("p", PROVIDER.pubkey), ("sa-delegation", foreign.evm_address, "9", "0x" + "11" * 65)],
    ]
    for tags in variants:
        await relay.inject(_rewrap(legit, tags))
    assert await entitled(relay, fixture.fixture_oracle(balances)) == [victim.pubkey]  # the delivery goes to the victim


async def test_a_republished_allocation_with_no_delegated_publisher_entitles_nobody(
    relay: FakeRelay, tmp_path: Path
) -> None:
    victim = Identity()
    balances = tmp_path / "b.json"
    fixture.set_balance(balances, victim.sa.address, 5000)
    alloc = sign_allocation(victim.sa, 1, 0, {PROVIDER.pubkey: 10_000})
    stranger = DeviceKey.generate()
    await relay.inject(build_event(stranger, ALLOCATION_KIND, alloc.model_dump_json(), [("p", PROVIDER.pubkey)]))
    assert await entitled(relay, fixture.fixture_oracle(balances)) == []


async def test_a_revoked_device_key_is_not_the_recipient_and_a_delegated_one_takes_over(
    relay: FakeRelay, tmp_path: Path
) -> None:
    victim = Identity()
    second = DeviceKey.generate()
    second_deleg = sign_delegation(victim.sa, second.pubkey_hex, 10**10)
    balances = tmp_path / "b.json"
    fixture.set_balance(balances, victim.sa.address, 5000)
    alloc = sign_allocation(victim.sa, 1, 0, {PROVIDER.pubkey: 10_000})
    now = int(time.time())
    await relay.inject(allocation_event(victim.device, alloc, victim.delegation, now - 100))
    await relay.inject(allocation_event(second, alloc, second_deleg, now - 100))
    both = await entitled(relay, fixture.fixture_oracle(balances))
    assert len(both) == 1 and both[0] in (victim.pubkey, second.pubkey_hex)
    # the victim revokes whichever device key was chosen; the other one is then the recipient
    chosen, other = (
        (victim.pubkey, second.pubkey_hex) if both[0] == victim.pubkey else (second.pubkey_hex, victim.pubkey)
    )
    await relay.inject(revocation_event(DeviceKey.generate(), sign_revocation(victim.sa, chosen, now - 10)))
    assert await entitled(relay, fixture.fixture_oracle(balances)) == [other]
