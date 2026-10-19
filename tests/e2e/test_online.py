"""One real anchor round trip through public RFC 3161 TSAs (needs the network; SA_ONLINE=1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from e2e_helpers import query
from e2e_net import sa
from e2e_scenario import FollowerSpec, Run, run_network
from sa_core.bundle import read_bundle
from sa_core.governance import proof_from_attestation
from sa_core.models import ANCHOR_KIND, SIGNAL_KIND
from sa_core.tsa import TsaAnchor

pytestmark = [pytest.mark.e2e, pytest.mark.online]


def test_public_tsas_anchor_a_replayed_signal_and_the_proof_survives_the_relay(
    relay_binary: Path, data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    empty = tmp_path_factory.mktemp("none") / "no-config.json"
    empty.write_text("{}")  # defaults: the public endpoints and the bundled pinned roots
    run: Run = run_network(
        tmp_path_factory.mktemp("online"), relay_binary, speed=7200.0, window=("2026-09-01", "2026-09-02"),
        tsa_config=empty, data_dir=data_dir, followers=[FollowerSpec("t0", 1_000_000)], settle_s=3.0,
        label="online",
    )  # fmt: skip
    try:
        n = int(run.provider_summary["signals"])
        assert (
            n >= 1
            and run.provider_summary["anchored"] == n
            and run.provider_summary["anchor_errors"] == []
        )
        attestations = query(
            run.relay.url, [{"kinds": [ANCHOR_KIND], "authors": [run.provider.pubkey]}]
        )
        assert len(attestations) == n
        signals = {
            e.id
            for e in query(
                run.relay.url, [{"kinds": [SIGNAL_KIND], "authors": [run.provider.pubkey]}]
            )
        }
        anchor = TsaAnchor()  # bundled roots, offline verification
        for att in attestations:
            proof = proof_from_attestation(att)
            assert proof.digest in signals and proof.kind == "tsa"
            data = json.loads(proof.data)
            assert len(data["tokens"]) >= 2  # two distinct organisations
            bound = anchor.verify(proof, bytes.fromhex(proof.digest))
            assert abs(bound.not_after - att.created_at) < 120
        out = sa("verify", run.bundle)
        assert f"{n} anchored" in out.stdout
        assert read_bundle(run.bundle).proofs
    finally:
        run.relay.stop()


def test_sep_3_17_a_verified_signal_is_executed_by_the_nautilus_paper_node_on_live_data(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The real FollowerExecutor path: live Binance quotes, simulated fills, no keys, no real order."""
    import time

    import httpx

    from e2e_scenario import make_actor
    from sa_core.models import Signal
    from sa_core.signals import build_signal_event

    work = tmp_path_factory.mktemp("nautilus")
    balances = work / "balances.json"
    balances.write_text("{}")
    me = make_actor(work, "me", balances, 0)
    from e2e_net import PASSPHRASE
    from sa_cli.home import Home

    home = Home(me.home, environ={"SA_PASSPHRASE": PASSPHRASE, "SA_SCRYPT_N": "1024"})
    device = home.load_device()
    sig = Signal(
        strategy_id="live", seq=1, prev=None, instrument="BINANCE:BTCUSDT", side="buy", action="open",
        size_fraction=0.1, order_type="market", timeframe=3600, created_ms=int(time.time() * 1000),
        bar_open_ms=int(time.time() * 1000) - 3_600_000,
    )  # fmt: skip
    file = work / "signal.json"
    file.write_text(build_signal_event(device, sig, home.load_delegation(device)).model_dump_json())
    t0 = time.time()
    res = sa(
        "follow",
        "--signal-file",
        file,
        "--provider",
        me.pubkey,
        "--ledger",
        work / "l.sqlite",
        "--duration",
        "25",
        home=me.home,
    )
    elapsed = time.time() - t0
    assert "seq 1: accepted" in res.stdout, res.stdout
    paper = json.loads(res.stdout.strip().splitlines()[-1])["paper"]
    assert paper["orders_filled"] == 1 and paper["rejections"] == [] and paper["quotes_seen"] > 0
    price = float(
        httpx.get(
            "https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10
        ).json()["price"]
    )
    notional = paper["open_qty"] * price
    assert abs(notional - 0.1 * 10_000) < 15, (
        f"0.1 x 10,000 USDT of BTC at {price} is not {paper['open_qty']} BTC"
    )
    print(
        f"\nNautilus paper node: filled {paper['open_qty']} BTC (~{notional:.0f} USDT) from a verified signal file in {elapsed:.0f} s"
    )
