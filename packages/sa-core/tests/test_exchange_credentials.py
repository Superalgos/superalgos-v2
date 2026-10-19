"""SEP-3.17: an exchange API key and secret are stored in one encrypted envelope and never shown."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from sa_core.keys import (
    SCRYPT_N_MIN,
    ExchangeCredentials,
    KeyEnvelope,
    KeyStoreError,
    NotEd25519Error,
    SAKey,
    load_exchange_credentials,
    load_sa_key,
    normalize_ed25519_secret,
    save_exchange_credentials,
    save_sa_key,
)

PASS = "correct horse battery"
CANARY_KEY = "CANARY-KEY-do-not-use-0123456789"
CANARY_SECRET = "CANARY-SECRET-do-not-use-abcdefghij"


def creds(key: str = CANARY_KEY, secret: str = CANARY_SECRET) -> ExchangeCredentials:
    return ExchangeCredentials(api_key=SecretStr(key), api_secret=SecretStr(secret))


def test_round_trip_and_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "exchange" / "binance-testnet.key.json"
    save_exchange_credentials(path, "binance-testnet", creds(), PASS, n=SCRYPT_N_MIN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    got = load_exchange_credentials(path, "binance-testnet", PASS)
    assert (
        got.api_key.get_secret_value() == CANARY_KEY
        and got.api_secret.get_secret_value() == CANARY_SECRET
    )


def test_neither_key_nor_secret_is_in_the_file(tmp_path: Path) -> None:
    path = tmp_path / "x.key.json"
    save_exchange_credentials(path, "binance-testnet", creds(), PASS, n=SCRYPT_N_MIN)
    data = path.read_bytes()
    for needle in (CANARY_KEY, CANARY_SECRET, "CANARY"):
        assert needle.encode() not in data
    assert KeyEnvelope.model_validate_json(data).public_id == "binance-testnet"


def test_repr_str_and_dump_hide_the_values() -> None:
    c = creds()
    for text in (repr(c), str(c), c.model_dump_json(), json.dumps(c.model_dump(mode="json"))):
        assert "CANARY" not in text


def test_wrong_passphrase_wrong_exchange_wrong_kind_and_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "x.key.json"
    save_exchange_credentials(path, "binance-testnet", creds(), PASS, n=SCRYPT_N_MIN)
    with pytest.raises(KeyStoreError, match="wrong passphrase"):
        load_exchange_credentials(path, "binance-testnet", "another passphrase")
    with pytest.raises(KeyStoreError, match="expected 'binance'"):
        load_exchange_credentials(path, "binance", PASS)
    with pytest.raises(KeyStoreError, match="refusing to overwrite"):
        save_exchange_credentials(path, "binance-testnet", creds(), PASS, n=SCRYPT_N_MIN)
    sa = tmp_path / "sa.json"
    save_sa_key(sa, SAKey.generate(), PASS, n=SCRYPT_N_MIN)
    with pytest.raises(KeyStoreError, match="expected 'exchange-api'"):
        load_exchange_credentials(sa, "binance-testnet", PASS)
    with pytest.raises(KeyStoreError, match="expected 'evm-sa'"):
        load_sa_key(path, PASS)


def test_a_payload_that_is_not_credentials_is_refused_without_echoing_it(tmp_path: Path) -> None:
    from sa_core.keys import _write_private, encrypt_secret

    path = tmp_path / "x.key.json"
    env = encrypt_secret(
        b'{"api_key": 1, "leak": "CANARY-PAYLOAD"}',
        "exchange-api",
        "binance-testnet",
        PASS,
        n=SCRYPT_N_MIN,
    )
    _write_private(path, env.model_dump_json())
    with pytest.raises(KeyStoreError, match="does not hold an API key and secret") as info:
        load_exchange_credentials(path, "binance-testnet", PASS)
    assert "CANARY" not in str(info.value) and info.value.__cause__ is None


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    key=st.text(min_size=1, max_size=80).filter(lambda t: t.strip() == t),
    secret=st.text(min_size=1, max_size=120).filter(lambda t: t.strip() == t),
)
def test_any_key_and_secret_round_trip_and_stay_out_of_the_file(
    tmp_path_factory: pytest.TempPathFactory, key: str, secret: str
) -> None:
    path = tmp_path_factory.mktemp("c") / "x.key.json"
    save_exchange_credentials(path, "binance-testnet", creds(key, secret), PASS, n=SCRYPT_N_MIN)
    got = load_exchange_credentials(path, "binance-testnet", PASS)
    assert (got.api_key.get_secret_value(), got.api_secret.get_secret_value()) == (key, secret)
    if len(secret) >= 12:  # shorter strings can occur in the public header by chance
        assert secret.encode() not in path.read_bytes()


# ---------------------------------------------------------------- the Binance spot secret must be an Ed25519 PEM


def _pem(key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey, protect: bool = False) -> str:
    enc = BestAvailableEncryption(b"pw") if protect else NoEncryption()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, enc).decode()


def _body(pem: str) -> str:
    return "".join(pem.strip().splitlines()[1:-1])


def test_an_ed25519_pem_is_accepted_with_or_without_its_begin_and_end_lines() -> None:
    pem = _pem(ed25519.Ed25519PrivateKey.generate())
    assert normalize_ed25519_secret(pem) == pem
    assert normalize_ed25519_secret(f"  {pem}\n\n") == pem
    assert normalize_ed25519_secret(_body(pem)) == pem
    assert normalize_ed25519_secret(pem.replace("\n", "\r\n")) == pem


@pytest.mark.parametrize(
    ("secret", "what"),
    [
        ("a1" * 32, "not a PKCS#8 PEM"),
        ("", "not a PKCS#8 PEM"),
        (_pem(rsa.generate_private_key(65537, 2048)), "RSA key"),
        (_pem(ed25519.Ed25519PrivateKey.generate(), protect=True), "encrypted PEM"),
    ],
    ids=["hmac", "empty", "rsa", "encrypted"],
)
def test_other_secrets_are_refused_with_one_line_and_no_secret(secret: str, what: str) -> None:
    with pytest.raises(NotEd25519Error) as info:
        normalize_ed25519_secret(secret)
    msg = str(info.value)
    assert what in msg and "\n" not in msg and "testnet.binance.vision" in msg
    assert not secret or (secret not in msg and (_body(secret) or secret) not in msg)


def test_a_different_key_type_in_pkcs8_is_not_ed25519() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    with pytest.raises(NotEd25519Error, match="not an Ed25519 key"):
        normalize_ed25519_secret(pem.decode())


@settings(max_examples=60, deadline=None)
@given(text=st.text(max_size=120))
def test_property_random_strings_are_refused_without_echoing_them(text: str) -> None:
    with pytest.raises(NotEd25519Error) as info:
        normalize_ed25519_secret(text)
    assert "\n" not in str(info.value)
    assert len(text.strip()) < 16 or text.strip() not in str(
        info.value
    )  # short strings match the fixed wording


@settings(max_examples=40, deadline=None)
@given(seed=st.binary(min_size=32, max_size=32), headers=st.booleans())
def test_property_generated_ed25519_keys_are_accepted(seed: bytes, headers: bool) -> None:
    pem = _pem(ed25519.Ed25519PrivateKey.from_private_bytes(seed))
    assert normalize_ed25519_secret(pem if headers else _body(pem)) == pem
