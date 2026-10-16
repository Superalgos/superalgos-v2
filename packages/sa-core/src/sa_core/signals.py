"""Build and verify signal (9317) and commitment (9318) events, public and private.

Verification order for every signal, relayed or read from a file (SEP-3.29): event id + BIP-340
signature, kind, exactly one valid `sa-delegation` tag at time `at`, content schema.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import ValidationError

from ._canon import sha256_hex
from .delegation import (
    DelegationError,
    delegation_from_event,
    delegation_tag,
    verify_event_delegation,
)
from .keys import DeviceKey
from .models import (
    COMMITMENT_KIND,
    SIGNAL_KIND,
    Commitment,
    Delegation,
    NostrEvent,
    Revocation,
    Signal,
    SignedSignal,
)
from .nostr import (
    NostrError,
    build_event,
    event_from_json,
    nip44_decrypt,
    nip44_encrypt,
    verify_event,
)


class SignalError(Exception):
    """A signal or commitment failed verification. The message says which check and why."""


def build_signal_event(
    key: DeviceKey, signal: Signal, delegation: Delegation, created_at: int | None = None
) -> NostrEvent:
    """Public signal: content is the signal JSON."""
    ts = signal.created_ms // 1000 if created_at is None else created_at
    return build_event(key, SIGNAL_KIND, signal.model_dump_json(), [delegation_tag(delegation)], ts)


def build_private_signal_event(
    key: DeviceKey,
    signal: Signal,
    delegation: Delegation,
    recipient_pubkey: str,
    created_at: int | None = None,
) -> NostrEvent:
    """Private signal for one entitled follower: content is NIP-44 ciphertext (SEP-3.26)."""
    ts = signal.created_ms // 1000 if created_at is None else created_at
    return build_event(
        key,
        SIGNAL_KIND,
        nip44_encrypt(signal.model_dump_json(), key, recipient_pubkey),
        [delegation_tag(delegation), ("p", recipient_pubkey)],
        ts,
    )


def commitment_digest(plaintext: str) -> str:
    return sha256_hex(plaintext)


def build_commitment_event(
    key: DeviceKey, signal: Signal, delegation: Delegation, created_at: int | None = None
) -> NostrEvent:
    """Commit to a private signal: only sha256(plaintext) plus its chain slot is public."""
    c = Commitment(
        strategy_id=signal.strategy_id,
        seq=signal.seq,
        prev=signal.prev,
        digest=commitment_digest(signal.model_dump_json()),
    )
    return build_event(
        key, COMMITMENT_KIND, c.model_dump_json(), [delegation_tag(delegation)], created_at
    )


def _check_envelope(event: NostrEvent, kind: int, label: str) -> None:
    try:
        verify_event(event)
    except NostrError as exc:
        raise SignalError(str(exc)) from exc
    if event.kind != kind:
        raise SignalError(
            f"event {event.id[:12]}.. has kind {event.kind}, expected {kind} ({label})"
        )


def verify_signal_event(
    event: NostrEvent,
    at: int,
    expected_evm: str | None = None,
    revocations: Iterable[Revocation] = (),
) -> SignedSignal:
    """Verify a public signal event. `at` is the unix time the delegation is judged at."""
    _check_envelope(event, SIGNAL_KIND, "signal")
    return _finish(event, event.content, at, expected_evm, revocations)


def open_private_signal(
    event: NostrEvent,
    recipient: DeviceKey,
    at: int,
    expected_evm: str | None = None,
    revocations: Iterable[Revocation] = (),
) -> SignedSignal:
    """Verify the envelope, decrypt with the recipient key, then verify the content (SEP-3.26)."""
    _check_envelope(event, SIGNAL_KIND, "signal")
    try:
        plaintext = nip44_decrypt(event.content, recipient, event.pubkey)
    except NostrError as exc:
        raise SignalError(f"cannot decrypt private signal {event.id[:12]}..: {exc}") from exc
    return _finish(event, plaintext, at, expected_evm, revocations)


def _finish(
    event: NostrEvent,
    content: str,
    at: int,
    expected_evm: str | None,
    revocations: Iterable[Revocation],
) -> SignedSignal:
    try:
        verify_event_delegation(event, at, expected_evm, revocations)
        delegation = delegation_from_event(event)
    except DelegationError as exc:
        raise SignalError(f"delegation check failed: {exc}") from exc
    try:
        signal = Signal.model_validate_json(content)
    except ValidationError as exc:
        raise SignalError(
            f"signal content invalid: {exc.errors()[0]['msg']} ({exc.error_count()} errors)"
        ) from exc
    return SignedSignal(event=event, signal=signal, delegation=delegation)


def verify_commitment_event(
    event: NostrEvent, at: int, revocations: Iterable[Revocation] = ()
) -> tuple[Commitment, str]:
    """Return (commitment, delegating SA address)."""
    _check_envelope(event, COMMITMENT_KIND, "commitment")
    try:
        addr = verify_event_delegation(event, at, None, revocations)
        return Commitment.model_validate_json(event.content), addr
    except DelegationError as exc:
        raise SignalError(f"delegation check failed: {exc}") from exc
    except ValidationError as exc:
        raise SignalError(f"commitment content invalid: {exc.errors()[0]['msg']}") from exc


def check_reveal(commitment: Commitment, plaintext: str) -> Signal:
    """The revealed plaintext must hash to the committed digest and fill the committed slot."""
    if commitment_digest(plaintext) != commitment.digest:
        raise SignalError(
            f"revealed plaintext hashes to {commitment_digest(plaintext)[:12]}.., "
            f"commitment holds {commitment.digest[:12]}.."
        )
    try:
        signal = Signal.model_validate_json(plaintext)
    except ValidationError as exc:
        raise SignalError(f"revealed signal invalid: {exc.errors()[0]['msg']}") from exc
    if (signal.strategy_id, signal.seq, signal.prev) != (
        commitment.strategy_id,
        commitment.seq,
        commitment.prev,
    ):
        raise SignalError(
            f"revealed signal slot ({signal.strategy_id}, {signal.seq}) differs from the "
            f"commitment slot ({commitment.strategy_id}, {commitment.seq})"
        )
    return signal


def signal_from_text(text: str, at: int, expected_evm: str | None = None) -> SignedSignal:
    """Off-relay signal pasted or read from a file: same checks as a relayed one (SEP-3.29)."""
    try:
        json.loads(text)
        event = event_from_json(text)
    except (ValueError, NostrError) as exc:
        raise SignalError(f"input is not a Nostr event JSON: {exc}") from exc
    return verify_signal_event(event, at, expected_evm)
