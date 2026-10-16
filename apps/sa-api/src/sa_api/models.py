"""Response models: the dashboard contract, key for key (``web/src/api/types.ts`` is the reference).

Units: timestamps are epoch milliseconds; ``win_rate``, ``completeness``, ``anchor_coverage`` are fractions 0..1;
``*_pct`` are percent numbers; ``power_backing`` and ``token_power`` are whole SA tokens.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Tier(_Out):
    min_power: float
    delay_fraction: float


class RecordSummary(_Out):
    pnl_pct: float
    win_rate: float
    max_dd_pct: float
    trades: int
    completeness: float
    anchor_coverage: float  # on-time anchors only
    signatures_ok: bool
    excluded: int  # signals left out of the score (no candles, unfilled, rejected over exposure, unrevealed...)
    late_anchor: int  # signals anchored after the tolerance: scored as misses, not hidden
    unanchored: int  # published signals with no verified anchor: scored as misses, not hidden


class Provider(_Out):
    pubkey: str
    name: str
    sa_address: str
    mode: Literal["public", "private"]
    timeframe_s: int
    tiers: list[Tier]
    followers: int
    power_backing: float
    record_summary: RecordSummary


class EquityPoint(_Out):
    t: int
    equity: float


class Trade(_Out):
    open_t: int
    close_t: int
    side: Literal["buy", "sell"]
    entry: float
    exit: float
    pnl_pct: float


class Gap(_Out):
    after_seq: int
    missing: int


class UnrevealedCommitment(_Out):
    id: str
    seq: int | None
    committed_ms: int


class Verification(_Out):
    signatures_ok: bool
    chain_complete: bool
    anchored_pct: float


class TrackRecord(_Out):
    equity: list[EquityPoint]
    trades: list[Trade]
    gaps: list[Gap]
    unrevealed_commitments: list[UnrevealedCommitment]
    verification: Verification


class Release(_Out):
    tier: int
    at_ms: int
    observed: (
        bool  # at_ms is a follower's real receipt time (ledger), not computed from the schedule
    )


class Signal(_Out):
    id: str
    seq: int
    created_ms: int
    anchored_ms: int | None
    instrument: str
    side: Literal["buy", "sell"]
    action: Literal["open", "close", "adjust"]
    size_fraction: float
    private: bool
    # the provider's one status: "out" = signed, time-stamped (anchored) and on the relay; otherwise the missing step
    status: Literal["out", "waiting_for_time_stamp"]


class DevSignal(Signal):
    """Dev views only (``dev_views``): a signal with the release time of every tier, observed from follower ledgers."""

    release: list[Release]


class FollowerSignal(_Out):
    """One row of a follower's own ledger (``/api/me/signals``). ``delay_ms`` is the wall-clock delay the follower's
    tier schedules (delay fraction x timeframe x time scale), null when the tier cannot be resolved."""

    id: str
    provider: str
    seq: int | None
    received_ms: int
    created_ms: int | None
    tier: int | None
    delay_ms: int | None
    outcome: Literal["executed", "rejected", "pending"]
    reason: str
    instrument: str | None
    side: Literal["buy", "sell"] | None
    action: Literal["open", "close", "adjust"] | None


class Backtest(_Out):
    id: str
    strategy_name: str
    instrument: str
    timeframe: str  # the candles the strategy ran on
    fills: list[BacktestFill]  # every fill of the stored BacktestReport, in time order
    start: str
    end: str
    pnl_pct: float
    trades: int
    win_rate: float
    max_dd_pct: float
    equity: list[EquityPoint]


class BacktestFill(_Out):
    t: int
    side: Literal["buy", "sell"]
    price: float
    qty: float
    kind: Literal[
        "entry", "exit"
    ]  # walking the position: away from flat is an entry, toward flat an exit


class MarketSymbol(_Out):
    instrument: str  # the bare Binance symbol, BTCUSDT (candles accept BINANCE:BTCUSDT too)
    base: str
    quote: str


class MarketCandle(_Out):
    t: int  # open time, epoch ms
    o: float
    h: float
    l: float  # noqa: E741 - the contract's key
    c: float
    v: float


class ValuePoint(_Out):
    t: int
    value: float


class BandPoint(_Out):
    t: int
    upper: float
    mid: float
    lower: float


class ChainBalance(_Out):
    chain: str
    balance: float


class Allocations(_Out):
    governance_bps: int
    providers: dict[str, int]


class Me(_Out):
    sa_address: str
    signed_in: (
        bool  # True: the address of a wallet session (EIP-4361); False: the server's demo identity
    )
    token_power: float
    balances: list[ChainBalance]
    allocations: Allocations


class Proposal(_Out):
    id: str
    title: str
    options: list[str]
    deadline: str
    tally: dict[str, float]
    complete: bool
    relays_answered: list[str]


class RelayStatus(_Out):
    url: str
    ok: bool


class ChainStatus(_Out):
    name: str
    ok: bool
    block: int | None


class Network(_Out):
    time_scale: float  # the relay's time_scale (1 live; 1/speed in a demo replay)
    dev_views: bool  # the API serves the development views (per-tier delivery timeline)
    relays: list[RelayStatus]
    chains: list[ChainStatus]


class NonceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str


class NonceResponse(_Out):
    nonce: str
    message: str


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    signature: str


class AddressResponse(_Out):
    address: str


class ValidationIssue(_Out):
    """One problem in a strategy document: its JSON path, the 0-based column inside the expression (null outside), and what."""

    path: str
    column: int | None
    message: str


class ValidateResponse(_Out):
    ok: bool
    errors: list[ValidationIssue]


class JobStatus(_Out):
    """A backtest job. ``backtest_id`` is set once the report is in ``GET /api/backtests``."""

    id: str
    state: Literal["running", "done", "failed", "timeout"]
    started_ms: int
    finished_ms: int | None
    backtest_id: str | None
    error: str | None


class BacktestRunBody(BaseModel):
    """Body of ``POST /api/backtests/run``. ``strategy`` is the strategy document, checked by the strategy validator."""

    model_config = ConfigDict(extra="forbid")
    strategy: dict[str, Any]
    instrument: str
    timeframe: str | None = None
    start: str
    end: str
