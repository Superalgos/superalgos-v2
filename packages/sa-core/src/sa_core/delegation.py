"""SA identity -> device key delegation (EIP-191), with expiry and revocation.

Wire form (ARCHITECTURE.md): tag `["sa-delegation", evm_address, expires_at, sig]`, where `sig`
is EIP-191 over `sa-delegation:{nostr_pubkey_hex}:{expires_at}`.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from . import _evm
from .keys import DeviceKey, SAKey
from .models import (
    DELEGATION_TAG,
    REVOCATION_KIND,
    Delegation,
    NostrEvent,
    Revocation,
    RevocationContent,
)
from .nostr import NostrError, build_event, verify_event

MAX_DELEGATION_DAYS = (
    90  # a delegation outlives a stolen device key for at most this long without a revocation
)


class DelegationError(Exception):
    """Delegation is malformed, forged, expired or revoked. The message names the value."""


def delegation_message(nostr_pubkey: str, expires_at: int) -> str:
    return f"sa-delegation:{nostr_pubkey}:{expires_at}"


def revocation_message(nostr_pubkey: str, effective_at: int) -> str:
    return f"sa-revocation:{nostr_pubkey}:{effective_at}"


def sign_delegation(sa: SAKey, nostr_pubkey: str, expires_at: int) -> Delegation:
    return Delegation(
        evm_address=sa.address,
        nostr_pubkey=nostr_pubkey,
        expires_at=expires_at,
        signature=sa.sign_message(delegation_message(nostr_pubkey, expires_at)),
    )


def sign_revocation(sa: SAKey, nostr_pubkey: str, effective_at: int) -> Revocation:
    return Revocation(
        evm_address=sa.address,
        nostr_pubkey=nostr_pubkey,
        effective_at=effective_at,
        signature=sa.sign_message(revocation_message(nostr_pubkey, effective_at)),
    )


def revocation_event(key: DeviceKey, rev: Revocation, created_at: int | None = None) -> NostrEvent:
    """Kind 9334. `key` is whoever publishes it (any key): the EIP-191 signature inside is the authority."""
    content = RevocationContent(
        device_pubkey=rev.nostr_pubkey,
        effective_at=rev.effective_at,
        address=rev.evm_address,
        sig=rev.signature,
    )
    return build_event(
        key, REVOCATION_KIND, content.model_dump_json(), [("p", rev.nostr_pubkey)], created_at
    )


def validate_revocation_event(event: NostrEvent) -> Revocation:
    """Event id/signature, kind, closed schema, and the EIP-191 signature by `address`. Any publisher is fine."""
    try:
        verify_event(event)
    except NostrError as exc:
        raise DelegationError(str(exc)) from exc
    if event.kind != REVOCATION_KIND:
        raise DelegationError(
            f"event {event.id[:12]}.. has kind {event.kind}, expected {REVOCATION_KIND} (revocation)"
        )
    try:
        c = RevocationContent.model_validate_json(event.content)
    except ValidationError as exc:
        raise DelegationError(f"revocation content invalid: {exc.errors()[0]['msg']}") from exc
    rev = Revocation(
        evm_address=c.address,
        nostr_pubkey=c.device_pubkey,
        effective_at=c.effective_at,
        signature=c.sig,
    )
    verify_revocation(rev)
    return rev


def revocations_from_events(events: Iterable[NostrEvent], limit: int = 10_000) -> list[Revocation]:
    """Every valid revocation among `events`, deduplicated, at most `limit` (invalid ones are skipped).

    Spam is bounded because each entry costs an ECDSA recovery and only (address, device) pairs that
    match a real delegation ever matter.
    """
    out: dict[tuple[str, str, int], Revocation] = {}
    for ev in events:
        if ev.kind != REVOCATION_KIND or len(out) >= limit:
            continue
        try:
            r = validate_revocation_event(ev)
        except DelegationError:
            continue
        out[(r.evm_address, r.nostr_pubkey, r.effective_at)] = r
    return list(out.values())


def delegation_tag(d: Delegation) -> tuple[str, str, str, str]:
    return (DELEGATION_TAG, d.evm_address, str(d.expires_at), d.signature)


def parse_delegation_tag(tag: tuple[str, ...], nostr_pubkey: str) -> Delegation:
    if len(tag) != 4 or tag[0] != DELEGATION_TAG:
        raise DelegationError(
            f"expected [{DELEGATION_TAG!r}, address, expires_at, sig], got {tag!r}"
        )
    try:
        return Delegation(
            evm_address=tag[1], nostr_pubkey=nostr_pubkey, expires_at=int(tag[2]), signature=tag[3]
        )
    except ValueError as exc:
        raise DelegationError(f"malformed {DELEGATION_TAG} tag {tag!r}: {exc}") from exc


def delegation_from_event(event: NostrEvent) -> Delegation:
    tags = event.tag_values(DELEGATION_TAG)
    if len(tags) != 1:
        raise DelegationError(
            f"event {event.id[:12]}.. has {len(tags)} {DELEGATION_TAG} tags; exactly one is required"
        )
    return parse_delegation_tag(tags[0], event.pubkey)


def verify_revocation(r: Revocation) -> None:
    try:
        _evm.require_signer(
            revocation_message(r.nostr_pubkey, r.effective_at), r.signature, r.evm_address
        )
    except _evm.SignatureError as exc:
        raise DelegationError(f"bad revocation signature: {exc}") from exc


def ensure_not_revoked(d: Delegation, at: int, revocations: Iterable[Revocation]) -> None:
    """Raise if a valid revocation by `d`'s address for `d`'s device key is effective at or before `at`."""
    for r in revocations:
        if r.evm_address != d.evm_address or r.nostr_pubkey != d.nostr_pubkey:
            continue
        try:
            verify_revocation(r)
        except DelegationError:
            continue
        if r.effective_at <= at:
            raise DelegationError(
                f"device key {d.nostr_pubkey[:12]}.. was revoked by {d.evm_address} "
                f"effective {r.effective_at}, checked at {at}"
            )


def verify_delegation(
    d: Delegation,
    event_pubkey: str,
    at: int,
    revocations: Iterable[Revocation] = (),
) -> str:
    """Return the delegating SA address if `d` authorises `event_pubkey` at unix time `at`.

    Rejects: wrong device key, expired (`at >= expires_at`), forged signature, and a valid
    revocation by the same SA address for the same device key effective at or before `at`.
    Revocations that fail verification are ignored (they cannot revoke anything).
    """
    if d.nostr_pubkey != event_pubkey:
        raise DelegationError(
            f"delegation is for device key {d.nostr_pubkey[:12]}.., event signed by {event_pubkey[:12]}.."
        )
    if at >= d.expires_at:
        raise DelegationError(f"delegation expired at {d.expires_at}, checked at {at}")
    try:
        _evm.require_signer(
            delegation_message(d.nostr_pubkey, d.expires_at), d.signature, d.evm_address
        )
    except _evm.SignatureError as exc:
        raise DelegationError(f"bad delegation signature: {exc}") from exc
    ensure_not_revoked(d, at, revocations)
    return d.evm_address


def verify_event_delegation(
    event: NostrEvent,
    at: int,
    expected_evm: str | None = None,
    revocations: Iterable[Revocation] = (),
) -> str:
    """Delegation check for an event: tag present, valid at `at`, optionally pinned to a signer."""
    addr = verify_delegation(delegation_from_event(event), event.pubkey, at, revocations)
    if expected_evm is not None and addr != expected_evm.lower():
        raise DelegationError(
            f"delegator {addr} is not the followed identity {expected_evm.lower()}"
        )
    return addr
