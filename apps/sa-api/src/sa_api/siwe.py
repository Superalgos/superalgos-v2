"""Sign-In with Ethereum (EIP-4361) for the dashboard: nonces, message check, wallet sessions.

Flow (the only non-GET routes of the API, ``/api/auth/*``):

1. ``nonce(address, host)``: the server builds the complete EIP-4361 message (domain, address, statement, URI, version 1,
   chain id, a random nonce, issued-at, expiration at most 10 minutes later) and remembers it under the nonce.
2. The wallet signs it (``personal_sign``, EIP-191).
3. ``verify(message, signature, host)``: the nonce is consumed first (single use, so a replay or a retry of a failed
   attempt needs a new nonce), then the message must be unexpired, name this server's domain and be exactly the message
   issued for that nonce, and the signature must recover the address in the message. Only then a session is created.

State is in memory and bounded: a restart signs everybody out, which is the simplest safe default. The session
token is 256 random bits; the browser holds it in an ``HttpOnly; SameSite=Strict`` cookie and the page never sees it.
No key is ever held or received here: the server only recovers an address from a signature (SEP-1.13).
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils.address import to_checksum_address

SESSION_COOKIE = "sa_session"
MAX_NONCE_TTL_S = 600  # EIP-4361 expiration is at most 10 minutes after issue
MAX_PENDING = 10_000
MAX_SESSIONS = 10_000
DEFAULT_NONCE_RATE_PER_MIN = 30  # per client IP
MAX_RATE_KEYS = 10_000  # client IPs tracked at once
STATEMENT = "Sign in to the Superalgos v2 dashboard. This proves you control this address; it moves no funds."

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIGNATURE = re.compile(r"^0x[0-9a-fA-F]{130}$")
_MESSAGE = re.compile(
    r"^(?P<domain>[^\s/?#]+) wants you to sign in with your Ethereum account:\n"
    r"(?P<address>0x[0-9a-fA-F]{40})\n\n"
    r"(?P<statement>[^\n]+)\n\n"
    r"URI: (?P<uri>[^\s]+)\n"
    r"Version: (?P<version>[^\n]+)\n"
    r"Chain ID: (?P<chain_id>[0-9]+)\n"
    r"Nonce: (?P<nonce>[A-Za-z0-9]{8,})\n"
    r"Issued At: (?P<issued_at>[^\n]+)\n"
    r"Expiration Time: (?P<expiration>[^\n]+)$"
)


class SiweError(Exception):
    """A sign-in step failed. ``status`` is the HTTP status; the message says what failed and what to do."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Parsed:
    domain: str
    address: str
    statement: str
    uri: str
    version: str
    chain_id: int
    nonce: str
    issued_at: str
    expiration: str


@dataclass(frozen=True)
class _Pending:
    address: str  # lowercase
    message: str
    domain: str
    expires_s: float


@dataclass(frozen=True)
class Session:
    address: str  # EIP-55 checksummed
    expires_s: float


