"""Allocations (SEP-3.32, SEP-3.33, SEP-3.34, SEP-3.35), provider power and tiers (SEP-3.21, SEP-3.42, SEP-3.40, SEP-3.22, SEP-3.23)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from sa_core.delegation import delegation_tag, sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import (
    TOKEN_UNIT,
    NostrEvent,
    PowerAllocation,
    PrivateRule,
    ProviderRuleSet,
    Tier,
)
from sa_core.nostr import build_event
from sa_core.power import (
    DEFAULT_DELAY_D_S,
    DEFAULT_TIERS,
    PowerError,
    TimedAllocation,
    allocation_event,
    allocation_in_force,
    allocation_message,
    check_timeframe,
    effective_at,
    governance_power,
    is_newer,
    provider_power,
    release_ms,
    resolve_tier,
    ruleset_event,
    ruleset_message,
    scale_power,
    sign_allocation,
    sign_ruleset,
    validate_allocation_event,
    validate_ruleset_event,
)

HEX = "0123456789abcdef"
PK = st.text(HEX, min_size=64, max_size=64)
SA1 = SAKey.generate()
DEV = DeviceKey.generate()
DELEG1 = sign_delegation(SA1, DEV.pubkey_hex, 10**10)  # SA1 delegates DEV
A, B = "a" * 64, "b" * 64
MILLION = 1_000_000 * TOKEN_UNIT


def _rules(
    sa: SAKey = SA1,
    provider: str = A,
    tiers: tuple[Tier, ...] = DEFAULT_TIERS,
    timeframe: int = 3600,
) -> ProviderRuleSet:
    return sign_ruleset(sa, provider, timeframe, tiers=tiers)


# ---------------------------------------------------------------- allocation validity


def test_allocation_message_is_the_documented_wire_text() -> None:
    msg = allocation_message("0x" + "AB" * 20, 3, 7000, {B: 2000, A: 1000})
    assert msg == f"sa-allocation:0x{'ab' * 20}:3:7000:{A}=1000,{B}=2000"
    assert allocation_message("0x" + "ab" * 20, 0, 0, {}).endswith(":0:0:")


def test_valid_allocation_event_round_trips() -> None:
    alloc = sign_allocation(SA1, 1, 6000, {A: 4000})
    ev = allocation_event(DEV, alloc, DELEG1, created_at=100)
    assert validate_allocation_event(ev) == alloc
    assert ev.kind == 9332 and ("p", A) in ev.tags


@given(st.data())
def test_sep_3_32_allocation_not_signed_by_its_address_is_rejected(data: st.DataObject) -> None:
    other = SAKey.generate()
    good = sign_allocation(SA1, 1, 4000, {A: 5000})
    claim = data.draw(st.sampled_from(["foreign-sig", "altered-split", "altered-nonce"]))
    if claim == "foreign-sig":  # the allocation claims SA1 but another key signed it
        forged = good.model_copy(update={"sig": sign_allocation(other, 1, 4000, {A: 5000}).sig})
    elif claim == "altered-split":
        forged = good.model_copy(update={"providers": {A: 5001}})
    else:
        forged = good.model_copy(update={"nonce": 2})
    ev = build_event(DEV, 9332, forged.model_dump_json(), [])
    with pytest.raises(PowerError, match="not signed by its SA address"):
        validate_allocation_event(ev)


@given(old=st.integers(0, 10**6), new=st.integers(0, 10**6))
def test_sep_3_32_only_a_higher_nonce_replaces_the_current_allocation(old: int, new: int) -> None:
    a = sign_allocation(SA1, old, 100, {})
    b = sign_allocation(SA1, new, 200, {})
    assert is_newer(a, b) == (new > old)
    assert is_newer(None, b)


def test_sep_3_32_allocation_of_another_identity_cannot_replace() -> None:
    with pytest.raises(PowerError, match="cannot replace"):
        is_newer(sign_allocation(SA1, 1), sign_allocation(SAKey.generate(), 2))


@given(
    gov=st.integers(0, 10_000),
    providers=st.dictionaries(PK, st.integers(1, 10_000), min_size=1, max_size=5),
)
def test_sep_3_33_allocations_over_100_percent_are_rejected_everywhere(
    gov: int, providers: dict[str, int]
) -> None:
    total = gov + sum(providers.values())
    if total <= 10_000:
        assert sign_allocation(SA1, 1, gov, providers).total_bps() == total
        return
    with pytest.raises(ValidationError, match="more than 10000"):
        sign_allocation(SA1, 1, gov, providers)
    # a hand-forged event (valid signature, raw JSON that skips the model) is rejected on receipt
    msg = allocation_message(SA1.address, 1, gov, providers)
    import json

    raw = json.dumps(
        {
            "v": 1,
            "address": SA1.address,
            "nonce": 1,
            "governance": gov,
            "providers": providers,
            "sig": SA1.sign_message(msg),
        }
    )
    with pytest.raises(PowerError, match="allocation content invalid"):
        validate_allocation_event(build_event(DEV, 9332, raw, []))


@given(
    power=st.integers(0, 10**9 * TOKEN_UNIT),
    gov=st.integers(0, 10_000),
    providers=st.dictionaries(PK, st.integers(1, 10_000), max_size=4),
)
def test_sep_3_34_a_unit_of_power_never_counts_for_governance_and_signals(
    power: int, gov: int, providers: dict[str, int]
) -> None:
    if gov + sum(providers.values()) > 10_000:
        return
    alloc = sign_allocation(SA1, 1, gov, providers)
    used = governance_power(alloc, power) + sum(provider_power(alloc, p, power) for p in providers)
    assert used <= power


def test_sep_3_22_tier_comes_from_power_allocated_to_that_provider() -> None:
    """1M SA: 5% to provider A, 95% to B -> T2 with A, T0 with B (default table)."""
    alloc = sign_allocation(SA1, 1, 0, {A: 500, B: 9500})
    ta = resolve_tier(_rules(provider=A), provider_power(alloc, A, MILLION))
    tb = resolve_tier(_rules(provider=B), provider_power(alloc, B, MILLION))
    assert (ta.tier_index, ta.delay_fraction) == (2, 0.5)
    assert (tb.tier_index, tb.delay_fraction) == (0, 0.0)
    assert provider_power(alloc, "c" * 64, MILLION) == 0
    assert provider_power(None, A, MILLION) == 0


@given(
    total=st.integers(0, 10**7).map(lambda t: t * TOKEN_UNIT),
    a_bps=st.integers(0, 5000),
    other=st.integers(0, 5000),
    gov=st.integers(0, 0),
)
def test_sep_3_22_property_other_shares_and_balance_do_not_move_the_tier(
    total: int, a_bps: int, other: int, gov: int
) -> None:
    def tier(oth: int) -> object:
        providers = {k: v for k, v in {A: a_bps, B: oth}.items() if v > 0}
        alloc = sign_allocation(SA1, 1, gov, providers)
        return resolve_tier(_rules(), provider_power(alloc, A, total))

    assert tier(other) == tier(0) == tier(5000)


def test_scale_power_bounds() -> None:
    assert scale_power(10**20, 2500) == 25 * 10**18
    for bad in (-1, 10_001):
        with pytest.raises(PowerError, match="within 0..10000"):
            scale_power(1, bad)


# ---------------------------------------------------------------- D delay (SEP-3.35)


def test_effective_time_and_unanchored_never_counts() -> None:
    assert effective_at(1000, DEFAULT_DELAY_D_S) == 1000 + 7 * 86400
    assert effective_at(None, 5) is None
    alloc = sign_allocation(SA1, 1, 10_000)
    assert allocation_in_force([TimedAllocation(alloc, "e1", None)], at=10**12) is None


@given(
    anchored=st.integers(0, 10**6),
    d=st.integers(0, 10**6),
    probe=st.integers(0, 3 * 10**6),
)
def test_sep_3_35_allocation_takes_effect_only_d_after_its_anchored_time(
    anchored: int, d: int, probe: int
) -> None:
    old = sign_allocation(SA1, 1, 10_000)
    new = sign_allocation(SA1, 2, 0, {A: 10_000})
    hist = [TimedAllocation(old, "old", 0), TimedAllocation(new, "new", anchored)]
    in_force = allocation_in_force(hist, probe, d)
    if probe < anchored + d:
        assert in_force == (old if probe >= d else None)  # the old one, once its own D elapsed
    else:
        assert in_force == new


def test_equivocation_at_equal_nonce_resolves_to_smallest_event_id() -> None:
    a = sign_allocation(SA1, 1, 100)
    b = sign_allocation(SA1, 1, 200)
    hist = [TimedAllocation(a, "bb", 0), TimedAllocation(b, "aa", 0)]
    assert allocation_in_force(hist, 0, 0) == b
    assert allocation_in_force(list(reversed(hist)), 0, 0) == b


# ---------------------------------------------------------------- rule sets (SEP-3.24)


def _event(rules, sa: SAKey = SA1, device: DeviceKey = DEV):  # type: ignore[no-untyped-def]
    d = sign_delegation(sa, device.pubkey_hex, 10**10)
    return ruleset_event(device, rules, d)


def test_sep_3_24_rule_set_signed_via_the_provider_sa_identity_is_accepted() -> None:
    rules = sign_ruleset(SA1, DEV.pubkey_hex, 3600, tiers=DEFAULT_TIERS)
    assert validate_ruleset_event(_event(rules), at=1) == rules
    priv = sign_ruleset(SA1, DEV.pubkey_hex, 60, private=PrivateRule(min_power=10))
    assert validate_ruleset_event(_event(priv), at=1) == priv
    assert ruleset_message(DEV.pubkey_hex, 60, None, PrivateRule(min_power=10)).endswith(
        ":60:private@10"
    )
    assert ruleset_message("p", 60, DEFAULT_TIERS, None).endswith(
        ":0@2.0000,10000@0.5000,100000@0.1000,500000@0.0000"
    )


@given(
    st.sampled_from(
        ["other-sa-sig", "altered-tier", "wrong-sa-claim", "foreign-publisher", "bad-d", "expired"]
    )
)
def test_sep_3_24_property_rule_sets_not_signed_via_the_sa_identity_are_rejected(case: str) -> None:
    other = SAKey.generate()
    rules = sign_ruleset(SA1, DEV.pubkey_hex, 3600, tiers=DEFAULT_TIERS)
    at = 1
    if case == "other-sa-sig":
        bad = rules.model_copy(
            update={"sig": sign_ruleset(other, DEV.pubkey_hex, 3600, tiers=DEFAULT_TIERS).sig}
        )
        ev = _event(bad)
    elif case == "altered-tier":
        tiers = (Tier(min_power=0, delay_fraction=0.0),)
        ev = _event(rules.model_copy(update={"tiers": tiers}))  # delays removed after signing
    elif case == "wrong-sa-claim":
        ev = _event(rules.model_copy(update={"sa_address": other.address}))
    elif case == "foreign-publisher":
        thief = DeviceKey.generate()
        ev = _event(rules, device=thief)  # pubkey != rules.provider
    elif case == "bad-d":
        d = sign_delegation(SA1, DEV.pubkey_hex, 10**10)
        ev = build_event(DEV, 37318, rules.model_dump_json(), [("d", B), delegation_tag(d)])
    else:
        ev = _event(rules)
        at = 10**10
    with pytest.raises(PowerError):
        validate_ruleset_event(ev, at=at)


def test_rule_set_event_failure_messages() -> None:
    rules = sign_ruleset(SA1, DEV.pubkey_hex, 3600, tiers=DEFAULT_TIERS)
    d = sign_delegation(SA1, DEV.pubkey_hex, 10**10)
    with pytest.raises(PowerError, match="expected 37318"):
        validate_ruleset_event(
            build_event(
                DEV, 1, rules.model_dump_json(), [("d", DEV.pubkey_hex), delegation_tag(d)]
            ),
            1,
        )
    with pytest.raises(PowerError, match="rule set content invalid"):
        validate_ruleset_event(
            build_event(DEV, 37318, "{}", [("d", DEV.pubkey_hex), delegation_tag(d)]), 1
        )
    with pytest.raises(PowerError, match="delegation is from"):
        wrong = SAKey.generate()
        validate_ruleset_event(_event(rules, sa=wrong), 1)
    with pytest.raises(PowerError, match="altered"):
        validate_ruleset_event(_event(rules).model_copy(update={"content": "x"}), 1)


def test_sep_3_40_timeframe_is_bound_by_the_rule_set() -> None:
    rules = _rules(timeframe=3600)
    check_timeframe(rules, 3600)
    with pytest.raises(PowerError, match="shortened"):
        check_timeframe(rules, 60)


# ---------------------------------------------------------------- tier resolution


@given(
    thresholds=st.lists(st.integers(0, 10**6), min_size=1, max_size=6, unique=True),
    p1=st.integers(0, 10**6),
    p2=st.integers(0, 10**6),
)
def test_sep_3_21_more_allocated_power_never_waits_longer(
    thresholds: list[int], p1: int, p2: int
) -> None:
    ordered = sorted(thresholds)
    tiers = tuple(
        Tier(min_power=t, delay_fraction=round(2.0 / (i + 1), 4)) for i, t in enumerate(ordered)
    )
    rules = _rules(tiers=tiers)
    lo, hi = sorted((p1, p2))
    r_lo, r_hi = resolve_tier(rules, lo * TOKEN_UNIT), resolve_tier(rules, hi * TOKEN_UNIT)
    assert r_hi.delay_fraction <= r_lo.delay_fraction
    assert r_hi.tier_index is not None and r_lo.tier_index is not None
    assert r_hi.tier_index <= r_lo.tier_index  # 0 is the best tier


def test_sep_3_42_zero_power_degrades_instead_of_blocking() -> None:
    pay_to_play = _rules(
        tiers=(Tier(min_power=100, delay_fraction=1.0), Tier(min_power=10, delay_fraction=3.0))
    )
    r = resolve_tier(pay_to_play, 0)
    assert r.entitled and r.fallback and r.tier_index == 1 and r.delay_fraction == 3.0
    full = resolve_tier(_rules(), 0)
    assert full.entitled and not full.fallback and full.delay_fraction == 2.0


def test_private_provider_gives_nothing_below_its_threshold() -> None:
    priv = sign_ruleset(SA1, A, 60, private=PrivateRule(min_power=5))
    assert not resolve_tier(priv, 4 * TOKEN_UNIT).entitled
    ok = resolve_tier(priv, 5 * TOKEN_UNIT)
    assert ok.entitled and ok.private and ok.delay_fraction == 0.0
    with pytest.raises(PowerError, match="not entitled"):
        release_ms(0, resolve_tier(priv, 0), 60)
    with pytest.raises(PowerError, match="negative"):
        resolve_tier(priv, -1)


@given(receipt=st.integers(0, 10**13), tf=st.integers(1, 10**6), frac=st.integers(0, 40_000))
def test_release_time_is_receipt_plus_delay_fraction_times_timeframe(
    receipt: int, tf: int, frac: int
) -> None:
    rules = _rules(tiers=(Tier(min_power=0, delay_fraction=frac / 10_000),), timeframe=tf)
    res = resolve_tier(rules, 0)
    assert release_ms(receipt, res, tf) == receipt + frac * tf * 1000 // 10_000


# ---------------------------------------------------------------- security review H1: 9332 needs a delegation


def _alloc_event(
    publisher: DeviceKey, alloc: PowerAllocation, tags: list[tuple[str, ...]], at: int = 100
) -> NostrEvent:
    return build_event(publisher, 9332, alloc.model_dump_json(), tags, at)


def test_h1_a_stranger_rewrapping_a_valid_allocation_is_rejected() -> None:
    """Valid EIP-191 content under a device key that no delegation links to the allocating address."""
    alloc = sign_allocation(SA1, 1, 4000, {A: 6000})
    stranger = DeviceKey.generate()
    with pytest.raises(PowerError, match="without a valid delegation"):
        validate_allocation_event(_alloc_event(stranger, alloc, [("p", A)]))


def test_h1_a_missing_or_foreign_sa_delegation_tag_is_rejected() -> None:
    alloc = sign_allocation(SA1, 1, 4000, {A: 6000})
    mine = sign_delegation(SA1, DEV.pubkey_hex, 10**10)
    foreign = sign_delegation(SAKey.generate(), DEV.pubkey_hex, 10**10)  # valid tag, other address
    stranger_dev = DeviceKey.generate()
    ok = [("p", A), delegation_tag(mine)]
    assert validate_allocation_event(_alloc_event(DEV, alloc, ok)) == alloc
    cases: list[tuple[list[tuple[str, ...]], str]] = [
        ([("p", A)], "has 0 sa-delegation tags"),
        ([("p", A), delegation_tag(foreign)], "is not the followed identity"),
        ([("p", A), delegation_tag(mine), delegation_tag(foreign)], "has 2 sa-delegation tags"),
        # SA1's tag, but made for a different device key than the publisher
        (
            [("p", A), delegation_tag(sign_delegation(SA1, stranger_dev.pubkey_hex, 10**10))],
            "device key",
        ),
        (
            [("p", A), ("sa-delegation", SA1.address, "10000000000", "0x" + "00" * 65)],
            "bad delegation signature",
        ),
    ]
    for tags, why in cases:
        with pytest.raises(PowerError, match=why):
            validate_allocation_event(_alloc_event(DEV, alloc, tags))


def test_h1_the_delegation_must_be_live_and_unrevoked_when_the_allocation_was_published() -> None:
    from sa_core.delegation import sign_revocation

    alloc = sign_allocation(SA1, 1, 0, {A: 1000})
    short = sign_delegation(SA1, DEV.pubkey_hex, 150)
    tags = [("p", A), delegation_tag(short)]
    assert validate_allocation_event(_alloc_event(DEV, alloc, tags, at=149)) == alloc
    with pytest.raises(PowerError, match="expired"):
        validate_allocation_event(_alloc_event(DEV, alloc, tags, at=150))
    rev = sign_revocation(SA1, DEV.pubkey_hex, 120)
    assert validate_allocation_event(_alloc_event(DEV, alloc, tags, at=119), [rev]) == alloc
    with pytest.raises(PowerError, match="revoked"):
        validate_allocation_event(_alloc_event(DEV, alloc, tags, at=120), [rev])
    # a caller that knows a better time (the anchored one) can judge at that instant instead
    with pytest.raises(PowerError, match="revoked"):
        validate_allocation_event(_alloc_event(DEV, alloc, tags, at=100), [rev], at=130)


def test_h1_allocation_event_refuses_a_delegation_that_does_not_match() -> None:
    alloc = sign_allocation(SA1, 1, 0, {})
    with pytest.raises(
        PowerError, match="use the delegation this SA address signed for this device key"
    ):
        allocation_event(DEV, alloc, sign_delegation(SAKey.generate(), DEV.pubkey_hex, 10**10))
    with pytest.raises(PowerError, match="use the delegation"):
        allocation_event(DEV, alloc, sign_delegation(SA1, DeviceKey.generate().pubkey_hex, 10**10))
