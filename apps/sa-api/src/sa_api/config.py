"""API configuration: where the data lives. One JSON file (``--config``) or flags; every path is read-only input."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    """The configuration is unusable. The message says which value and what to do."""


class FollowerSource(BaseModel):
    """A follower whose real receipt times refine the release schedule of the dev timeline (``dev_views``).

    ``home`` is the follower's ``sa`` home (``identity.json`` and ``allocation.json`` give its tier with each provider);
    ``ledger`` is its SQLite ledger (default ``<home>/follower/ledger.sqlite``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    home: Path
    ledger: Path | None = None


class ApiConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relays: tuple[str, ...] = Field(
        default=("ws://127.0.0.1:7781",),
        description="relays read for rule sets, allocations, signals, proposals",
    )
    balances: Path | None = Field(
        default=None, description="fixture balances file (demo); default: the real chains"
    )
    tsa_config: Path | None = Field(
        default=None, description="same JSON as `sa --tsa-config`; default: public TSAs"
    )
    time_scale: float = Field(
        default=1.0, gt=0, description="the relay's time_scale: multiplies every tier delay"
    )
    delay_d_s: int = Field(
        default=7 * 24 * 3600, ge=0, description="allocation delay D, as configured on the relay"
    )
    governance_anchor: Literal["ots", "created-at"] = Field(
        default="ots", description="'created-at' trusts event times (DEMO ONLY, not tamper-proof)"
    )
    bundles_dir: Path | None = Field(
        default=None, description="provider bundles (*.json), merged with relay events"
    )
    backtests_dir: Path | None = Field(
        default=None, description="BacktestReport JSON files (`sa backtest --json`)"
    )
    replay_specs: dict[str, Path] = Field(
        default_factory=dict,
        description="provider pubkey -> replay.json, to score replayed history",
    )
    data_dir: Path | None = Field(
        default=None,
        description="Binance archive cache for replays; its catalog/ also serves /api/market/candles (default $SA_DATA_DIR or ./.sa-data)",
    )
    labels: dict[str, str] = Field(
        default_factory=dict, description="provider pubkey -> display name"
    )
    me_home: Path | None = Field(
        default=None, description="`sa` home whose identity /api/me shows (public files only)"
    )
    followers: tuple[FollowerSource, ...] = Field(
        default=(),
        description="dev views only: followers whose ledgers feed the per-tier timeline",
    )
    dev_views: bool = Field(
        default=False,
        description="serve the development views: /api/dev/* (per-tier delivery with other followers' receipts). Off in production; on in `sa demo`",
    )
    web_dist: Path | None = Field(
        default=None, description="built dashboard (web/dist), served at /"
    )
    siwe_domain: str | None = Field(
        default=None,
        description="the domain EIP-4361 messages must name (host[:port] users see); default: the request's Host header. Set it behind a proxy",
    )
    siwe_uri: str | None = Field(
        default=None, description="the URI line of sign-in messages; default scheme://domain"
    )
    siwe_chain_id: int = Field(default=1, ge=1, description="the Chain ID line of sign-in messages")
    siwe_nonce_ttl_s: int = Field(
        default=600,
        ge=1,
        le=600,
        description="how long a sign-in message stays valid (at most 10 minutes)",
    )
    session_ttl_s: int = Field(
        default=86_400, ge=60, description="wallet session lifetime (in memory, lost on restart)"
    )
    min_proposer_power: int = Field(
        default=1_000,
        ge=0,
        description="whole SA tokens a proposer's address must hold at the snapshot for a proposal to be shown",
    )
    nonce_rate_per_min: int = Field(
        default=30,
        ge=1,
        description="POST /api/auth/nonce requests allowed per client IP per minute (429 beyond)",
    )
    proxy_headers: bool = Field(
        default=False,
        description="trust X-Forwarded-For/-Proto from forwarded_allow_ips (behind a TLS-terminating proxy); the Secure cookie flag and the per-IP limit depend on it",
    )
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description="comma-separated proxy addresses whose X-Forwarded-* headers are trusted when proxy_headers is on",
    )
    binance_base_url: str = Field(
        default="https://api.binance.com",
        description="Binance public REST (no key) for /api/market/symbols and candles outside the local catalog",
    )
    market_rate_per_min: int = Field(
        default=120,
        ge=1,
        description="/api/market/* requests allowed per client IP per minute (429 beyond)",
    )
    market_symbols_ttl_s: float = Field(
        default=3600.0, ge=0, description="how long the Binance spot symbol list is cached"
    )
    market_candle_ttl_s: float = Field(
        default=2.0,
        ge=0,
        description="cache of candle windows that include the forming candle (closed history is cached for 10 minutes)",
    )
    market_lock_wait_s: float = Field(
        default=1.0,
        ge=0,
        description="how long a candle request waits for the data-dir lock (a download or catalog conversion) before asking Binance instead",
    )
    capital: float = Field(
        default=10_000.0,
        gt=0,
        description="equity curve units; also the starting capital of a backtest job",
    )
    backtest_timeout_s: float = Field(
        default=900.0,
        gt=0,
        description="POST /api/backtests/run: a job still running after this many seconds is killed and marked 'timeout'",
    )
    backtest_max_days: int = Field(
        default=400,
        ge=1,
        description="POST /api/backtests/run: longest window (end - start) a job may cover",
    )
    tolerance_s: int = Field(
        default=60, ge=0, description="how late a signal may be anchored and still count"
    )
    cache_ttl_s: float = Field(default=2.0, ge=0, description="relay snapshot cache")
    max_points: int = Field(default=300, ge=10, description="equity points returned per curve")

    @classmethod
    def load(cls, path: Path) -> ApiConfig:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"cannot read config {path}: {exc.strerror}; pass an existing file with --config"
            ) from exc
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise ConfigError(
                f"config {path} is invalid at {where or 'the top level'}: {first['msg']}"
            ) from exc
