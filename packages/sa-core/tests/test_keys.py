"""SEP-1.11: a private key or mnemonic is never written to disk unencrypted."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from sa_core.keys import (
    SCRYPT_N_MIN,
    DeviceKey,
    KeyEnvelope,
    KeyStoreError,
    SAKey,
    decrypt_secret,
    encrypt_secret,
    load_device_key,
    load_sa_key,
    save_device_key,
    save_sa_key,
)

FAST = SCRYPT_N_MIN  # scrypt cost is irrelevant to these tests
PASS = "correct horse battery"


def _all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_sep_1_11_device_key_file_holds_no_plaintext_secret(tmp_path: Path) -> None:
    key = DeviceKey.generate()
    path = tmp_path / "keys" / "device.json"
    save_device_key(path, key, PASS, n=FAST)
    data = _all_bytes(tmp_path)
    secret = key.secret_bytes()
    assert secret not in data
    assert secret.hex().encode() not in data
    assert secret.hex().upper().encode() not in data
    assert PASS.encode() not in data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.rglob("*.tmp"))
    loaded = load_device_key(path, PASS)
    assert loaded.pubkey_hex == key.pubkey_hex
    assert loaded.secret_bytes() == secret


def test_sep_1_11_sa_key_file_holds_no_plaintext_secret(tmp_path: Path) -> None:
    key = SAKey.generate()
    path = tmp_path / "sa.json"
    save_sa_key(path, key, PASS, n=FAST)
    data = path.read_bytes()
    assert key.secret_bytes() not in data
    assert key.secret_bytes().hex().encode() not in data
    assert load_sa_key(path, PASS).address == key.address


def test_wrong_passphrase_and_tampering_are_rejected(tmp_path: Path) -> None:
    key = DeviceKey.generate()
    path = tmp_path / "d.json"
    save_device_key(path, key, PASS, n=FAST)
    with pytest.raises(KeyStoreError, match="wrong passphrase"):
        load_device_key(path, "another passphrase")
    doc = json.loads(path.read_text())
    doc["public_id"] = "f" * 64  # header is authenticated data
    path.write_text(json.dumps(doc))
    with pytest.raises(KeyStoreError, match="wrong passphrase, or the key file was modified"):
        load_device_key(path, PASS)


def test_guards_on_save_and_load(tmp_path: Path) -> None:
    key = DeviceKey.generate()
    path = tmp_path / "d.json"
    with pytest.raises(KeyStoreError, match="at least 8"):
        save_device_key(path, key, "short", n=FAST)
    with pytest.raises(KeyStoreError, match="scrypt n=4"):
        save_device_key(path, key, PASS, n=4)
    save_device_key(path, key, PASS, n=FAST)
    with pytest.raises(KeyStoreError, match="refusing to overwrite"):
        save_device_key(path, key, PASS, n=FAST)
    with pytest.raises(KeyStoreError, match="holds a 'nostr-device' key, expected 'evm-sa'"):
        load_sa_key(path, PASS)
    with pytest.raises(KeyStoreError, match="not found"):
        load_device_key(tmp_path / "missing.json", PASS)
    (tmp_path / "junk.json").write_text("{}")
    with pytest.raises(KeyStoreError, match="not a Superalgos key file"):
        load_device_key(tmp_path / "junk.json", PASS)


def test_weakened_or_abusive_kdf_cost_is_refused() -> None:
    env = encrypt_secret(b"\x01" * 32, "nostr-device", "x", PASS, n=FAST)
    for n in (2, 2**30):
        bad = KeyEnvelope.model_validate(env.model_dump() | {"n": n})
        with pytest.raises(KeyStoreError, match="scrypt"):
            decrypt_secret(bad, PASS)
    corrupt = KeyEnvelope.model_validate(env.model_dump() | {"salt": "!!"})
    with pytest.raises(KeyStoreError, match="invalid base64"):
        decrypt_secret(corrupt, PASS)
    short = KeyEnvelope.model_validate(env.model_dump() | {"ciphertext": "AAAA"})
    with pytest.raises(KeyStoreError, match="wrong length"):
        decrypt_secret(short, PASS)


def test_key_objects_never_show_secrets() -> None:
    d, s = DeviceKey.generate(), SAKey.generate()
    assert d.secret_bytes().hex() not in repr(d)
    assert s.secret_bytes().hex() not in repr(s)
    with pytest.raises(KeyStoreError, match="not valid 32-byte hex"):
        DeviceKey.from_hex("zz")
    assert DeviceKey.from_hex(d.secret_bytes().hex()).pubkey_hex == d.pubkey_hex
    with pytest.raises(KeyStoreError, match="32 bytes"):
        SAKey(b"short")
    assert s.sign_message("hi").startswith("0x")


def test_l3_a_crafted_key_file_cannot_make_load_burn_memory_or_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n and r each pass their own bound, but n*r drives scrypt's memory (128*n*r bytes) and n*r*p its time."""
    path = tmp_path / "device.json"
    save_device_key(path, DeviceKey.generate(), PASS, n=FAST)
    doc = json.loads(path.read_text())
    ran: list[tuple[int, int, int]] = []

    def spy(*_a: object, **kw: int) -> bytes:
        ran.append((kw["n"], kw["r"], kw["p"]))
        raise AssertionError("scrypt must not run for a file that asks for this much")

    monkeypatch.setattr("sa_core.keys.hashlib.scrypt", spy)
    for n, r, p in ((2**20, 64, 1), (2**20, 8, 16), (2**17, 64, 16), (2**20, 16, 1)):
        path.write_text(json.dumps(doc | {"n": n, "r": r, "p": p}))
        with pytest.raises(KeyStoreError, match=rf"n={n}, r={r}, p={p}.*limits"):
            load_device_key(path, PASS)
    assert ran == []


def test_l3_the_default_cost_and_the_documented_maximum_still_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sa_core.keys import SCRYPT_N_DEFAULT, SCRYPT_NR_MAX

    assert SCRYPT_N_DEFAULT * 8 <= SCRYPT_NR_MAX  # the default (n=2**17, r=8) is inside the bound
    env = encrypt_secret(b"\x02" * 32, "nostr-device", "x", PASS, n=FAST)
    seen: list[int] = []
    real = decrypt_secret

    def spy(*_a: object, **kw: int) -> bytes:
        seen.append(kw["n"] * kw["r"])
        raise ValueError("stop before doing the work")

    monkeypatch.setattr("sa_core.keys.hashlib.scrypt", spy)
    big = KeyEnvelope.model_validate(env.model_dump() | {"n": 2**17, "r": 16, "p": 1})
    with pytest.raises(ValueError, match="stop before"):
        real(big, PASS)
    assert seen == [2**21]
