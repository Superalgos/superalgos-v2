"""Chain registry (chains.toml) and the read-only SA balance oracle.

Power is held as 18-decimal integers whatever a chain's own decimals. A balance that cannot be
read is an `OracleError`, never zero: callers must treat it as "unknown" (SEP-2.5).
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import TOKEN_UNIT, Address

BALANCE_OF = "0x70a08231"  # selector of balanceOf(address)
READ_ONLY_METHODS = frozenset({"eth_call", "eth_blockNumber", "eth_chainId"})
MAX_TTL_S = 3600  # SEP-2.7: a balance change shows in token power within 1 hour


class OracleError(Exception):
    """A balance could not be read or a registry is invalid. Never treat as zero."""


class ChainConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    rpc: str = Field(pattern=r"^https?://")
    contract: Address
    decimals: int = Field(default=18, ge=0, le=36)
    enabled: bool = True

    def to_units(self, raw: int) -> int:
        """Convert a raw token amount of this chain to 18-decimal power units."""
        if self.decimals <= 18:
            return int(raw * 10 ** (18 - self.decimals))
        return int(raw // 10 ** (self.decimals - 18))


class ChainRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chains: tuple[ChainConfig, ...]

    @model_validator(mode="after")
    def _unique(self) -> ChainRegistry:
        ids = [c.id for c in self.chains]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"duplicate chain id in registry: {dup}")
        if not any(c.enabled for c in self.chains):
            raise ValueError("chain registry has no enabled chain; enable at least one")
        return self

    @property
    def active(self) -> tuple[ChainConfig, ...]:
        return tuple(c for c in self.chains if c.enabled)

    def get(self, chain_id: int) -> ChainConfig:
        for c in self.chains:
            if c.id == chain_id:
                return c
        raise KeyError(
            f"chain {chain_id} is not in the registry (known: {[c.id for c in self.chains]})"
        )

    @classmethod
    def from_toml(cls, text: str) -> ChainRegistry:
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise OracleError(f"chains.toml is not valid TOML: {exc}") from exc
        return cls.model_validate({"chains": raw.get("chain", [])})

    @classmethod
    def from_file(cls, path: str | Path) -> ChainRegistry:
        return cls.from_toml(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def default(cls) -> ChainRegistry:
        """The registry shipped with the package (BSC, Ethereum, zkSync)."""
        return cls.from_toml(resources.files("sa_core").joinpath("chains.toml").read_text("utf-8"))


# ---------------------------------------------------------------- backends


class BalanceBackend(Protocol):
    def balance_of(self, chain: ChainConfig, address: str, block: int | None) -> int:
        """Raw balance (the chain's own decimals) at `block`, or latest when None."""
        ...


class FixtureBackend:
    """In-memory balances: {chain_id: {address: [(from_block, raw), ...]}}. Test and demo use."""

    def __init__(
        self, data: Mapping[int, Mapping[str, list[tuple[int, int]]]] | None = None
    ) -> None:
        self._data: dict[int, dict[str, list[tuple[int, int]]]] = {
            cid: {a.lower(): sorted(cp) for a, cp in per.items()}
            for cid, per in (data or {}).items()
        }
        self.calls = 0

    def set_balance(self, chain_id: int, address: str, raw: int, from_block: int = 0) -> None:
        cps = self._data.setdefault(chain_id, {}).setdefault(address.lower(), [])
        cps.append((from_block, raw))
        cps.sort()

    def balance_of(self, chain: ChainConfig, address: str, block: int | None) -> int:
        self.calls += 1
        value = 0
        for from_block, raw in self._data.get(chain.id, {}).get(address.lower(), []):
            if block is None or from_block <= block:
                value = raw
        return value


class JsonRpcBackend:
    """`eth_call balanceOf` over JSON-RPC. Timeouts, bounded retries, method allowlist."""

    def __init__(
        self,
        *,
        timeout_s: float = 8.0,
        retries: int = 2,
        backoff_s: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        self._client = client or httpx.Client(timeout=timeout_s)
        self._sleep = sleep

    def rpc(self, chain: ChainConfig, method: str, params: list[Any]) -> str:
        if method not in READ_ONLY_METHODS:
            raise OracleError(
                f"refusing RPC method {method!r}: the oracle is read-only "
                f"(allowed: {sorted(READ_ONLY_METHODS)})"
            )
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last = ""
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.post(chain.rpc, json=body, timeout=self.timeout_s)
                resp.raise_for_status()
                out = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}"
                if attempt < self.retries:
                    self._sleep(self.backoff_s * 2**attempt)
                continue
            if not isinstance(out, dict):
                raise OracleError(
                    f"{chain.name} RPC {chain.rpc} answered a non-object: {out!r:.80}"
                )
            if "error" in out:
                raise OracleError(f"{chain.name} RPC error for {method}: {out['error']}")
            return str(out.get("result", ""))
        raise OracleError(
            f"{chain.name} RPC {chain.rpc} failed after {self.retries + 1} attempts ({last}); "
            "try another rpc in chains.toml"
        )

    def balance_of(self, chain: ChainConfig, address: str, block: int | None) -> int:
        a = address.lower().removeprefix("0x")
        if len(a) != 40:
            raise OracleError(f"address {address!r} is not 20 hex bytes")
        call = {"to": chain.contract, "data": BALANCE_OF + "0" * 24 + a}
        result = self.rpc(chain, "eth_call", [call, "latest" if block is None else hex(block)])
        h = result.removeprefix("0x")
        if not h:
            raise OracleError(
                f"empty eth_call result: is {chain.contract} a contract on {chain.name}?"
            )
        return int(h, 16)


# ---------------------------------------------------------------- oracle


class Power(BaseModel):
    """Token power of one identity: 18-decimal units, with the per-chain breakdown."""

    model_config = ConfigDict(frozen=True)

    total: int
    per_chain: dict[int, int]

    @property
    def tokens(self) -> int:
        """Whole SA tokens (floor)."""
        return self.total // TOKEN_UNIT


class BalanceOracle:
    """Sums SA balances over the enabled chains, with a TTL cache for latest-block reads.

    Reads at an explicit block (snapshots) are immutable and cached for the process lifetime.
    """

    def __init__(
        self,
        registry: ChainRegistry,
        backend: BalanceBackend,
        *,
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= ttl_s <= MAX_TTL_S:
            raise OracleError(f"ttl_s={ttl_s} must be within 0..{MAX_TTL_S} (SEP-2.7: one hour)")
        self.registry = registry
        self.backend = backend
        self.ttl_s = ttl_s
        self._clock = clock
        self._latest: dict[tuple[int, str], tuple[float, int]] = {}
        self._pinned: dict[tuple[int, str, int], int] = {}

    def _balance(self, chain: ChainConfig, address: str, block: int | None) -> int:
        if block is not None:
            pkey = (chain.id, address, block)
            if pkey not in self._pinned:
                self._pinned[pkey] = chain.to_units(self.backend.balance_of(chain, address, block))
            return self._pinned[pkey]
        key = (chain.id, address)
        hit = self._latest.get(key)
        now = self._clock()
        if hit is not None and now - hit[0] < self.ttl_s:
            return hit[1]
        value = chain.to_units(self.backend.balance_of(chain, address, None))
        self._latest[key] = (now, value)
        return value

    def power(self, address: str, blocks: Mapping[int, int] | None = None) -> Power:
        """Total power at the given per-chain blocks (latest where a chain is not listed)."""
        addr = address.lower()
        blocks = blocks or {}
        known = {c.id for c in self.registry.chains}
        unknown = sorted(set(blocks) - known)
        if unknown:
            raise OracleError(f"snapshot names chain(s) {unknown} that are not in the registry")
        per_chain = {c.id: self._balance(c, addr, blocks.get(c.id)) for c in self.registry.active}
        return Power(total=sum(per_chain.values()), per_chain=per_chain)

    def invalidate(self) -> None:
        self._latest.clear()
