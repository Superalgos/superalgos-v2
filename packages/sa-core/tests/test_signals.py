"""Signal events: SEP-2.4/SEP-3.4 on events, SEP-3.26/SEP-3.27 private signals, SEP-3.28 commitments, SEP-3.29."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core_helpers import FAR, T0_MS, Provider
from sa_core.delegation import delegation_tag, sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import NostrEvent
from sa_core.nostr import build_event
from sa_core.signals import (
    SignalError,
    build_commitment_event,
    build_private_signal_event,
    build_signal_event,
    check_reveal,
    commitment_digest,
    open_private_signal,
    signal_from_text,
    verify_commitment_event,
    verify_signal_event,
)

NOW = T0_MS // 1000


def test_valid_signal_verifies_and_exposes_the_sa_identity() -> None:
    p = Provider()
    ev = p.publish()
    got = verify_signal_event(ev, NOW, expected_evm=p.sa.address)
    assert got.signal.seq == 1 and got.delegation.evm_address == p.sa.address
    assert got.event == ev


@given(
    field=st.sampled_from(["instrument", "side", "action", "size_fraction", "seq", "created_ms"]),
    shift=st.integers(1, 5),
)
def test_sep_3_4_altering_the_payload_after_signing_fails_verification(
    field: str, shift: int
) -> None:
    p = PROVIDER
    ev = build_signal_event(p.device, p.next_signal(), p.delegation)
    sig = p.next_signal().model_dump()
    sig[field] = {
        "instrument": "BINANCE:ETHUSDT",
        "side": "sell",
        "action": "close",
        "size_fraction": 0.99,
        "seq": 1 + shift,
        "created_ms": T0_MS + shift,
    }[field]
    if field == "seq":
        sig["prev"] = "a" * 64
    if sig == p.next_signal().model_dump():
        return
    import json

    tampered = ev.model_copy(update={"content": json.dumps(sig)})
    with pytest.raises(SignalError, match="altered"):
        verify_signal_event(tampered, NOW)


PROVIDER = Provider()


def test_sep_2_4_expired_and_wrongly_delegated_signals_fail() -> None:
    p = Provider(expires_at=NOW + 10)
    ev = p.publish()
    verify_signal_event(ev, NOW)
    with pytest.raises(SignalError, match="expired"):
        verify_signal_event(ev, NOW + 10)
    other = DeviceKey.generate()
    stolen = build_signal_event(other, p.next_signal(), p.delegation)  # tag is for another device
    with pytest.raises(SignalError, match="bad delegation signature"):
        verify_signal_event(stolen, NOW)
    with pytest.raises(SignalError, match="not the followed identity"):
        verify_signal_event(ev, NOW, expected_evm=SAKey.generate().address)


def test_signal_failure_modes_name_the_check() -> None:
    p = Provider()
    sig = p.next_signal()
    no_tag = build_event(p.device, 9317, sig.model_dump_json(), [])
    with pytest.raises(SignalError, match="exactly one"):
        verify_signal_event(no_tag, NOW)
    wrong_kind = build_event(p.device, 1, sig.model_dump_json(), [delegation_tag(p.delegation)])
    with pytest.raises(SignalError, match="expected 9317"):
        verify_signal_event(wrong_kind, NOW)
    junk = build_event(p.device, 9317, "{}", [delegation_tag(p.delegation)])
    with pytest.raises(SignalError, match="signal content invalid"):
        verify_signal_event(junk, NOW)


def test_sep_3_26_private_signal_is_decrypted_and_verified_by_the_entitled_follower() -> None:
    p = Provider()
    follower_a, follower_b = DeviceKey.generate(), DeviceKey.generate()
    sig = p.next_signal(at_ms=T0_MS)
    ev_a = build_private_signal_event(p.device, sig, p.delegation, follower_a.pubkey_hex)
    ev_b = build_private_signal_event(p.device, sig, p.delegation, follower_b.pubkey_hex)
    assert ev_a.id != ev_b.id  # one ciphertext per recipient
    for ev, follower in ((ev_a, follower_a), (ev_b, follower_b)):
        got = open_private_signal(ev, follower, NOW, expected_evm=p.sa.address)
        assert got.signal == sig


def test_sep_3_27_non_entitled_client_cannot_read_a_private_signal() -> None:
    p = Provider()
    entitled, stranger = DeviceKey.generate(), DeviceKey.generate()
    sig = p.next_signal()
    ev = build_private_signal_event(p.device, sig, p.delegation, entitled.pubkey_hex)
    assert sig.instrument not in ev.content and "BTCUSDT" not in ev.content
    with pytest.raises(SignalError, match="cannot decrypt"):
        open_private_signal(ev, stranger, NOW)
    with pytest.raises(SignalError, match="expected"):
        verify_signal_event(ev, NOW)  # not parseable as a public signal


def test_sep_3_28_commitment_digest_binds_the_plaintext() -> None:
    p = Provider()
    sig = p.next_signal()
    cev = build_commitment_event(p.device, sig, p.delegation)
    com, addr = verify_commitment_event(cev, NOW)
    assert addr == p.sa.address and com.digest == commitment_digest(sig.model_dump_json())
    assert com.seq == 1 and com.strategy_id == "s1"
    assert check_reveal(com, sig.model_dump_json()) == sig
    other = p.next_signal(side="sell")
    with pytest.raises(SignalError, match="hashes to"):
        check_reveal(com, other.model_dump_json())


def test_reveal_must_fill_the_committed_slot() -> None:
    p = Provider()
    sig = p.next_signal()
    com, _ = verify_commitment_event(build_commitment_event(p.device, sig, p.delegation), NOW)
    moved = com.model_copy(update={"seq": 2, "prev": "b" * 64})
    with pytest.raises(SignalError, match="slot"):
        check_reveal(moved.model_copy(update={"digest": com.digest}), sig.model_dump_json())
    garbage = "not json"
    with pytest.raises(SignalError, match="revealed signal invalid"):
        check_reveal(com.model_copy(update={"digest": commitment_digest(garbage)}), garbage)


def test_commitment_verification_failures() -> None:
    p = Provider()
    ev = build_commitment_event(p.device, p.next_signal(), p.delegation)
    with pytest.raises(SignalError, match="expected 9318"):
        verify_commitment_event(p.publish(), NOW)
    with pytest.raises(SignalError, match="expired"):
        verify_commitment_event(ev, FAR + 1)
    bad = build_event(p.device, 9318, "{}", [delegation_tag(p.delegation)])
    with pytest.raises(SignalError, match="commitment content invalid"):
        verify_commitment_event(bad, NOW)


def test_sep_3_29_off_relay_signal_text_gets_the_same_checks() -> None:
    p = Provider()
    ev = p.publish()
    text = ev.model_dump_json()
    assert signal_from_text(text, NOW, expected_evm=p.sa.address).signal.seq == 1
    tampered = text.replace('"side\\":\\"buy', '"side\\":\\"sell')
    assert tampered != text
    with pytest.raises(SignalError, match="altered"):
        signal_from_text(tampered, NOW)
    with pytest.raises(SignalError, match="not a Nostr event JSON"):
        signal_from_text("hello", NOW)
    other = DeviceKey.generate()  # wrong delegation: delegation was issued to another device key
    stolen = build_signal_event(other, p.next_signal(), p.delegation)
    with pytest.raises(SignalError, match="bad delegation signature"):
        signal_from_text(stolen.model_dump_json(), NOW)


@given(st.data())
def test_sep_3_29_property_wrong_delegation_or_tamper_is_rejected(data: st.DataObject) -> None:
    p = PROVIDER
    ev = build_signal_event(p.device, p.next_signal(), p.delegation)
    which = data.draw(st.sampled_from(["content", "tag-sig", "tag-addr"]))
    if which == "content":
        bad: NostrEvent = ev.model_copy(update={"content": ev.content.replace("0.5", "0.6")})
    else:
        forged = sign_delegation(SAKey.generate(), p.pubkey, FAR)
        tag = delegation_tag(p.delegation)
        new_tag = (
            ("sa-delegation", tag[1], tag[2], forged.signature)
            if which == "tag-sig"
            else delegation_tag(forged)
        )
        bad = ev.model_copy(update={"tags": (new_tag,)})
    with pytest.raises(SignalError):
        signal_from_text(bad.model_dump_json(), NOW, expected_evm=p.sa.address)


@given(expires=st.integers(NOW + 1, NOW + 10**6), lag=st.integers(0, 10**6))
def test_sep_2_4_property_signal_from_an_expired_delegation_always_fails(
    expires: int, lag: int
) -> None:
    p = Provider(expires_at=expires)
    ev = build_signal_event(p.device, p.next_signal(), p.delegation)
    verify_signal_event(ev, expires - 1)
    with pytest.raises(SignalError, match="expired"):
        verify_signal_event(ev, expires + lag)


@given(revoked_at=st.integers(0, 10**6), lag=st.integers(0, 10**6))
def test_sep_2_4_property_signal_from_a_revoked_device_key_always_fails(
    revoked_at: int, lag: int
) -> None:
    from sa_core.delegation import sign_revocation

    p = PROVIDER
    ev = build_signal_event(p.device, p.next_signal(), p.delegation)
    rev = sign_revocation(p.sa, p.pubkey, revoked_at)
    with pytest.raises(SignalError, match="revoked"):
        verify_signal_event(ev, revoked_at + lag, revocations=[rev])
