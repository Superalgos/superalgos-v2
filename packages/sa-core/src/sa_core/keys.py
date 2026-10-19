"""Device keys (Nostr secp256k1 / BIP-340) and SA keys (EVM), encrypted at rest (SEP-1.11).

On disk a key is only ever a JSON envelope: scrypt(passphrase, salt) derives a 32-byte key, and
ChaCha20-Poly1305 encrypts the 32 secret bytes under it, with the envelope header as associated data, so
editing the header (KDF cost, kind, public id) breaks decryption. The plaintext secret exists
only in memory, inside `DeviceKey` / `SAKey`.

The SA key is the root identity and "never a hot key" (design principle): this module lets a user
keep it in an encrypted file, but the running system only needs the SA key to sign delegations,
allocations and rule sets, never to sign signals.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Literal

from coincurve import PrivateKey, PublicKey
from Crypto.Cipher import ChaCha20_Poly1305
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_der_private_key,
)
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel, ConfigDict, Field, SecretStr

SCRYPT_N_DEFAULT = 2**17  # ~128 MiB, ~0.3 s: OWASP-class cost for interactive unlock
SCRYPT_N_MIN = 2**10  # refuse to load a file whose KDF cost was weakened
SCRYPT_N_MAX = 2**20  # refuse a file that would exhaust memory on load (1 GiB at r=8)
SCRYPT_NR_MAX = (
    2**21
)  # n * r bounds scrypt memory (128 * n * r bytes: 256 MiB); the default is 2**17 * 8 = 2**20
SCRYPT_NRP_MAX = 2**22  # n * r * p bounds scrypt CPU time; the default is 2**20
MIN_PASSPHRASE_CHARS = 8


class KeyStoreError(Exception):
    """A key file could not be created, read or decrypted. The message says what to do."""


class DeviceKey:
    """Nostr signing key. Holds the secret in memory only; `repr` never shows it."""

    __slots__ = ("_secret", "pubkey_hex")

    def __init__(self, secret: bytes) -> None:
        self._secret = bytes(secret)
        try:
            if len(self._secret) != 32:
                raise ValueError("wrong length")
            self.pubkey_hex = PrivateKey(self._secret).public_key_xonly.format().hex()
        except ValueError as exc:
            raise KeyStoreError(
                f"device secret must be 32 bytes in 1..n-1 (got {len(self._secret)} bytes): {exc}"
            ) from exc

    @classmethod
    def generate(cls) -> DeviceKey:
        while True:
            candidate = secrets.token_bytes(32)
            try:
                return cls(candidate)
            except KeyStoreError:  # out of range, probability ~2^-128
                continue

    @classmethod
    def from_hex(cls, secret_hex: str) -> DeviceKey:
        try:
            return cls(bytes.fromhex(secret_hex))
        except ValueError as exc:
            raise KeyStoreError(f"device secret is not valid 32-byte hex: {exc}") from exc

    def sign(self, digest: bytes) -> bytes:
        """BIP-340 signature over a 32-byte digest (a Nostr event id)."""
        if len(digest) != 32:
            raise KeyStoreError(f"BIP-340 signs a 32-byte digest, got {len(digest)} bytes")
        return PrivateKey(self._secret).sign_schnorr(digest, secrets.token_bytes(32))

    def ecdh(self, peer_pubkey_hex: str) -> bytes:
        """Raw shared x coordinate with `peer_pubkey_hex` (input to NIP-44)."""
        try:
            peer = PublicKey(b"\x02" + bytes.fromhex(peer_pubkey_hex))
        except ValueError as exc:
            raise KeyStoreError(
                f"peer public key {peer_pubkey_hex[:16]!r} is not a valid x-only secp256k1 key: {exc}"
            ) from exc
        return peer.multiply(self._secret).format()[1:]

    def secret_bytes(self) -> bytes:
        """The raw secret. Callers must not write it to disk; use `save_device_key`."""
        return self._secret

    def __repr__(self) -> str:
        return f"DeviceKey(pubkey={self.pubkey_hex[:12]}..)"


class SAKey:
    """EVM key for the SA identity (EIP-191 personal_sign)."""

    __slots__ = ("_account",)

    def __init__(self, secret: bytes) -> None:
        if len(secret) != 32:
            raise KeyStoreError(f"SA secret must be 32 bytes, got {len(secret)}")
        self._account: LocalAccount = Account.from_key(secret)

    @classmethod
    def generate(cls) -> SAKey:
        return cls(secrets.token_bytes(32))

    @property
    def address(self) -> str:
        """Lowercase 0x address."""
        return str(self._account.address).lower()

    def sign_message(self, text: str) -> str:
        """EIP-191 `personal_sign` signature as 0x + 130 hex."""
        signed = self._account.sign_message(encode_defunct(text=text))
        return "0x" + bytes(signed.signature).hex()

    def secret_bytes(self) -> bytes:
        return bytes(self._account.key)

    def __repr__(self) -> str:
        return f"SAKey(address={self.address})"


# ---------------------------------------------------------------- encrypted file format


class KeyEnvelope(BaseModel):
    """What is written to disk. Contains no plaintext secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = 1
    kind: Literal["nostr-device", "evm-sa", "exchange-api"]
    public_id: str  # pubkey hex or lowercase address: lets a user pick the right file
    kdf: Literal["scrypt"] = "scrypt"
    n: int = Field(ge=1)
    r: int = Field(ge=1, le=64)
    p: int = Field(ge=1, le=16)
    salt: str
    nonce: str
    ciphertext: str  # base64(ciphertext || 16-byte tag)

    def aad(self) -> bytes:
        header = self.model_dump(exclude={"nonce", "ciphertext"})
        return json.dumps(header, sort_keys=True, separators=(",", ":")).encode()


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        passphrase.encode(), salt=salt, n=n, r=r, p=p, maxmem=129 * n * r + 2**20, dklen=32
    )


