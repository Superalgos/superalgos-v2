"""Nostr: NIP-01 events (id + BIP-340), NIP-44 v2 encryption, NIP-42 AUTH, async relay client.

Events are built and verified with this module only; no Nostr SDK is involved, so the same
bytes verify in Go (khatru) and TypeScript.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import itertools
import json
import math
import secrets
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import websockets
from coincurve import PublicKeyXOnly
from Crypto.Cipher import ChaCha20

from .keys import DeviceKey, KeyStoreError
from .models import AUTH_KIND, NostrEvent


class NostrError(Exception):
    """Event, ciphertext or relay interaction is invalid. The message names the value."""


# ---------------------------------------------------------------- NIP-01


def compute_event_id(
    pubkey: str, created_at: int, kind: int, tags: Iterable[Sequence[str]], content: str
) -> str:
    """sha256 of the canonical `[0, pubkey, created_at, kind, tags, content]` array."""
    ser = json.dumps(
        [0, pubkey, created_at, kind, [list(t) for t in tags], content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(ser.encode()).hexdigest()


def build_event(
    key: DeviceKey,
    kind: int,
    content: str,
    tags: Iterable[Sequence[str]] = (),
    created_at: int | None = None,
) -> NostrEvent:
    ts = int(time.time()) if created_at is None else created_at
    tag_t = tuple(tuple(t) for t in tags)
    eid = compute_event_id(key.pubkey_hex, ts, kind, tag_t, content)
    sig = key.sign(bytes.fromhex(eid)).hex()
    return NostrEvent(
        id=eid,
        pubkey=key.pubkey_hex,
        created_at=ts,
        kind=kind,
        tags=tag_t,
        content=content,
        sig=sig,
    )


def schnorr_verify(msg: bytes, pubkey: bytes, sig: bytes) -> bool:
    """BIP-340 verification by libsecp256k1. False (never raises) on malformed input."""
    if len(msg) != 32 or len(pubkey) != 32 or len(sig) != 64:
        return False
    try:
        return bool(PublicKeyXOnly(pubkey).verify(sig, msg))
    except ValueError:  # pubkey not on the curve
        return False


def verify_event(event: NostrEvent) -> None:
    """Raise `NostrError` unless `id` matches the body and `sig` is a valid BIP-340 signature."""
    want = compute_event_id(event.pubkey, event.created_at, event.kind, event.tags, event.content)
    if want != event.id:
        raise NostrError(
            f"event id {event.id[:12]}.. does not match its body (recomputed {want[:12]}..): "
            "content, tags or timestamp were altered"
        )
    if not schnorr_verify(
        bytes.fromhex(event.id), bytes.fromhex(event.pubkey), bytes.fromhex(event.sig)
    ):
        raise NostrError(
            f"signature of event {event.id[:12]}.. is not valid for pubkey {event.pubkey[:12]}.."
        )


def event_from_json(text: str | bytes | dict[str, Any]) -> NostrEvent:
    try:
        if isinstance(text, dict):
            return NostrEvent.model_validate(text)
        return NostrEvent.model_validate_json(text)
    except ValueError as exc:
        raise NostrError(f"not a NIP-01 event: {exc}") from exc


# ---------------------------------------------------------------- NIP-44 v2
#
# Spec: https://github.com/nostr-protocol/nips/blob/master/44.md. Checked against the official
# vectors (tests/data/nip44.vectors.json). Primitives: libsecp256k1 (via coincurve) for the
# unhashed ECDH, stdlib HMAC-SHA256 for HKDF, pycryptodome for ChaCha20.

_NIP44_VERSION = 2
_NIP44_SALT = b"nip44-v2"
NIP44_MAX_PLAINTEXT = 65_535


def conversation_key(key: DeviceKey, peer_pubkey_hex: str) -> bytes:
    """HKDF-extract(salt='nip44-v2', ikm=ECDH x). Symmetric in (a, B) and (b, A)."""
    try:
        shared = key.ecdh(peer_pubkey_hex)
    except KeyStoreError as exc:
        raise NostrError(f"cannot derive a NIP-44 conversation key: {exc}") from exc
    return hmac.digest(_NIP44_SALT, shared, "sha256")


def padded_len(n: int) -> int:
    """NIP-44 padding: 32 for n <= 32, else chunked to 1/8 of the next power of two."""
    if n <= 32:
        return 32
    next_power = 1 << (n - 1).bit_length()
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (math.floor((n - 1) / chunk) + 1)


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-Expand with SHA-256. `prk` is used as is (no second extract)."""
    out, block = b"", b""
    for counter in range(1, -(-length // 32) + 1):
        block = hmac.digest(prk, block + info + bytes([counter]), "sha256")
        out += block
    return out[:length]


def message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    """(chacha_key, chacha_nonce, hmac_key) = HKDF-expand(prk=conv_key, info=nonce, 76)."""
    if len(conv_key) != 32 or len(nonce) != 32:
        raise NostrError(
            f"NIP-44 needs a 32-byte conversation key and nonce, got {len(conv_key)} and {len(nonce)}"
        )
    okm = _hkdf_expand(conv_key, nonce, 76)
    return okm[:32], okm[32:44], okm[44:76]


def _mac(key: bytes, message: bytes, aad: bytes) -> bytes:
    return hmac.digest(key, aad + message, "sha256")


def encrypt_with_conversation_key(plaintext: str, conv_key: bytes, nonce: bytes) -> str:
    """NIP-44 v2 payload (base64) from a ready conversation key and an explicit 32-byte nonce."""
    raw = plaintext.encode()
    if not 1 <= len(raw) <= NIP44_MAX_PLAINTEXT:
        raise NostrError(f"plaintext is {len(raw)} bytes; NIP-44 needs 1..{NIP44_MAX_PLAINTEXT}")
    chacha_key, chacha_nonce, hmac_key = message_keys(conv_key, nonce)
    padded = len(raw).to_bytes(2, "big") + raw + b"\x00" * (padded_len(len(raw)) - len(raw))
    ct = ChaCha20.new(key=chacha_key, nonce=chacha_nonce).encrypt(padded)
    return base64.b64encode(
        bytes([_NIP44_VERSION]) + nonce + ct + _mac(hmac_key, ct, nonce)
    ).decode()


def nip44_encrypt(
    plaintext: str, key: DeviceKey, peer_pubkey_hex: str, *, nonce: bytes | None = None
) -> str:
    """NIP-44 v2 payload (base64). `nonce` is for test vectors only; leave it None."""
    nonce = secrets.token_bytes(32) if nonce is None else nonce
    return encrypt_with_conversation_key(plaintext, conversation_key(key, peer_pubkey_hex), nonce)


def decrypt_with_conversation_key(payload: str, conv_key: bytes) -> str:
    if not payload or payload.startswith("#"):
        raise NostrError("unsupported NIP-44 payload (empty or '#'-prefixed future version)")
    if not 132 <= len(payload) <= 87_472:
        raise NostrError(f"NIP-44 payload has invalid length {len(payload)}")
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise NostrError(f"NIP-44 payload is not base64: {exc}") from exc
    if not 99 <= len(data) <= 65_603:
        raise NostrError(f"NIP-44 decoded payload has invalid length {len(data)}")
    if data[0] != _NIP44_VERSION:
        raise NostrError(f"unsupported NIP-44 version {data[0]}; expected {_NIP44_VERSION}")
    nonce, ct, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = message_keys(conv_key, nonce)
    if not hmac.compare_digest(_mac(hmac_key, ct, nonce), mac):
        raise NostrError("NIP-44 MAC mismatch: wrong key, or the ciphertext was modified")
    padded = ChaCha20.new(key=chacha_key, nonce=chacha_nonce).decrypt(ct)
    n = int.from_bytes(padded[:2], "big")
    body = padded[2 : 2 + n]
    if n == 0 or len(body) != n or len(padded) != 2 + padded_len(n):
        raise NostrError("NIP-44 padding is invalid")
    try:
        return body.decode()
    except UnicodeDecodeError as exc:
        raise NostrError(f"NIP-44 plaintext is not UTF-8: {exc}") from exc


def nip44_decrypt(payload: str, key: DeviceKey, peer_pubkey_hex: str) -> str:
    return decrypt_with_conversation_key(payload, conversation_key(key, peer_pubkey_hex))


# ---------------------------------------------------------------- NIP-42 AUTH


def build_auth_event(
    key: DeviceKey,
    relay_url: str,
    challenge: str,
    extra_tags: Iterable[Sequence[str]] = (),
    created_at: int | None = None,
) -> NostrEvent:
    """Kind-22242 AUTH event. `extra_tags` carries the `sa-delegation` tag for gated relays."""
    tags: list[Sequence[str]] = [("relay", relay_url), ("challenge", challenge), *extra_tags]
    return build_event(key, AUTH_KIND, "", tags, created_at)


# ---------------------------------------------------------------- relay client


@dataclass(frozen=True)
class PublishResult:
    accepted: bool
    message: str


class RelayClient:
    """Minimal async NIP-01 client: publish, one-shot query, live subscribe, NIP-42 AUTH.

    Use as `async with RelayClient(url) as relay:`. When `auth_key` is given, an AUTH challenge
    is answered automatically (with `auth_tags` appended), and an `auth-required:` rejection is
    retried once after authenticating.
    """

    def __init__(
        self,
        url: str,
        *,
        auth_key: DeviceKey | None = None,
        auth_tags: Sequence[Sequence[str]] = (),
        connect_timeout: float = 5.0,
    ) -> None:
        self.url = url
        self._auth_key = auth_key
        self._auth_tags = [tuple(t) for t in auth_tags]
        self._connect_timeout = connect_timeout
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._ok: dict[str, asyncio.Future[PublishResult]] = {}
        self._subs: dict[str, asyncio.Queue[NostrEvent | None]] = {}
        self._challenge: str | None = None
        self._authed = asyncio.Event()
        self._challenge_seen = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._ids = itertools.count(1)

    async def __aenter__(self) -> RelayClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url, open_timeout=self._connect_timeout, max_size=2**22),
                self._connect_timeout + 1,
            )
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            raise NostrError(
                f"cannot connect to relay {self.url}: {type(exc).__name__}: {exc}; "
                "check the URL and that the relay is running"
            ) from exc
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        for task in [self._reader, *self._tasks]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        for q in self._subs.values():
            q.put_nowait(None)

    async def _send(self, frame: list[Any]) -> None:
        await self._ws.send(json.dumps(frame, separators=(",", ":")))

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, list) and msg:
                    await self._dispatch(msg)
        except (websockets.WebSocketException, OSError):
            pass
        finally:
            self._closed = True
            for q in self._subs.values():
                q.put_nowait(None)
            for fut in self._ok.values():
                if not fut.done():
                    fut.set_result(PublishResult(False, "connection closed before OK"))

    async def _dispatch(self, msg: list[Any]) -> None:
        kind = msg[0]
        if kind == "EVENT" and len(msg) >= 3:
            q = self._subs.get(str(msg[1]))
            if q is not None:
                with contextlib.suppress(NostrError):  # malformed relay output: drop that event
                    q.put_nowait(event_from_json(msg[2]))
        elif kind in ("EOSE", "CLOSED") and len(msg) >= 2:
            q = self._subs.get(str(msg[1]))
            if q is not None:
                q.put_nowait(None)
        elif kind == "OK" and len(msg) >= 4:
            fut = self._ok.get(str(msg[1]))
            if fut is not None and not fut.done():
                fut.set_result(PublishResult(bool(msg[2]), str(msg[3])))
        elif kind == "AUTH" and len(msg) >= 2 and isinstance(msg[1], str):
            self._challenge = msg[1]
            self._challenge_seen.set()
            if self._auth_key is not None:
                await self._authenticate(self._auth_key, msg[1])

    async def _authenticate(self, key: DeviceKey, challenge: str) -> None:
        ev = build_auth_event(key, self.url, challenge, self._auth_tags)
        fut: asyncio.Future[PublishResult] = asyncio.get_running_loop().create_future()
        self._ok[ev.id] = fut
        await self._send(["AUTH", ev.model_dump(mode="json")])
        # the reader task resolves `fut`; do not block the reader on it
        task = asyncio.create_task(self._await_auth(fut))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _await_auth(self, fut: asyncio.Future[PublishResult]) -> None:
        if (await fut).accepted:
            self._authed.set()

    async def authenticate(self, timeout: float = 5.0) -> None:
        """Wait for the relay challenge and for AUTH to be accepted."""
        if self._auth_key is None:
            raise NostrError(f"relay {self.url} wants AUTH but this client has no auth_key")
        try:
            async with asyncio.timeout(timeout):
                await self._challenge_seen.wait()
                await self._authed.wait()
        except TimeoutError as exc:
            raise NostrError(f"relay {self.url} did not complete AUTH within {timeout}s") from exc

    async def publish(self, event: NostrEvent, timeout: float = 5.0) -> PublishResult:
        res = await self._publish_once(event, timeout)
        if not res.accepted and res.message.startswith("auth-required") and self._auth_key:
            await self.authenticate(timeout)
            res = await self._publish_once(event, timeout)
        return res

    async def _publish_once(self, event: NostrEvent, timeout: float) -> PublishResult:
        fut: asyncio.Future[PublishResult] = asyncio.get_running_loop().create_future()
        self._ok[event.id] = fut
        try:
            await self._send(["EVENT", event.model_dump(mode="json")])
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError as exc:
            raise NostrError(
                f"relay {self.url} did not answer OK for event {event.id[:12]}.. in {timeout}s"
            ) from exc
        finally:
            self._ok.pop(event.id, None)

    async def query(
        self, filters: Sequence[dict[str, Any]], timeout: float = 5.0
    ) -> list[NostrEvent]:
        """REQ, collect events until EOSE (or CLOSED), then CLOSE."""
        sid = f"q{next(self._ids)}"
        q: asyncio.Queue[NostrEvent | None] = asyncio.Queue()
        self._subs[sid] = q
        out: list[NostrEvent] = []
        try:
            await self._send(["REQ", sid, *filters])
            async with asyncio.timeout(timeout):
                while (ev := await q.get()) is not None:
                    out.append(ev)
        except TimeoutError as exc:
            raise NostrError(
                f"relay {self.url} sent no EOSE within {timeout}s ({len(out)} events so far)"
            ) from exc
        finally:
            self._subs.pop(sid, None)
            with contextlib.suppress(Exception):
                await self._send(["CLOSE", sid])
        return out

    async def subscribe(self, filters: Sequence[dict[str, Any]]) -> AsyncIterator[NostrEvent]:
        """Live subscription: yields stored then new events until the caller stops iterating."""
        sid = f"s{next(self._ids)}"
        q: asyncio.Queue[NostrEvent | None] = asyncio.Queue()
        self._subs[sid] = q
        try:
            await self._send(["REQ", sid, *filters])
            while True:
                ev = await q.get()
                if ev is None:
                    if self._closed:
                        return
                    continue  # EOSE marker: keep streaming
                yield ev
        finally:
            self._subs.pop(sid, None)
            with contextlib.suppress(Exception):
                await self._send(["CLOSE", sid])
