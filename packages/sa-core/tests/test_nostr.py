"""NIP-01 events, SEP-3.4 tamper detection, NIP-44 v2."""

from __future__ import annotations

import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sa_core.keys import DeviceKey
from sa_core.models import NostrEvent
from sa_core.nostr import (
    NostrError,
    build_auth_event,
    build_event,
    compute_event_id,
    conversation_key,
    event_from_json,
    nip44_decrypt,
    nip44_encrypt,
    padded_len,
    verify_event,
)


def test_event_round_trip_and_known_id_shape(device: DeviceKey) -> None:
    ev = build_event(device, 1, "héllo ✓", [("t", "x")], created_at=1_700_000_000)
    verify_event(ev)
    assert event_from_json(ev.model_dump_json()) == ev
    assert event_from_json(ev.model_dump(mode="json")) == ev
    assert ev.id == compute_event_id(ev.pubkey, 1_700_000_000, 1, [["t", "x"]], "héllo ✓")
    assert len(ev.sig) == 128


@given(
    content=st.text(max_size=80),
    field=st.sampled_from(["content", "tags", "created_at", "kind", "pubkey", "sig", "id"]),
    junk=st.text("0123456789abcdef", min_size=1, max_size=8),
)
def test_sep_3_4_any_mutation_fails_verification(
    device: DeviceKey, content: str, field: str, junk: str
) -> None:
    ev = build_event(device, 9317, content, [("a", "b")], created_at=1_700_000_000)
    data = ev.model_dump()
    if field == "content":
        data["content"] = content + junk
    elif field == "tags":
        data["tags"] = (*data["tags"], ("x", junk))
    elif field == "created_at":
        data["created_at"] += 1
    elif field == "kind":
        data["kind"] += 1
    elif field == "pubkey":
        data["pubkey"] = DeviceKey.generate().pubkey_hex
    elif field == "sig":
        flipped = bytearray(bytes.fromhex(data["sig"]))
        flipped[0] ^= 1
        data["sig"] = bytes(flipped).hex()
    else:
        data["id"] = ("0" if data["id"][0] != "0" else "1") + data["id"][1:]
    with pytest.raises(NostrError):
        verify_event(NostrEvent.model_validate(data))


def test_verify_errors_name_the_problem(device: DeviceKey) -> None:
    ev = build_event(device, 1, "a")
    with pytest.raises(NostrError, match="altered"):
        verify_event(ev.model_copy(update={"content": "b"}))
    other = DeviceKey.generate()
    forged = build_event(other, 1, "a", created_at=ev.created_at)
    with pytest.raises(NostrError, match="not valid for pubkey"):
        verify_event(forged.model_copy(update={"sig": ev.sig, "id": forged.id}))
    with pytest.raises(NostrError, match="not a NIP-01 event"):
        event_from_json("{}")


def test_auth_event_carries_relay_challenge_and_extra_tags(device: DeviceKey) -> None:
    ev = build_auth_event(device, "ws://r", "chal", [("sa-delegation", "0xa", "1", "0xs")])
    verify_event(ev)
    assert ev.kind == 22242
    assert ("relay", "ws://r") in ev.tags and ("challenge", "chal") in ev.tags
    assert ev.tag_values("sa-delegation")


@pytest.mark.parametrize(
    ("n", "padded"),
    [(1, 32), (16, 32), (32, 32), (33, 64), (64, 64), (65, 96), (100, 128), (200, 224),
     (256, 256), (257, 320), (300, 320), (500, 512), (515, 640), (65535, 65536)],
)  # fmt: skip
def test_nip44_padding_schedule(n: int, padded: int) -> None:
    assert padded_len(n) == padded


def test_nip44_conversation_key_matches_spec_vector() -> None:
    a = DeviceKey((1).to_bytes(32, "big"))
    b = DeviceKey((2).to_bytes(32, "big"))
    expected = "c41c775356fd92eadc63ff5a0dc1da211b268cbea22316767095b2871ea1412d"
    assert conversation_key(a, b.pubkey_hex).hex() == expected
    assert conversation_key(b, a.pubkey_hex).hex() == expected


@given(text=st.text(min_size=1, max_size=300).filter(lambda s: len(s.encode()) <= 65535))
def test_nip44_round_trip_between_two_parties(text: str) -> None:
    a, b = DeviceKey.generate(), DeviceKey.generate()
    payload = nip44_encrypt(text, a, b.pubkey_hex)
    assert nip44_decrypt(payload, b, a.pubkey_hex) == text
    assert nip44_decrypt(payload, a, b.pubkey_hex) == text  # symmetric conversation key


def test_nip44_rejects_wrong_key_tampering_and_bad_input(device: DeviceKey) -> None:
    other, stranger = DeviceKey.generate(), DeviceKey.generate()
    payload = nip44_encrypt("secret signal", device, other.pubkey_hex)
    assert "secret" not in base64.b64decode(payload).decode("latin-1")
    with pytest.raises(NostrError, match="MAC mismatch"):
        nip44_decrypt(payload, stranger, device.pubkey_hex)
    raw = bytearray(base64.b64decode(payload))
    raw[40] ^= 1
    with pytest.raises(NostrError, match="MAC mismatch"):
        nip44_decrypt(base64.b64encode(bytes(raw)).decode(), other, device.pubkey_hex)
    with pytest.raises(NostrError, match="unsupported NIP-44 payload"):
        nip44_decrypt("#future", other, device.pubkey_hex)
    with pytest.raises(NostrError, match="invalid length"):
        nip44_decrypt("AAAA", other, device.pubkey_hex)
    with pytest.raises(NostrError, match="not base64"):
        nip44_decrypt("!" * 140, other, device.pubkey_hex)
    v1 = bytearray(base64.b64decode(payload))
    v1[0] = 1
    with pytest.raises(NostrError, match="unsupported NIP-44 version 1"):
        nip44_decrypt(base64.b64encode(bytes(v1)).decode(), other, device.pubkey_hex)
    with pytest.raises(NostrError, match="needs 1..65535"):
        nip44_encrypt("", device, other.pubkey_hex)
    with pytest.raises(NostrError, match="conversation key"):
        nip44_encrypt("x", device, "00" * 32)


def test_nip44_padding_and_length_are_validated(device: DeviceKey) -> None:
    from Crypto.Cipher import ChaCha20

    from sa_core.nostr import _mac, message_keys

    other = DeviceKey.generate()
    nonce = b"\x07" * 32
    ck, cn, hk = message_keys(conversation_key(device, other.pubkey_hex), nonce)
    # a validly MAC'd message whose padding is wrong must still be refused
    bad_plain = (5).to_bytes(2, "big") + b"hello" + b"\x00" * 59  # padded to 66; expected 34
    ct = ChaCha20.new(key=ck, nonce=cn).encrypt(bad_plain)
    payload = base64.b64encode(bytes([2]) + nonce + ct + _mac(hk, ct, nonce)).decode()
    with pytest.raises(NostrError, match="padding is invalid"):
        nip44_decrypt(payload, other, device.pubkey_hex)
