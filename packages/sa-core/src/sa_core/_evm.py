"""EIP-191 `personal_sign` helpers shared by delegation, allocations, rule sets, governance."""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_defunct


class SignatureError(ValueError):
    """An EIP-191 signature is malformed or does not recover to the expected address."""


def recover_address(message: str, signature: str) -> str:
    """Lowercase 0x address that signed `message` with EIP-191 personal_sign."""
    try:
        return str(
            Account.recover_message(encode_defunct(text=message), signature=signature)
        ).lower()
    except Exception as exc:  # eth_account raises several types for malformed signatures
        raise SignatureError(f"unrecoverable EIP-191 signature {signature[:14]}..: {exc}") from exc


def require_signer(message: str, signature: str, expected: str) -> None:
    got = recover_address(message, signature)
    if got != expected.lower():
        raise SignatureError(f"signature recovers {got}, expected {expected.lower()}")
