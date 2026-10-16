"""Pydantic v2 models for every record that crosses a process boundary.

All models are frozen and reject unknown fields. `model_dump_json` followed by
`model_validate_json` returns an equal object for every valid instance (property-tested).

Wire kinds and field names follow ARCHITECTURE.md. Where the contract is silent the choice is
documented next to the model and listed in packages/sa-core/README.md ("Seam additions").
"""

from __future__ import annotations

import itertools
import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------- wire constants

SIGNAL_KIND = 9317
COMMITMENT_KIND = 9318
PROPOSAL_KIND = 9330
VOTE_KIND = 9331
ALLOCATION_KIND = 9332
GOV_DELEGATION_KIND = 9333  # seam addition: ARCHITECTURE.md gives 37318 to rule sets
REVOCATION_KIND = (
    9334  # device-key revocation, signed (EIP-191) by the SA address; any signer may publish
)
ANCHOR_KIND = 1040
RULESET_KIND = 37318
AUTH_KIND = 22242  # NIP-42

DELEGATION_TAG = "sa-delegation"
BPS = 10_000  # 100% = 10,000 basis points; integer arithmetic keeps tallies exact
TOKEN_UNIT = 10**18  # power is held in 18-decimal integer units

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")


def norm_addr(value: str) -> str:
    """Lowercase 0x + 40 hex, or ValueError naming the bad value."""
    low = value.lower()
    if not _ADDR.match(low):
        raise ValueError(f"{value!r} is not an EVM address (0x + 40 hex characters)")
    return low


Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Hex128 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
Address = Annotated[str, AfterValidator(norm_addr)]
EvmSig = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{130}$")]
Bps = Annotated[int, Field(ge=0, le=BPS)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- Nostr event


class NostrEvent(_Frozen):
    """NIP-01 event. `id` and `sig` are checked by `sa_core.nostr.verify_event`, not here."""

    id: Hex64
    pubkey: Hex64
    created_at: int = Field(ge=0)
    kind: int = Field(ge=0, le=65535)
    tags: tuple[tuple[str, ...], ...] = ()
    content: str
    sig: Hex128

    def tag_values(self, name: str) -> list[tuple[str, ...]]:
        return [t for t in self.tags if t and t[0] == name]


# ---------------------------------------------------------------- signal


class Signal(_Frozen):
    """Signal content (kind 9317), as ARCHITECTURE.md "Signal content fields".

    Provider identity and signature are the event's `pubkey`/`sig` (see `SignedSignal`); the
    content is what is hashed for a commitment (SEP-3.28).
    """

    v: Literal[1] = 1
    strategy_id: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=1)
    prev: Hex64 | None
    instrument: str = Field(pattern=r"^[A-Z0-9]+:[A-Z0-9._-]+$", examples=["BINANCE:BTCUSDT"])
    side: Literal["buy", "sell"]
    action: Literal["open", "close", "adjust"]
    size_fraction: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    order_type: Literal["market", "limit", "stop"]
    limit_price: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    stop_price: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    timeframe: int = Field(gt=0)  # seconds
    created_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _chain_and_price(self) -> Signal:
        if self.seq == 1 and self.prev is not None:
            raise ValueError(f"seq 1 must have prev=null, got prev={self.prev!r}")
        if self.seq > 1 and self.prev is None:
            raise ValueError(f"seq {self.seq} must carry the previous signal's id in prev")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("order_type 'limit' requires limit_price")
        if self.order_type == "stop" and self.stop_price is None:
            raise ValueError("order_type 'stop' requires stop_price")
        return self


class Commitment(_Frozen):
    """Commitment content (kind 9318): sha256 of the signal plaintext, plus its chain slot.

    Seam addition: `strategy_id`, `seq` and `prev` are public so a bundle's completeness can be
    checked without the plaintext (SEP-3.15). They reveal ordering, not trading intent.
    """

    v: Literal[1] = 1
    strategy_id: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=1)
    prev: Hex64 | None
    digest: Hex64  # sha256(signal plaintext, UTF-8)

    @model_validator(mode="after")
    def _chain(self) -> Commitment:
        if (self.seq == 1) != (self.prev is None):
            raise ValueError(f"seq {self.seq} and prev {self.prev!r} disagree (seq 1 <=> null)")
        return self


# ---------------------------------------------------------------- delegation


class Delegation(_Frozen):
    """EIP-191 statement: `evm_address` authorises `nostr_pubkey` until `expires_at`."""

    evm_address: Address
    nostr_pubkey: Hex64
    expires_at: int = Field(gt=0)  # unix seconds
    signature: EvmSig


class Revocation(_Frozen):
    """EIP-191 statement: `evm_address` withdraws `nostr_pubkey`'s delegation from `effective_at`.

    On the wire it is the content of kind 9334 (`RevocationContent`); the in-memory form keeps the
    field names of `Delegation`.
    """

    evm_address: Address
    nostr_pubkey: Hex64
    effective_at: int = Field(ge=0)
    signature: EvmSig


class RevocationContent(_Frozen):
    """Content of kind 9334: `address` withdraws `device_pubkey` from `effective_at` (unix seconds).

    `sig` is EIP-191 by `address` over `sa-revocation:{device_pubkey}:{effective_at}`. The event may be
    signed and published by anyone: the EIP-191 signature is the authority.
    """

    v: Literal[1] = 1
    device_pubkey: Hex64
    effective_at: int = Field(ge=0)
    address: Address
    sig: EvmSig


class SignedSignal(_Frozen):
    """A parsed, not yet trusted, signal event: event + content + delegation tag."""

    event: NostrEvent
    signal: Signal
    delegation: Delegation


