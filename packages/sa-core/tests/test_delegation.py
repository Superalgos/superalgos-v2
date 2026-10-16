"""SEP-2.3 (delegation accepted within expiry), SEP-2.4 (expired or revoked always fails)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sa_core.delegation import (
    DelegationError,
    delegation_from_event,
    delegation_message,
    delegation_tag,
    parse_delegation_tag,
    sign_delegation,
    sign_revocation,
    verify_delegation,
    verify_event_delegation,
    verify_revocation,
)
from sa_core.keys import DeviceKey, SAKey
from sa_core.nostr import build_event


def test_wire_message_and_tag_match_the_contract(sa: SAKey, device: DeviceKey) -> None:
    d = sign_delegation(sa, device.pubkey_hex, 2_000_000_000)
    assert (
        delegation_message(device.pubkey_hex, 2_000_000_000)
        == f"sa-delegation:{device.pubkey_hex}:2000000000"
    )
    assert delegation_tag(d) == ("sa-delegation", sa.address, "2000000000", d.signature)
    assert parse_delegation_tag(delegation_tag(d), device.pubkey_hex) == d


def test_sep_2_3_accepted_only_within_expiry(sa: SAKey, device: DeviceKey) -> None:
    d = sign_delegation(sa, device.pubkey_hex, 1_000)
    assert verify_delegation(d, device.pubkey_hex, at=999) == sa.address
    with pytest.raises(DelegationError, match="expired at 1000"):
        verify_delegation(d, device.pubkey_hex, at=1_000)


@given(exp=st.integers(1, 10**9), delta=st.integers(0, 10**6))
def test_sep_2_4_expired_delegation_is_always_rejected(
    sa: SAKey, device: DeviceKey, exp: int, delta: int
) -> None:
    d = sign_delegation(sa, device.pubkey_hex, exp)
    with pytest.raises(DelegationError, match="expired"):
        verify_delegation(d, device.pubkey_hex, at=exp + delta)
    assert verify_delegation(d, device.pubkey_hex, at=exp - 1 if exp > 1 else 0) == sa.address


@given(effective=st.integers(0, 10**6), after=st.integers(0, 10**6))
def test_sep_2_4_revoked_delegation_is_always_rejected(
    sa: SAKey, device: DeviceKey, effective: int, after: int
) -> None:
    d = sign_delegation(sa, device.pubkey_hex, 10**9)
    r = sign_revocation(sa, device.pubkey_hex, effective)
    verify_revocation(r)
    with pytest.raises(DelegationError, match="revoked"):
        verify_delegation(d, device.pubkey_hex, at=effective + after, revocations=[r])
    if effective > 0:
        assert (
            verify_delegation(d, device.pubkey_hex, at=effective - 1, revocations=[r]) == sa.address
        )


def test_revocations_that_do_not_apply_are_ignored(sa: SAKey, device: DeviceKey) -> None:
    d = sign_delegation(sa, device.pubkey_hex, 10**9)
    stranger = SAKey.generate()
    other_device = DeviceKey.generate()
    by_stranger = sign_revocation(stranger, device.pubkey_hex, 0)  # not the delegator
    other_key = sign_revocation(sa, other_device.pubkey_hex, 0)  # another device key
    forged = sign_revocation(sa, device.pubkey_hex, 0)
    forged = forged.model_copy(
        update={"signature": by_stranger.signature, "evm_address": sa.address}
    )
    with pytest.raises(DelegationError, match="bad revocation signature"):
        verify_revocation(forged)
    assert (
        verify_delegation(d, device.pubkey_hex, 5, [by_stranger, other_key, forged]) == sa.address
    )


def test_forged_wrong_key_and_altered_expiry_are_rejected(sa: SAKey, device: DeviceKey) -> None:
    d = sign_delegation(sa, device.pubkey_hex, 5_000)
    other = DeviceKey.generate()
    with pytest.raises(DelegationError, match="is for device key"):
        verify_delegation(d, other.pubkey_hex, 1)
    with pytest.raises(DelegationError, match="bad delegation signature"):
        verify_delegation(d.model_copy(update={"expires_at": 9_000}), device.pubkey_hex, 1)
    impostor = SAKey.generate()
    with pytest.raises(DelegationError, match="bad delegation signature"):
        verify_delegation(
            d.model_copy(update={"evm_address": impostor.address}), device.pubkey_hex, 1
        )
    with pytest.raises(DelegationError, match="bad delegation signature"):
        verify_delegation(
            d.model_copy(update={"signature": "0x" + "00" * 65}), device.pubkey_hex, 1
        )


def test_tag_parsing_errors_name_the_tag(device: DeviceKey) -> None:
    with pytest.raises(DelegationError, match="expected"):
        parse_delegation_tag(("sa-delegation", "x"), device.pubkey_hex)
    with pytest.raises(DelegationError, match="malformed"):
        parse_delegation_tag(("sa-delegation", "nope", "1", "0x1"), device.pubkey_hex)
    ev = build_event(device, 1, "x")
    with pytest.raises(DelegationError, match="0 sa-delegation tags"):
        delegation_from_event(ev)


def test_event_delegation_can_pin_the_followed_identity(sa: SAKey, device: DeviceKey) -> None:
    d = sign_delegation(sa, device.pubkey_hex, 10**9)
    ev = build_event(device, 9317, "x", [delegation_tag(d)])
    assert (
        verify_event_delegation(ev, 5, expected_evm=sa.address.upper().replace("0X", "0x"))
        == sa.address
    )
    with pytest.raises(DelegationError, match="not the followed identity"):
        verify_event_delegation(ev, 5, expected_evm=SAKey.generate().address)


# ---------------------------------------------------------------- kind 9334 revocation events (M3)


def test_m3_revocation_event_round_trips_and_any_publisher_may_sign_it(
    sa: SAKey, device: DeviceKey
) -> None:
    from sa_core.delegation import (
        revocation_event,
        revocations_from_events,
        validate_revocation_event,
    )

    rev = sign_revocation(sa, device.pubkey_hex, 1_000)
    stranger = DeviceKey.generate()
    ev = revocation_event(stranger, rev, created_at=1_500)
    assert (
        ev.kind == 9334 and ev.pubkey == stranger.pubkey_hex and ("p", device.pubkey_hex) in ev.tags
    )
    assert validate_revocation_event(ev) == rev
    import json as _json

    assert _json.loads(ev.content) == {
        "v": 1,
        "device_pubkey": device.pubkey_hex,
        "effective_at": 1_000,
        "address": sa.address,
        "sig": rev.signature,
    }
    assert revocations_from_events([ev, ev]) == [rev]  # deduplicated


def test_m3_forged_wrong_kind_and_extra_field_revocations_are_ignored(
    sa: SAKey, device: DeviceKey
) -> None:
    import json as _json

    from sa_core.delegation import (
        revocation_event,
        revocations_from_events,
        validate_revocation_event,
    )

    other = SAKey.generate()
    good = sign_revocation(sa, device.pubkey_hex, 5)
    forged = good.model_copy(
        update={"signature": sign_revocation(other, device.pubkey_hex, 5).signature}
    )
    ev_forged = revocation_event(device, forged)
    with pytest.raises(DelegationError, match="bad revocation signature"):
        validate_revocation_event(ev_forged)
    wrong_kind = build_event(device, 1, "x")
    with pytest.raises(DelegationError, match="expected 9334"):
        validate_revocation_event(wrong_kind)
    content = _json.loads(revocation_event(device, good).content) | {"api_secret": "x"}
    with pytest.raises(DelegationError, match="content invalid"):
        validate_revocation_event(build_event(device, 9334, _json.dumps(content)))
    tampered = revocation_event(device, good).model_copy(update={"content": "{}"})
    with pytest.raises(DelegationError):
        validate_revocation_event(tampered)
    assert revocations_from_events([ev_forged, wrong_kind, tampered]) == []
    assert len(revocations_from_events([revocation_event(device, good)] * 3, limit=0)) == 0


def test_m3_a_stranger_cannot_revoke_someone_elses_key(sa: SAKey, device: DeviceKey) -> None:
    """The revocation must be signed by the address that signed the delegation; a different address is ignored."""
    d = sign_delegation(sa, device.pubkey_hex, 2_000)
    bystander = sign_revocation(SAKey.generate(), device.pubkey_hex, 0)
    assert verify_delegation(d, device.pubkey_hex, 10, [bystander]) == sa.address
    real = sign_revocation(sa, device.pubkey_hex, 10)
    with pytest.raises(DelegationError, match="revoked"):
        verify_delegation(d, device.pubkey_hex, 10, [real])
    assert verify_delegation(d, device.pubkey_hex, 9, [real]) == sa.address  # not yet effective
