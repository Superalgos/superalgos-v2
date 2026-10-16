"""Official NIP-44 v2 test vectors.

Source: https://github.com/paulmillr/nip44/blob/main/nip44.vectors.json (the file the NIP-44
spec points to), vendored at tests/data/nip44.vectors.json, sha256
269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from sa_core.keys import DeviceKey, KeyStoreError
from sa_core.nostr import (
    NostrError,
    conversation_key,
    decrypt_with_conversation_key,
    encrypt_with_conversation_key,
    message_keys,
    nip44_decrypt,
    nip44_encrypt,
    padded_len,
)

DATA = Path(__file__).parent / "data" / "nip44.vectors.json"
SHA256 = "269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040"
V2: dict[str, Any] = json.loads(DATA.read_text())["v2"]


def test_vendored_vector_file_is_the_published_one() -> None:
    assert hashlib.sha256(DATA.read_bytes()).hexdigest() == SHA256


@pytest.mark.parametrize("v", V2["valid"]["get_conversation_key"])
def test_get_conversation_key(v: dict[str, str]) -> None:
    key = DeviceKey(bytes.fromhex(v["sec1"]))
    assert conversation_key(key, v["pub2"]).hex() == v["conversation_key"]


@pytest.mark.parametrize("v", V2["invalid"]["get_conversation_key"])
def test_invalid_conversation_key_inputs_are_refused(v: dict[str, str]) -> None:
    with pytest.raises((NostrError, KeyStoreError)):
        conversation_key(DeviceKey(bytes.fromhex(v["sec1"])), v["pub2"])


def test_get_message_keys() -> None:
    group = V2["valid"]["get_message_keys"]
    conv = bytes.fromhex(group["conversation_key"])
    for k in group["keys"]:
        chacha_key, chacha_nonce, hmac_key = message_keys(conv, bytes.fromhex(k["nonce"]))
        assert chacha_key.hex() == k["chacha_key"]
        assert chacha_nonce.hex() == k["chacha_nonce"]
        assert hmac_key.hex() == k["hmac_key"]


@pytest.mark.parametrize(("n", "padded"), V2["valid"]["calc_padded_len"])
def test_calc_padded_len(n: int, padded: int) -> None:
    assert padded_len(n) == padded


@pytest.mark.parametrize("v", V2["valid"]["encrypt_decrypt"])
def test_encrypt_decrypt(v: dict[str, str]) -> None:
    sec1, sec2 = DeviceKey(bytes.fromhex(v["sec1"])), DeviceKey(bytes.fromhex(v["sec2"]))
    nonce = bytes.fromhex(v["nonce"])
    assert conversation_key(sec1, sec2.pubkey_hex).hex() == v["conversation_key"]
    assert nip44_encrypt(v["plaintext"], sec1, sec2.pubkey_hex, nonce=nonce) == v["payload"]
    assert nip44_decrypt(v["payload"], sec2, sec1.pubkey_hex) == v["plaintext"]


@pytest.mark.parametrize("v", V2["valid"]["encrypt_decrypt_long_msg"])
def test_encrypt_decrypt_long_msg(v: dict[str, Any]) -> None:
    conv = bytes.fromhex(v["conversation_key"])
    plaintext = v["pattern"] * v["repeat"]
    assert hashlib.sha256(plaintext.encode()).hexdigest() == v["plaintext_sha256"]
    payload = encrypt_with_conversation_key(plaintext, conv, bytes.fromhex(v["nonce"]))
    assert hashlib.sha256(payload.encode()).hexdigest() == v["payload_sha256"]
    assert decrypt_with_conversation_key(payload, conv) == plaintext


def test_invalid_message_lengths_are_refused() -> None:
    conv = b"\x01" * 32
    for entry in V2["invalid"]["encrypt_msg_lengths"]:
        with pytest.raises(NostrError, match="needs 1..65535"):
            encrypt_with_conversation_key("a" * entry, conv, b"\x02" * 32)


@pytest.mark.parametrize("v", V2["invalid"]["decrypt"], ids=lambda v: v["note"])
def test_invalid_payloads_are_refused(v: dict[str, str]) -> None:
    with pytest.raises(NostrError):
        decrypt_with_conversation_key(v["payload"], bytes.fromhex(v["conversation_key"]))


def test_a_payload_with_a_flipped_mac_bit_is_refused() -> None:
    v = V2["valid"]["encrypt_decrypt"][0]
    raw = bytearray(base64.b64decode(v["payload"]))
    raw[-1] ^= 1
    with pytest.raises(NostrError, match="MAC mismatch"):
        decrypt_with_conversation_key(
            base64.b64encode(bytes(raw)).decode(), bytes.fromhex(v["conversation_key"])
        )
