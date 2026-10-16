"""BIP-340 test vectors against the libsecp256k1 binding (coincurve), and DeviceKey/ECDH behaviour."""

from __future__ import annotations

import pytest
from coincurve import PrivateKey
from hypothesis import given
from hypothesis import strategies as st

from sa_core.keys import DeviceKey, KeyStoreError
from sa_core.nostr import schnorr_verify

N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

VECTORS = [
    # (secret, pubkey, aux, msg, signature) from bip-0340/test-vectors.csv
    (
        "0000000000000000000000000000000000000000000000000000000000000003",
        "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0000000000000000000000000000000000000000000000000000000000000000",
        "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0",
    ),
    (
        "B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "0000000000000000000000000000000000000000000000000000000000000001",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A",
    ),
]


@pytest.mark.parametrize(("sk", "pk", "aux", "msg", "sig"), VECTORS)
def test_bip340_vectors(sk: str, pk: str, aux: str, msg: str, sig: str) -> None:
    secret, aux_b, msg_b = bytes.fromhex(sk), bytes.fromhex(aux), bytes.fromhex(msg)
    assert DeviceKey(secret).pubkey_hex.upper() == pk
    assert PrivateKey(secret).sign_schnorr(msg_b, aux_b).hex().upper() == sig
    assert schnorr_verify(msg_b, bytes.fromhex(pk), bytes.fromhex(sig))


def test_device_key_sign_verifies_with_random_aux() -> None:
    key = DeviceKey.generate()
    msg = b"\x11" * 32
    assert schnorr_verify(msg, bytes.fromhex(key.pubkey_hex), key.sign(msg))


def test_verify_rejects_malformed_input() -> None:
    key = DeviceKey((7).to_bytes(32, "big"))
    pub = bytes.fromhex(key.pubkey_hex)
    msg = b"\x01" * 32
    sig = key.sign(msg)
    assert schnorr_verify(msg, pub, sig)
    assert not schnorr_verify(msg, pub, sig[:-1])
    assert not schnorr_verify(msg[:-1], pub, sig)
    assert not schnorr_verify(msg, b"\xff" * 32, sig)  # x out of the field
    assert not schnorr_verify(msg, pub, b"\xff" * 64)  # r, s out of range
    assert not schnorr_verify(b"\x00" * 32, pub, sig)


def test_secret_range_and_lengths() -> None:
    for bad in (b"\x00" * 32, N.to_bytes(32, "big"), b"\x01" * 31):
        with pytest.raises(KeyStoreError, match="device secret"):
            DeviceKey(bad)
    key = DeviceKey.generate()
    with pytest.raises(KeyStoreError, match="32-byte digest"):
        key.sign(b"x")
    with pytest.raises(KeyStoreError, match="peer public key"):
        key.ecdh("01" * 31)
    with pytest.raises(KeyStoreError, match="peer public key"):
        key.ecdh("ff" * 32)
    with pytest.raises(KeyStoreError, match="peer public key"):
        key.ecdh("zz")


@given(a=st.integers(1, N - 1), b=st.integers(1, N - 1))
def test_ecdh_is_symmetric(a: int, b: int) -> None:
    ka, kb = DeviceKey(a.to_bytes(32, "big")), DeviceKey(b.to_bytes(32, "big"))
    assert ka.ecdh(kb.pubkey_hex) == kb.ecdh(ka.pubkey_hex)
