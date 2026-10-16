"""SEP-2.6 cross-implementation check: the Go relay oracle and the Python oracle read the same SA
balances on BSC, Ethereum and zkSync (needs public RPCs and a Go toolchain; SA_ONLINE=1).

The Go side is `relay/cmd/sabalances`, which uses the relay's own `internal/chains` code and the
relay's example config; the Python side is `sa_core.chains` with the bundled registry. They are two
independent implementations of "balanceOf over JSON-RPC, normalised to 18 decimals, summed over the
registry", so equal per-chain values and sums mean neither mis-reads an address or a decimals setting.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sa_core.chains import BalanceOracle, ChainRegistry, JsonRpcBackend

pytestmark = [pytest.mark.e2e, pytest.mark.online]

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "relay"

# Real SA holders found with eth_getLogs on zkSync and cross-read on all three chains on 2026-10-13
# (each holds on BSC, Ethereum and zkSync), the BSC token contract itself, and an address with none.
ADDRESSES = [
    "0x00e7a2386f41bb36dd9fd1ab53cc68aa26b23db9",
    "0x7e1da8d6c52dced67ccbb2351844750cdf5487a5",
    "0xa54c30122d477589311ee28b85c0e8dc8f12680a",
    "0xfb981ed9a92377ca4d75d924b9ca06df163924fd",
    "0x000000000000000000000000000000000000dead",
]
CHAINS = {"bsc": 56, "ethereum": 1, "zksync": 324}
ATTEMPTS = 4  # a transfer between the two reads can differ by one block; read again


def go_balances() -> dict[str, dict[str, object]]:
    proc = subprocess.run(
        ["go", "run", "./cmd/sabalances", "-config", "sarelay.example.toml", *ADDRESSES],  # noqa: S607
        cwd=RELAY, capture_output=True, text=True, timeout=240, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, f"sabalances failed ({proc.returncode}): {proc.stderr.strip()}"
    out: dict[str, dict[str, object]] = json.loads(proc.stdout)
    return out


def test_go_and_python_oracles_agree_on_bsc_ethereum_and_zksync() -> None:
    registry = ChainRegistry.default()
    assert {c.name for c in registry.active} == set(CHAINS), (
        "Python registry must be exactly BSC, Ethereum, zkSync"
    )
    last = ""
    for _ in range(ATTEMPTS):
        oracle = BalanceOracle(registry, JsonRpcBackend(), ttl_s=0.0)
        go = go_balances()
        py = {a: oracle.power(a) for a in ADDRESSES}
        mismatches: list[str] = []
        for addr in ADDRESSES:
            per_go: dict[str, str] = go[addr]["per_chain"]  # type: ignore[assignment]
            for name, cid in CHAINS.items():
                if int(per_go[name]) != py[addr].per_chain[cid]:
                    mismatches.append(
                        f"{addr} {name}: go {per_go[name]} != python {py[addr].per_chain[cid]}"
                    )
            if int(str(go[addr]["total"])) != py[addr].total:
                mismatches.append(
                    f"{addr} total: go {go[addr]['total']} != python {py[addr].total}"
                )
        if not mismatches:
            break
        last = "\n".join(mismatches)
    else:
        pytest.fail(f"Go and Python oracles disagree after {ATTEMPTS} attempts:\n{last}")
    # positive control: the comparison is not trivially 0 == 0 on every chain
    for addr in ADDRESSES[:3]:
        assert all(py[addr].per_chain[cid] > 0 for cid in CHAINS.values()), (
            f"{addr} should hold SA on every chain"
        )
    assert py[ADDRESSES[3]].per_chain[56] > 0
    assert py[ADDRESSES[4]].total == 0
    for addr in ADDRESSES[:3]:
        print(
            addr,
            {n: py[addr].per_chain[c] // 10**18 for n, c in CHAINS.items()},
            "total",
            py[addr].tokens,
        )