def encrypt_secret(
    secret: bytes,
    kind: Literal["nostr-device", "evm-sa", "exchange-api"],
    public_id: str,
    passphrase: str,
    *,
    n: int = SCRYPT_N_DEFAULT,
) -> KeyEnvelope:
    if len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise KeyStoreError(
            f"passphrase has {len(passphrase)} characters; use at least {MIN_PASSPHRASE_CHARS}"
        )
    if not SCRYPT_N_MIN <= n <= SCRYPT_N_MAX:
        raise KeyStoreError(f"scrypt n={n} is outside {SCRYPT_N_MIN}..{SCRYPT_N_MAX}")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = KeyEnvelope(
        kind=kind,
        public_id=public_id,
        n=n,
        r=8,
        p=1,
        salt=base64.b64encode(salt).decode(),
        nonce=base64.b64encode(nonce).decode(),
        ciphertext="",
    )
    cipher = ChaCha20_Poly1305.new(key=_derive(passphrase, salt, n, 8, 1), nonce=nonce)
    cipher.update(header.aad())
    ct, tag = cipher.encrypt_and_digest(secret)
    return header.model_copy(update={"ciphertext": base64.b64encode(ct + tag).decode()})


def decrypt_secret(env: KeyEnvelope, passphrase: str) -> bytes:
    if not SCRYPT_N_MIN <= env.n <= SCRYPT_N_MAX:
        raise KeyStoreError(
            f"key file asks for scrypt n={env.n}, outside {SCRYPT_N_MIN}..{SCRYPT_N_MAX}; "
            "the file was edited or is not a Superalgos key file"
        )
    if env.n * env.r > SCRYPT_NR_MAX or env.n * env.r * env.p > SCRYPT_NRP_MAX:
        raise KeyStoreError(
            f"key file asks for scrypt n={env.n}, r={env.r}, p={env.p} (n*r={env.n * env.r}, "
            f"n*r*p={env.n * env.r * env.p}), above the limits n*r <= {SCRYPT_NR_MAX} and "
            f"n*r*p <= {SCRYPT_NRP_MAX}; it would exhaust memory or CPU. The file was edited or is "
            "not a Superalgos key file"
        )
    try:
        salt = base64.b64decode(env.salt, validate=True)
        nonce = base64.b64decode(env.nonce, validate=True)
        blob = base64.b64decode(env.ciphertext, validate=True)
    except ValueError as exc:
        raise KeyStoreError(f"key file has invalid base64: {exc}") from exc
    if len(blob) < 17 or len(nonce) != 12:
        raise KeyStoreError("key file ciphertext or nonce has the wrong length; file is corrupt")
    cipher = ChaCha20_Poly1305.new(key=_derive(passphrase, salt, env.n, env.r, env.p), nonce=nonce)
    cipher.update(env.aad())
    try:
        return bytes(cipher.decrypt_and_verify(blob[:-16], blob[-16:]))
    except ValueError as exc:
        raise KeyStoreError(
            "wrong passphrase, or the key file was modified; nothing was decrypted"
        ) from exc


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` with mode 0600, atomically, refusing to overwrite."""
    path = Path(path)
    if path.exists():
        raise KeyStoreError(f"{path} already exists; refusing to overwrite a key file")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_device_key(
    path: Path, key: DeviceKey, passphrase: str, *, n: int = SCRYPT_N_DEFAULT
) -> None:
    env = encrypt_secret(key.secret_bytes(), "nostr-device", key.pubkey_hex, passphrase, n=n)
    _write_private(path, env.model_dump_json(indent=2) + "\n")


def save_sa_key(path: Path, key: SAKey, passphrase: str, *, n: int = SCRYPT_N_DEFAULT) -> None:
    env = encrypt_secret(key.secret_bytes(), "evm-sa", key.address, passphrase, n=n)
    _write_private(path, env.model_dump_json(indent=2) + "\n")


def _read_envelope(path: Path, kind: str) -> KeyEnvelope:
    try:
        env = KeyEnvelope.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyStoreError(f"key file {path} not found; create one first") from exc
    except ValueError as exc:
        raise KeyStoreError(f"{path} is not a Superalgos key file: {exc}") from exc
    if env.kind != kind:
        raise KeyStoreError(f"{path} holds a {env.kind!r} key, expected {kind!r}")
    return env


def load_device_key(path: Path, passphrase: str) -> DeviceKey:
    env = _read_envelope(path, "nostr-device")
    key = DeviceKey(decrypt_secret(env, passphrase))
    if key.pubkey_hex != env.public_id:
        raise KeyStoreError(
            f"{path} decrypts to {key.pubkey_hex[:12]}.., header says {env.public_id[:12]}.."
        )
    return key


def load_sa_key(path: Path, passphrase: str) -> SAKey:
    env = _read_envelope(path, "evm-sa")
    key = SAKey(decrypt_secret(env, passphrase))
    if key.address != env.public_id:
        raise KeyStoreError(f"{path} decrypts to {key.address}, header says {env.public_id}")
    return key


# ---------------------------------------------------------------- exchange API credentials


class ExchangeCredentials(BaseModel):
    """An exchange API key and secret. ``repr``/``str``/``model_dump`` never show the values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr
    api_secret: SecretStr


class NotEd25519Error(ValueError):
    """The exchange secret is not an unencrypted Ed25519 PKCS#8 private key. The message never contains the secret."""


def normalize_ed25519_secret(secret: str) -> str:
    """The unencrypted Ed25519 PKCS#8 PEM in ``secret`` (with or without its BEGIN/END lines), as a full PEM.

    Raises :class:`NotEd25519Error` with a one-line message that says what is wrong and what to do, and never
    includes any part of ``secret``.
    """
    fix = (
        "generate an Ed25519 key, register its public key on testnet.binance.vision, "
        "and pass the private key PEM as the secret"
    )
    text = secret.strip()
    if "ENCRYPTED" in text:
        raise NotEd25519Error(
            f"the API secret is an encrypted PEM; decrypt it first (openssl pkey), then {fix}"
        )
    body = "".join(line.strip() for line in text.splitlines() if not line.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
        key = load_der_private_key(der, password=None)
    except Exception:
        raise NotEd25519Error(
            f"the API secret is not a PKCS#8 PEM private key (an HMAC secret?); {fix}"
        ) from None
    if isinstance(key, rsa.RSAPrivateKey):
        raise NotEd25519Error(f"the API secret is an RSA key, not Ed25519; {fix}")
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise NotEd25519Error(f"the API secret is not an Ed25519 key; {fix}")
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


def save_exchange_credentials(
    path: Path,
    exchange: str,
    credentials: ExchangeCredentials,
    passphrase: str,
    *,
    n: int = SCRYPT_N_DEFAULT,
) -> None:
    """Encrypt key and secret together in one envelope. The header names only ``exchange``, never the key."""
    payload = json.dumps(
        {
            "api_key": credentials.api_key.get_secret_value(),
            "api_secret": credentials.api_secret.get_secret_value(),
        },
        separators=(",", ":"),
    ).encode()
    env = encrypt_secret(payload, "exchange-api", exchange, passphrase, n=n)
    _write_private(path, env.model_dump_json(indent=2) + "\n")


def load_exchange_credentials(path: Path, exchange: str, passphrase: str) -> ExchangeCredentials:
    env = _read_envelope(path, "exchange-api")
    if env.public_id != exchange:
        raise KeyStoreError(
            f"{path} holds credentials for {env.public_id!r}, expected {exchange!r}"
        )
    try:
        return ExchangeCredentials.model_validate_json(decrypt_secret(env, passphrase))
    except ValueError:
        # pydantic's message would echo the decrypted input; say only what is wrong with the file
        raise KeyStoreError(
            f"{path} decrypted but does not hold an API key and secret; add them again"
        ) from None