# ---------------------------------------------------------------- anchors


class AnchorProof(_Frozen):
    """Opaque proof that `digest` existed no later than some time. `data` is kind-specific.

    kind "ots": base64 of a detached .ots file. kind "bsc-commit": JSON batch proof. kind "tsa":
    JSON batch proof with k RFC 3161 tokens (see `sa_core.tsa.TsaProofData`).
    """

    kind: str = Field(min_length=1, max_length=32)
    digest: Hex64  # the stamped 32 bytes, normally a Nostr event id
    data: str


# ---------------------------------------------------------------- power allocation


class PowerAllocation(_Frozen):
    """Content of kind 9332. Whole split in basis points, signed by the SA address (EIP-191)."""

    v: Literal[1] = 1
    address: Address
    nonce: int = Field(ge=0)
    governance: Bps = 0
    providers: dict[Hex64, Annotated[int, Field(ge=1, le=BPS)]] = Field(default_factory=dict)
    sig: EvmSig

    @model_validator(mode="after")
    def _sum(self) -> PowerAllocation:
        total = self.governance + sum(self.providers.values())
        if total > BPS:
            raise ValueError(f"allocation sums to {total} bps, more than {BPS} (SEP-3.33)")
        return self

    def total_bps(self) -> int:
        return self.governance + sum(self.providers.values())


# ---------------------------------------------------------------- provider rule set


class Tier(_Frozen):
    """`min_power` in whole SA tokens; `delay_fraction` x timeframe is the release delay."""

    min_power: int = Field(ge=0)
    delay_fraction: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)

    @field_validator("delay_fraction")
    @classmethod
    def _grid(cls, v: float) -> float:
        if abs(v * 10_000 - round(v * 10_000)) > 1e-6:
            raise ValueError(f"delay_fraction {v} must have at most 4 decimals")
        return v


class PrivateRule(_Frozen):
    min_power: int = Field(ge=0)


class ProviderRuleSet(_Frozen):
    """Content of kind 37318 (d = provider pubkey): tiers or private, bound timeframe."""

    v: Literal[1] = 1
    provider: Hex64
    sa_address: Address
    timeframe: int = Field(gt=0)  # seconds, bound at registration (SEP-3.40)
    tiers: tuple[Tier, ...] | None = None
    private: PrivateRule | None = None
    sig: EvmSig

    @model_validator(mode="after")
    def _shape(self) -> ProviderRuleSet:
        if (self.tiers is None) == (self.private is None):
            raise ValueError("a rule set has exactly one of 'tiers' or 'private'")
        if self.tiers is not None:
            if not self.tiers:
                raise ValueError("'tiers' must not be empty; use 'private' for a closed provider")
            powers = [t.min_power for t in self.tiers]
            if len(set(powers)) != len(powers):
                raise ValueError(f"duplicate min_power in tiers: {sorted(powers)}")
            ordered = sorted(self.tiers, key=lambda t: t.min_power)
            for lo, hi in itertools.pairwise(ordered):
                if hi.delay_fraction > lo.delay_fraction:
                    raise ValueError(
                        f"tier min_power={hi.min_power} waits {hi.delay_fraction}x but "
                        f"min_power={lo.min_power} waits {lo.delay_fraction}x: more power must "
                        "never wait longer"
                    )
        return self


# ---------------------------------------------------------------- governance


class Proposal(_Frozen):
    v: Literal[1] = 1
    title: str = Field(min_length=1, max_length=200)
    options: tuple[str, ...] = Field(min_length=2)
    deadline: int = Field(gt=0)  # unix seconds; anchored time must be <= deadline
    snapshot_time: int = Field(gt=0)  # allocations / delegations are read as of this time
    snapshot_blocks: dict[int, int] = Field(default_factory=dict)  # chain_id -> block
    relays: tuple[str, ...] = Field(min_length=1)  # official relays (SEP-3.36)
    dust_floor: int = Field(default=0, ge=0)  # whole tokens

    @model_validator(mode="after")
    def _order(self) -> Proposal:
        if self.snapshot_time > self.deadline:
            raise ValueError(
                f"snapshot_time {self.snapshot_time} is after deadline {self.deadline}"
            )
        return self


class Vote(_Frozen):
    """Vote content (kind 9331). The voter is the SA address in the event's delegation tag."""

    v: Literal[1] = 1
    proposal: Hex64
    option: int = Field(ge=0)
    nonce: int = Field(ge=0)


class GovDelegation(_Frozen):
    """Content of kind 9333: hand a share of governance power to other SA addresses (one hop)."""

    v: Literal[1] = 1
    address: Address
    nonce: int = Field(ge=0)
    to: dict[Address, Annotated[int, Field(ge=1, le=BPS)]]
    sig: EvmSig

    @model_validator(mode="after")
    def _shares(self) -> GovDelegation:
        if self.address in self.to:
            raise ValueError(f"{self.address} cannot delegate to itself")
        total = sum(self.to.values())
        if total > BPS:
            raise ValueError(f"delegation shares sum to {total} bps, more than {BPS}")
        return self


# ---------------------------------------------------------------- bundle


class Reveal(_Frozen):
    """Plaintext of a committed signal, published after the fact."""

    commitment_id: Hex64  # event id of the kind-9318 commitment
    plaintext: str = Field(min_length=1)


class Bundle(_Frozen):
    """A provider's full signed history: events + reveals + anchor proofs (SEP-3.14)."""

    v: Literal[1] = 1
    provider: Hex64
    events: tuple[NostrEvent, ...]
    reveals: tuple[Reveal, ...] = ()
    proofs: tuple[AnchorProof, ...] = ()
    exported_at: int = Field(ge=0)
