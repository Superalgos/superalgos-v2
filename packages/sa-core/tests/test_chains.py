"""SEP-2.5: token power is the sum of SA balances over the chain registry; oracle is read-only."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sa_core.chains import (
    BALANCE_OF,
    BalanceOracle,
    ChainConfig,
    ChainRegistry,
    FixtureBackend,
    JsonRpcBackend,
    OracleError,
)

ADDR = "0x" + "ab" * 20
BSC, ETH, ZKS = 56, 1, 324


def test_default_registry_matches_the_seps() -> None:
    reg = ChainRegistry.default()
    by_name = {c.name: c for c in reg.chains}
    assert by_name["bsc"].contract == "0xfb981ed9a92377ca4d75d924b9ca06df163924fd"
    assert by_name["ethereum"].contract == "0xc17272c3e15074c55b810bceba02ba0c4481cd79"
    assert by_name["zksync"].id == 324 and by_name["zksync"].enabled
    assert by_name["zksync"].contract == "0xe3d85bd3363b6f0f91351591f9a8fd0d0a1145ed"
    assert [c.id for c in reg.active] == [56, 1, 324]
    assert reg.get(56).name == "bsc"
    with pytest.raises(KeyError, match="chain 99"):
        reg.get(99)


def test_registry_validation(tmp_path: Any) -> None:
    one = '[[chain]]\nid=1\nname="a"\nrpc="https://x"\ncontract="0x' + "1" * 40 + '"\n'
    assert ChainRegistry.from_toml(one).chains[0].decimals == 18
    with pytest.raises(ValueError, match="duplicate chain id"):
        ChainRegistry.from_toml(one + one)
    with pytest.raises(ValueError, match="no enabled chain"):
        ChainRegistry.from_toml(one + "enabled=false\n")
    with pytest.raises(OracleError, match="not valid TOML"):
        ChainRegistry.from_toml("[[chain")
    with pytest.raises(ValueError, match="contract"):
        ChainRegistry.from_toml(one.replace("1" * 40, "zz"))
    path = tmp_path / "chains.toml"
    path.write_text(one)
    assert ChainRegistry.from_file(path).chains[0].name == "a"


def _oracle(**kw: Any) -> tuple[BalanceOracle, FixtureBackend]:
    backend = FixtureBackend()
    return BalanceOracle(ChainRegistry.default(), backend, **kw), backend


def test_sep_2_6_power_is_the_sum_over_every_enabled_chain() -> None:
    oracle, backend = _oracle()
    backend.set_balance(BSC, ADDR, 5 * 10**18)
    backend.set_balance(ETH, ADDR.upper().replace("0X", "0x"), 7 * 10**18)
    backend.set_balance(ZKS, ADDR, 11 * 10**18)
    p = oracle.power(ADDR)
    assert p.total == 23 * 10**18 and p.tokens == 23
    assert p.per_chain == {56: 5 * 10**18, 1: 7 * 10**18, 324: 11 * 10**18}


def test_disabled_chains_do_not_count_and_decimals_are_normalised() -> None:
    reg = ChainRegistry(
        chains=(
            ChainConfig(id=1, name="a", rpc="https://x", contract=ADDR, decimals=6),
            ChainConfig(id=2, name="b", rpc="https://x", contract=ADDR, decimals=20),
            ChainConfig(id=3, name="off", rpc="https://x", contract=ADDR, enabled=False),
        )
    )
    backend = FixtureBackend(
        {1: {ADDR: [(0, 3_000_000)]}, 2: {ADDR: [(0, 5 * 10**20)]}, 3: {ADDR: [(0, 10**30)]}}
    )
    p = BalanceOracle(reg, backend).power(ADDR)
    assert p.per_chain == {1: 3 * 10**18, 2: 5 * 10**18}


def test_sep_2_7_cache_ttl_bounds_staleness() -> None:
    now = [0.0]
    oracle, backend = _oracle(ttl_s=600, clock=lambda: now[0])
    backend.set_balance(BSC, ADDR, 10**18)
    assert oracle.power(ADDR).total == 10**18
    backend.set_balance(BSC, ADDR, 2 * 10**18, from_block=1)
    now[0] = 599
    assert oracle.power(ADDR).total == 10**18  # still cached
    calls = backend.calls
    now[0] = 601
    assert oracle.power(ADDR).total == 2 * 10**18  # TTL elapsed: re-read, within one hour
    assert backend.calls == calls + 3
    oracle.invalidate()
    oracle.power(ADDR)
    assert backend.calls == calls + 6
    with pytest.raises(OracleError, match="one hour"):
        BalanceOracle(ChainRegistry.default(), backend, ttl_s=3601)


def test_snapshot_reads_are_pinned_and_unknown_chains_rejected() -> None:
    oracle, backend = _oracle()
    backend.set_balance(BSC, ADDR, 10**18, from_block=0)
    backend.set_balance(BSC, ADDR, 9 * 10**18, from_block=100)
    assert oracle.power(ADDR, {56: 50}).per_chain[56] == 10**18
    assert oracle.power(ADDR, {56: 150}).per_chain[56] == 9 * 10**18
    n = backend.calls
    oracle.power(ADDR, {56: 50})
    assert backend.calls == n  # pinned-block reads are immutable, latest reads are within TTL
    with pytest.raises(OracleError, match=r"chain\(s\) \[99\]"):
        oracle.power(ADDR, {99: 1})


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_json_rpc_backend_reads_balance_of_and_pins_the_block() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(42 * 10**18)})

    chain = ChainRegistry.default().get(56)
    backend = JsonRpcBackend(client=_client(handler))
    assert backend.balance_of(chain, ADDR, None) == 42 * 10**18
    assert backend.balance_of(chain, ADDR, 255) == 42 * 10**18
    call, tag = seen[0]["params"]
    assert seen[0]["method"] == "eth_call" and call["to"] == chain.contract
    assert call["data"] == BALANCE_OF + "0" * 24 + "ab" * 20 and tag == "latest"
    assert seen[1]["params"][1] == "0xff"


def test_json_rpc_backend_retries_then_gives_up_with_a_useful_error() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def flaky(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"result": "0x5"})

    chain = ChainRegistry.default().get(1)
    ok = JsonRpcBackend(client=_client(flaky), retries=2, sleep=sleeps.append)
    assert ok.balance_of(chain, ADDR, None) == 5
    assert sleeps == [0.5, 1.0]

    down = JsonRpcBackend(
        client=_client(lambda r: httpx.Response(500)), retries=1, sleep=sleeps.append
    )
    with pytest.raises(OracleError, match=r"failed after 2 attempts.*try another rpc"):
        down.balance_of(chain, ADDR, None)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"error": {"code": -32000, "message": "boom"}}, "RPC error for eth_call"),
        ({"result": "0x"}, "empty eth_call result"),
        ([1, 2], "non-object"),
    ],
)
def test_json_rpc_backend_rejects_bad_answers(response: Any, message: str) -> None:
    backend = JsonRpcBackend(client=_client(lambda r: httpx.Response(200, json=response)))
    with pytest.raises(OracleError, match=message):
        backend.balance_of(ChainRegistry.default().get(56), ADDR, None)


def test_oracle_is_read_only_and_validates_addresses() -> None:
    backend = JsonRpcBackend(client=_client(lambda r: httpx.Response(200, json={"result": "0x1"})))
    chain = ChainRegistry.default().get(56)
    for method in ("eth_sendRawTransaction", "eth_sendTransaction", "personal_sign"):
        with pytest.raises(OracleError, match="read-only"):
            backend.rpc(chain, method, [])
    with pytest.raises(OracleError, match="not 20 hex bytes"):
        backend.balance_of(chain, "0x1234", None)


@pytest.mark.online
def test_online_bsc_balance_and_zksync_contract_exist() -> None:
    """SEP-2.6 against live RPCs; also confirms the zkSync address in chains.toml is a contract."""
    backend = JsonRpcBackend()
    for chain in ChainRegistry.default().active:
        bal = backend.balance_of(chain, "0x" + "00" * 19 + "01", None)
        assert bal >= 0, chain.name
