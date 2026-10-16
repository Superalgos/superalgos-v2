"""Power allocations and provider tiers.

- Allocation (kind 9332): whole split in basis points, signed by the SA address (EIP-191).
  Sum <= 10000 bps (SEP-3.33), so a unit of power serves governance or one provider, never both
  (SEP-3.34). The highest valid nonce wins (SEP-3.32). It takes effect D after its anchored time
  (SEP-3.35); an unanchored allocation never takes effect.
- Rule set (kind 37318): a provider's tiers or private rule, bound to a timeframe, signed via the
  provider's SA identity (SEP-3.24).
- Tier: highest tier whose min_power <= allocated power (SEP-3.22). Index 0 is the best tier.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from . import _evm
from .delegation import (
    DelegationError,
    delegation_from_event,
    delegation_tag,
    verify_event_delegation,
)
from .keys import DeviceKey, SAKey
from .models import (
    ALLOCATION_KIND,
    BPS,
    RULESET_KIND,
    TOKEN_UNIT,
    Delegation,
    NostrEvent,
    PowerAllocation,
    PrivateRule,
    ProviderRuleSet,
    Revocation,
    Tier,
)
from .nostr import NostrError, build_event, verify_event

DEFAULT_DELAY_D_S = 7 * 24 * 3600  # SEP-3.35: D >= longest voting period, provisionally 7 days

# Suggested starting table: 0 / 0.1x / 0.5x / 2x of the timeframe.
# Thresholds are placeholders until a holder-distribution snapshot (open question); they satisfy the
# SEP-3.22 example: 1M SA split 5% / 95% gives T2 with the 5% provider and T0 with the 95% one.
DEFAULT_TIERS: tuple[Tier, ...] = (
    Tier(min_power=500_000, delay_fraction=0.0),
    Tier(min_power=100_000, delay_fraction=0.1),
    Tier(min_power=10_000, delay_fraction=0.5),
    Tier(min_power=0, delay_fraction=2.0),
)


class PowerError(Exception):
    """An allocation or rule set is invalid. The message names the value."""


# ---------------------------------------------------------------- allocation


def allocation_message(address: str, nonce: int, governance: int, providers: dict[str, int]) -> str:
    """Canonical EIP-191 text: `sa-allocation:{address}:{nonce}:{governance}:{pk=bps,...}`."""
    body = ",".join(f"{pk}={providers[pk]}" for pk in sorted(providers))
    return f"sa-allocation:{address.lower()}:{nonce}:{governance}:{body}"


def sign_allocation(
    sa: SAKey, nonce: int, governance: int = 0, providers: dict[str, int] | None = None
) -> PowerAllocation:
    prov = dict(providers or {})
    msg = allocation_message(sa.address, nonce, governance, prov)
    return PowerAllocation(
        address=sa.address,
        nonce=nonce,
        governance=governance,
        providers=prov,
        sig=sa.sign_message(msg),
    )


def allocation_event(
    key: DeviceKey, alloc: PowerAllocation, delegation: Delegation, created_at: int | None = None
) -> NostrEvent:
    """Kind 9332, signed by a device key that carries exactly one `sa-delegation` tag from `alloc.address`.

    The EIP-191 signature in the content authorises the split; the tag proves the publishing device key
    speaks for the same address, so a stranger cannot re-wrap someone else's allocation under their own key.
    """
    if delegation.evm_address != alloc.address or delegation.nostr_pubkey != key.pubkey_hex:
        raise PowerError(
            f"delegation is from {delegation.evm_address} for device key {delegation.nostr_pubkey[:12]}.., "
            f"but the allocation is by {alloc.address} and the publisher is {key.pubkey_hex[:12]}..; "
            "use the delegation this SA address signed for this device key"
        )
    tags: list[tuple[str, ...]] = [("p", pk) for pk in sorted(alloc.providers)]
    tags.append(delegation_tag(delegation))
    return build_event(key, ALLOCATION_KIND, alloc.model_dump_json(), tags, created_at)


def validate_allocation_event(
    event: NostrEvent, revocations: Iterable[Revocation] = (), at: int | None = None
) -> PowerAllocation:
    """Event signature, kind, schema (sum <= 10000), EIP-191 signature by `alloc.address`, and delegation.

    The event must carry exactly one valid `sa-delegation` tag whose EVM address equals `alloc.address`
    (the publishing device key is delegated by the allocating address), judged at `at` (default: the
    event's `created_at`) and not revoked by then.
    """
    try:
        verify_event(event)
    except NostrError as exc:
        raise PowerError(str(exc)) from exc
    if event.kind != ALLOCATION_KIND:
        raise PowerError(
            f"event {event.id[:12]}.. has kind {event.kind}, expected {ALLOCATION_KIND}"
        )
    try:
        alloc = PowerAllocation.model_validate_json(event.content)
    except ValidationError as exc:
        raise PowerError(f"allocation content invalid: {exc.errors()[0]['msg']}") from exc
    try:
        _evm.require_signer(
            allocation_message(alloc.address, alloc.nonce, alloc.governance, alloc.providers),
            alloc.sig,
            alloc.address,
        )
    except _evm.SignatureError as exc:
        raise PowerError(f"allocation not signed by its SA address {alloc.address}: {exc}") from exc
    try:
        verify_event_delegation(
            event, event.created_at if at is None else at, alloc.address, revocations
        )
    except DelegationError as exc:
        raise PowerError(
            f"allocation {event.id[:12]}.. by {alloc.address} was published by device key "
            f"{event.pubkey[:12]}.. without a valid delegation from that address: {exc}"
        ) from exc
    return alloc


def is_newer(current: PowerAllocation | None, new: PowerAllocation) -> bool:
    """SEP-3.32: `new` replaces `current` only if it is the same identity with a higher nonce."""
    if current is None:
        return True
    if current.address != new.address:
        raise PowerError(f"allocation for {new.address} cannot replace one for {current.address}")
    return new.nonce > current.nonce


@dataclass(frozen=True)
class TimedAllocation:
    alloc: PowerAllocation
    event_id: str
    anchored: int | None  # earliest proven not_after (unix s); None = unanchored


def effective_at(anchored: int | None, delay_d: int) -> int | None:
    """When an allocation anchored at `anchored` takes effect, or None if it never does."""
    return None if anchored is None else anchored + delay_d


def allocation_in_force(
    history: Iterable[TimedAllocation], at: int, delay_d: int = DEFAULT_DELAY_D_S
) -> PowerAllocation | None:
    """The allocation in force at unix time `at`: effective (anchored + D <= at), highest nonce.

    Ties on nonce (equivocation) resolve to the smallest event id so every verifier agrees.
    """
    eligible = [
        t for t in history if (e := effective_at(t.anchored, delay_d)) is not None and e <= at
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda t: (-t.alloc.nonce, t.event_id)).alloc


def scale_power(token_power: int, bps: int) -> int:
    """`token_power` (18-decimal units) x bps / 10000, floor."""
    if not 0 <= bps <= BPS:
        raise PowerError(f"bps {bps} must be within 0..{BPS}")
    return token_power * bps // BPS


def provider_power(alloc: PowerAllocation | None, provider: str, token_power: int) -> int:
    """Power allocated to `provider` (SEP-3.22): never the raw balance, never other providers' share."""
    return 0 if alloc is None else scale_power(token_power, alloc.providers.get(provider, 0))


def governance_power(
    alloc: PowerAllocation | None, token_power: int, default_governance_bps: int = BPS
) -> int:
    """Governance share (SEP-3.34). With no allocation the whole balance defaults to governance."""
    bps = default_governance_bps if alloc is None else alloc.governance
    return scale_power(token_power, bps)


# ---------------------------------------------------------------- rule sets


def _tier_text(rules_tiers: Sequence[Tier] | None, private: PrivateRule | None) -> str:
    if private is not None:
        return f"private@{private.min_power}"
    if rules_tiers is None:
        raise PowerError("rule set has neither tiers nor private")
    ordered = sorted(rules_tiers, key=lambda t: t.min_power)
    return ",".join(f"{t.min_power}@{t.delay_fraction:.4f}" for t in ordered)


def ruleset_message(
    provider: str, timeframe: int, tiers: Sequence[Tier] | None, private: PrivateRule | None
) -> str:
    """`sa-ruleset:{provider}:{timeframe}:{min@delay,...|private@min}`, delays as `%.4f`."""
    return f"sa-ruleset:{provider}:{timeframe}:{_tier_text(tiers, private)}"


def sign_ruleset(
    sa: SAKey,
    provider: str,
    timeframe: int,
    *,
    tiers: Sequence[Tier] | None = None,
    private: PrivateRule | None = None,
) -> ProviderRuleSet:
    msg = ruleset_message(provider, timeframe, tiers, private)
    return ProviderRuleSet(
        provider=provider,
        sa_address=sa.address,
        timeframe=timeframe,
        tiers=None if tiers is None else tuple(tiers),
        private=private,
        sig=sa.sign_message(msg),
    )


def ruleset_event(
    key: DeviceKey, rules: ProviderRuleSet, delegation: Delegation, created_at: int | None = None
) -> NostrEvent:
    """Kind 37318, d = provider pubkey; carries the provider's `sa-delegation` tag."""
    return build_event(
        key,
        RULESET_KIND,
        rules.model_dump_json(),
        [("d", rules.provider), delegation_tag(delegation)],
        created_at,
    )


def validate_ruleset_event(
    event: NostrEvent, at: int, revocations: Iterable[Revocation] = ()
) -> ProviderRuleSet:
    """SEP-3.24: accepted only if signed via the provider's SA identity.

    Checks: event signature; `d` == `provider` == event pubkey; the event's delegation tag is
    valid at `at` and names `sa_address`; the content EIP-191 signature recovers to `sa_address`.
    """
    try:
        verify_event(event)
    except NostrError as exc:
        raise PowerError(str(exc)) from exc
    if event.kind != RULESET_KIND:
        raise PowerError(f"event {event.id[:12]}.. has kind {event.kind}, expected {RULESET_KIND}")
    try:
        rules = ProviderRuleSet.model_validate_json(event.content)
    except ValidationError as exc:
        raise PowerError(f"rule set content invalid: {exc.errors()[0]['msg']}") from exc
    d_tags = event.tag_values("d")
    if len(d_tags) != 1 or len(d_tags[0]) < 2 or d_tags[0][1] != rules.provider:
        raise PowerError(f"d tag {d_tags!r} must equal provider {rules.provider[:12]}..")
    if event.pubkey != rules.provider:
        raise PowerError(
            f"rule set for provider {rules.provider[:12]}.. was published by {event.pubkey[:12]}.."
        )
    try:
        addr = verify_event_delegation(event, at, None, revocations)
        _ = delegation_from_event(event)
    except DelegationError as exc:
        raise PowerError(f"rule set delegation invalid: {exc}") from exc
    if addr != rules.sa_address:
        raise PowerError(
            f"delegation is from {addr}, rule set names SA identity {rules.sa_address}"
        )
    try:
        _evm.require_signer(
            ruleset_message(rules.provider, rules.timeframe, rules.tiers, rules.private),
            rules.sig,
            rules.sa_address,
        )
    except _evm.SignatureError as exc:
        raise PowerError(f"rule set not signed by SA identity {rules.sa_address}: {exc}") from exc
    return rules


# ---------------------------------------------------------------- tier resolution


@dataclass(frozen=True)
class TierResolution:
    entitled: bool
    tier_index: int | None  # 0 = best tier; None when not entitled or private
    delay_fraction: float  # 0.0 when not entitled
    private: bool
    fallback: bool  # below every threshold: served from the lowest public tier


def resolve_tier(rules: ProviderRuleSet, allocated_power: int) -> TierResolution:
    """Tier for a follower with `allocated_power` (18-decimal units) allocated to this provider."""
    if allocated_power < 0:
        raise PowerError(f"allocated power {allocated_power} is negative")
    if rules.private is not None:
        ok = allocated_power >= rules.private.min_power * TOKEN_UNIT
        return TierResolution(ok, None, 0.0, True, False)
    if rules.tiers is None:
        raise PowerError("rule set has neither tiers nor private")
    ordered = sorted(rules.tiers, key=lambda t: t.min_power, reverse=True)
    for i, t in enumerate(ordered):
        if allocated_power >= t.min_power * TOKEN_UNIT:
            return TierResolution(True, i, t.delay_fraction, False, False)
    last = len(ordered) - 1  # nothing matched: degrade to the lowest public tier, never block
    return TierResolution(True, last, ordered[last].delay_fraction, False, True)


def release_ms(receipt_ms: int, resolution: TierResolution, timeframe_s: int) -> int:
    """Relay receipt time + delay_fraction x timeframe, in integer milliseconds."""
    if not resolution.entitled:
        raise PowerError("a client that is not entitled has no release time")
    delay_ms = round(resolution.delay_fraction * 10_000) * timeframe_s * 1000 // 10_000
    return receipt_ms + delay_ms


def check_timeframe(rules: ProviderRuleSet, signal_timeframe: int) -> None:
    """SEP-3.40: a signal must carry the timeframe bound in the provider's rule set."""
    if signal_timeframe != rules.timeframe:
        raise PowerError(
            f"signal timeframe {signal_timeframe}s differs from the {rules.timeframe}s bound in the "
            f"rule set of provider {rules.provider[:12]}..; delays would be shortened"
        )