def iso_ms(unix_s: float) -> str:
    return (
        datetime.fromtimestamp(unix_s, UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{int(unix_s * 1000) % 1000:03d}Z"
    )


def parse_time(text: str) -> float:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise SiweError(
            400,
            f"timestamp {text!r} in the message is not ISO 8601; sign the message exactly as the server issued it",
        ) from exc


def parse_message(message: str) -> Parsed:
    """Strict EIP-4361 parse of the message layout this server issues."""
    m = _MESSAGE.match(message)
    if m is None:
        raise SiweError(
            400,
            "message is not an EIP-4361 sign-in message in the form this server issues; "
            "request one from POST /api/auth/nonce and sign it unchanged",
        )
    return Parsed(
        domain=m["domain"], address=m["address"], statement=m["statement"], uri=m["uri"], version=m["version"],
        chain_id=int(m["chain_id"]), nonce=m["nonce"], issued_at=m["issued_at"], expiration=m["expiration"],
    )  # fmt: skip


def checksum(address: str) -> str:
    if not _ADDRESS.match(address):
        raise SiweError(400, f"address {address!r} must be 0x followed by 40 hex digits")
    return str(to_checksum_address(address))


def recover(message: str, signature: str) -> str:
    """The EIP-191 (personal_sign) signer of ``message``, checksummed."""
    if not _SIGNATURE.match(signature):
        raise SiweError(
            400,
            f"signature must be 0x followed by 130 hex digits (r, s, v), got {len(signature)} characters",
        )
    try:
        return str(Account.recover_message(encode_defunct(text=message), signature=signature))
    except Exception as exc:  # eth_keys raises several types for an unrecoverable signature
        raise SiweError(401, f"signature does not recover an address ({exc}); sign again") from exc


class SiweAuth:
    def __init__(
        self,
        *,
        domain: str | None = None,
        uri: str | None = None,
        chain_id: int = 1,
        nonce_ttl_s: int = MAX_NONCE_TTL_S,
        session_ttl_s: int = 86_400,
        nonce_rate_per_min: int = DEFAULT_NONCE_RATE_PER_MIN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if nonce_rate_per_min < 1:
            raise ValueError(f"nonce_rate_per_min={nonce_rate_per_min} must be at least 1")
        self.nonce_rate_per_min = nonce_rate_per_min
        self._hits: dict[str, deque[float]] = {}
        if not 0 < nonce_ttl_s <= MAX_NONCE_TTL_S:
            raise ValueError(
                f"nonce_ttl_s={nonce_ttl_s} must be within 1..{MAX_NONCE_TTL_S} seconds (EIP-4361 advice)"
            )
        self.domain, self.uri, self.chain_id = domain, uri, chain_id
        self.nonce_ttl_s, self.session_ttl_s = nonce_ttl_s, session_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._sessions: dict[str, Session] = {}

    # ---------------------------------------------------------------- nonce

    def _expected_domain(self, host: str) -> str:
        return self.domain or host

    def _rate_limit(self, client: str, now: float) -> None:
        """At most ``nonce_rate_per_min`` nonces per client IP in any 60 s (sliding window); 429 beyond.

        The pending-nonce pool is shared, so without this one client could fill it and lock everybody out."""
        with self._lock:
            if len(self._hits) >= MAX_RATE_KEYS and client not in self._hits:
                for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - 60]:
                    del self._hits[k]
                if len(self._hits) >= MAX_RATE_KEYS:
                    raise SiweError(
                        429, "too many clients are requesting sign-in nonces; retry in a minute"
                    )
            q = self._hits.setdefault(client, deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.nonce_rate_per_min:
                raise SiweError(
                    429,
                    f"{client or 'this client'} asked for more than {self.nonce_rate_per_min} sign-in nonces in a "
                    f"minute; wait {max(1, int(q[0] + 60 - now) + 1)} s and request again",
                )
            q.append(now)

    def nonce(
        self, address: str, host: str, scheme: str = "http", client: str = ""
    ) -> tuple[str, str]:
        """A fresh single-use nonce and the complete message to sign for ``address``.

        ``client`` is the caller's IP (behind a proxy: set ``--proxy-headers``, or every user shares the proxy's IP)."""
        addr = checksum(address)
        if not host:
            raise SiweError(
                400, "request has no Host header and no siwe_domain is configured; set siwe_domain"
            )
        domain = self._expected_domain(host)
        now = self._clock()
        self._rate_limit(client, now)
        nonce = secrets.token_hex(16)
        message = (
            f"{domain} wants you to sign in with your Ethereum account:\n{addr}\n\n{STATEMENT}\n\n"
            f"URI: {self.uri or f'{scheme}://{domain}'}\nVersion: 1\nChain ID: {self.chain_id}\nNonce: {nonce}\n"
            f"Issued At: {iso_ms(now)}\nExpiration Time: {iso_ms(now + self.nonce_ttl_s)}"
        )
        with self._lock:
            self._purge(now)
            if len(self._pending) >= MAX_PENDING:
                raise SiweError(
                    429,
                    f"{MAX_PENDING} sign-ins are pending; wait a few minutes and request a new nonce",
                )
            self._pending[nonce] = _Pending(addr.lower(), message, domain, now + self.nonce_ttl_s)
        return nonce, message

    def _purge(self, now: float) -> None:
        for k in [k for k, p in self._pending.items() if p.expires_s <= now]:
            del self._pending[k]
        for k in [k for k, s in self._sessions.items() if s.expires_s <= now]:
            del self._sessions[k]

    # ---------------------------------------------------------------- verify

    def verify(self, message: str, signature: str, host: str) -> tuple[str, str, int]:
        """Check a signed message and open a session: returns (address, session token, max-age seconds)."""
        parsed = parse_message(message)
        if not _SIGNATURE.match(signature):
            raise SiweError(
                400,
                f"signature must be 0x followed by 130 hex digits (r, s, v), got {len(signature)} characters",
            )
        with self._lock:  # consume first: a nonce works once, whatever happens next
            pending = self._pending.pop(parsed.nonce, None)
        if pending is None:
            raise SiweError(
                401, "unknown or already used nonce; request a new one from POST /api/auth/nonce"
            )
        now = self._clock()
        expiry = min(parse_time(parsed.expiration), pending.expires_s)
        if now >= expiry:
            raise SiweError(
                401,
                f"sign-in message expired at {iso_ms(expiry)} (now {iso_ms(now)}); request a new nonce",
            )
        want = self._expected_domain(host)
        if parsed.domain != want:
            raise SiweError(
                401,
                f"message domain {parsed.domain!r} is not this server's {want!r}; sign in on the site you are using",
            )
        if parsed.version != "1" or parsed.chain_id != self.chain_id:
            raise SiweError(
                401,
                f"message must be version 1 on chain {self.chain_id}, got {parsed.version} on {parsed.chain_id}",
            )
        if message != pending.message:
            raise SiweError(
                401,
                "message differs from the one issued for this nonce; sign the message unchanged",
            )
        signer = recover(message, signature)
        if signer.lower() != pending.address:
            raise SiweError(
                401,
                f"signature recovers {signer}, but the message is for {checksum(pending.address)}",
            )
        token = secrets.token_urlsafe(32)
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                self._purge(now)
            if len(self._sessions) >= MAX_SESSIONS:
                raise SiweError(429, f"{MAX_SESSIONS} sessions are open; try again later")
            self._sessions[token] = Session(signer, now + self.session_ttl_s)
        return signer, token, self.session_ttl_s

    # ---------------------------------------------------------------- sessions

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if s is not None and s.expires_s <= self._clock():
                del self._sessions[token]
                return None
            return s

    def logout(self, token: str | None) -> bool:
        """Close the session; False if there was none."""
        with self._lock:
            return bool(token) and self._sessions.pop(token or "", None) is not None
