"""Fixture balance oracle, shared with the relay's ``kind = "fixture"`` chain.

One JSON file ``{"0xaddress": "tokens"}`` (whole SA tokens, decimal strings) gives demo identities
different token power. The Go relay re-reads the file on every lookup; this module reads and writes the
same file for the Python side (provider entitlement, ``sa identity --power``, governance tallies).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sa_core.chains import BalanceOracle, ChainConfig, ChainRegistry, OracleError
from sa_core.models import TOKEN_UNIT, norm_addr

FIXTURE_CHAIN = ChainConfig(
    id=900_001,
    name="fixture",
    rpc="http://fixture.invalid/",  # never contacted: FileBackend answers
    contract="0x" + "00" * 20,
    decimals=18,
)


def read_balances(path: Path) -> dict[str, Decimal]:
    """``{address: whole tokens}``; a missing file is an error that says how to create it."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OracleError(
            f"balance fixture {path} not found; create it with `sa identity --demo --balances {path} --tokens N`"
        ) from exc
    except ValueError as exc:
        raise OracleError(f"balance fixture {path} is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise OracleError(f"balance fixture {path} must be a JSON object {{address: tokens}}, got {type(raw).__name__}")
    out: dict[str, Decimal] = {}
    for addr, tokens in raw.items():
        try:
            out[norm_addr(str(addr))] = Decimal(str(tokens))
        except (ValueError, InvalidOperation) as exc:
            raise OracleError(
                f"balance fixture {path}: {addr!r} -> {tokens!r} is not an address and a token amount"
            ) from exc
    return out


def set_balance(path: Path, address: str, tokens: Decimal | int | str) -> None:
    """Add or replace one balance (atomic write: the relay may be reading the file)."""
    path = Path(path)
    try:
        current = read_balances(path)
    except OracleError:
        if path.exists():
            raise
        current = {}
    amount = Decimal(str(tokens))
    if amount < 0:
        raise OracleError(f"balance for {address} must not be negative, got {amount}")
    current[norm_addr(address)] = amount
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({a: format(v, "f") for a, v in sorted(current.items())}, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


class FileBackend:
    """``BalanceBackend`` over the fixture file (re-read on every call, like the relay)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def balance_of(self, chain: ChainConfig, address: str, block: int | None) -> int:
        tokens = read_balances(self.path).get(norm_addr(address), Decimal(0))
        return int(tokens * TOKEN_UNIT)


def fixture_oracle(path: Path, *, ttl_s: float = 0.0) -> BalanceOracle:
    """A ``BalanceOracle`` reading the fixture file. TTL 0 so a balance edit shows at once."""
    return BalanceOracle(ChainRegistry(chains=(FIXTURE_CHAIN,)), FileBackend(path), ttl_s=ttl_s)
