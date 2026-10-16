"""A private provider: entitled followers decrypt, everyone else gets nothing (SEP-3.26, SEP-3.27, SEP-3.28, SEP-3.12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from e2e_helpers import bundle_from_relay, query, replay_candles, verifiers_for
from e2e_net import sa
from e2e_scenario import Run, device_key
from sa_core.bundle import check_bundle, read_bundle
from sa_core.models import COMMITMENT_KIND, SIGNAL_KIND, Delegation
from sa_core.trackrecord import compute

pytestmark = pytest.mark.e2e

ENTITLED = ("rich", "mid")  # allocated power 1,000,000 and 20,000 SA, threshold 10,000
OTHERS = ("low", "zero")  # 5,000 SA allocated (below the threshold), and nothing


def n_signals(run: Run) -> int:
    return int(run.provider_summary["signals"])


def _delegation(run: Run, name: str) -> Delegation:
    actor = next(a for a in run.followers if a.name == name)
    return Delegation.model_validate_json((actor.home / "delegation.json").read_text())


def test_sep_3_26_entitled_followers_decrypt_verify_and_trade_every_signal(
    private_run: Run,
) -> None:
    n = n_signals(private_run)
    assert n >= 5
    commitments = {
        e.id
        for e in query(
            private_run.relay.url,
            [{"kinds": [COMMITMENT_KIND], "authors": [private_run.provider.pubkey]}],
        )
    }
    for name in ENTITLED:
        rows = private_run.ledger(name).rows()
        assert sorted(r.seq for r in rows if r.status == "accepted" and r.seq is not None) == list(
            range(1, n + 1)
        ), f"{name}: {[r.reason for r in rows]}"
        assert all(r.ref in commitments for r in rows), (
            "a private signal's chain reference is its commitment"
        )
        assert any(r.filled for r in rows)


def test_sep_3_27_everyone_else_receives_no_private_signal(private_run: Run) -> None:
    for name in OTHERS:
        assert private_run.ledger(name).rows() == [], f"{name} got a private signal"
        actor = next(a for a in private_run.followers if a.name == name)
        events = query(
            private_run.relay.url, [{"kinds": [SIGNAL_KIND], "authors": [private_run.provider.pubkey]}],
            device_key(actor), _delegation(private_run, name),
        )  # fmt: skip
        assert events == [], f"{name} can read {len(events)} private signal event(s) from the relay"
    anonymous = query(
        private_run.relay.url, [{"kinds": [SIGNAL_KIND], "authors": [private_run.provider.pubkey]}]
    )
    assert anonymous == []


def test_each_entitled_follower_reads_only_its_own_ciphertext(private_run: Run) -> None:
    n = n_signals(private_run)
    for name in ENTITLED:
        actor = next(a for a in private_run.followers if a.name == name)
        events = query(
            private_run.relay.url, [{"kinds": [SIGNAL_KIND], "authors": [private_run.provider.pubkey]}],
            device_key(actor), _delegation(private_run, name),
        )  # fmt: skip
        assert len(events) == n and all(("p", actor.pubkey) in e.tags for e in events)
        assert all("size_fraction" not in e.content for e in events)  # ciphertext on the wire


def test_the_relay_store_holds_no_private_plaintext(private_run: Run) -> None:
    hits = [
        p
        for p in private_run.relay.storage.rglob("*")
        if p.is_file() and b"size_fraction" in p.read_bytes()
    ]
    assert hits == [], f"plaintext signal fields found in the relay store: {hits}"


def test_sep_3_28_the_revealed_bundle_scores_and_matches_the_relay_rebuild(
    private_run: Run, data_dir: Path, tsa_config: Path
) -> None:
    bundle = read_bundle(private_run.bundle)
    check = check_bundle(bundle)
    assert check.complete and not check.unrevealed and len(bundle.reveals) == n_signals(private_run)
    trust = Path(json.loads(tsa_config.read_text())["trust_dir"])
    candles, verifiers = replay_candles(private_run.spec, data_dir), verifiers_for(trust)
    from_file = compute(bundle, candles, verifiers=verifiers)
    from_relay = compute(
        bundle_from_relay(
            private_run.relay.url, bundle.provider, bundle.exported_at, bundle.reveals
        ),
        candles,
        verifiers=verifiers,
    )
    assert from_file.to_json() == from_relay.to_json()
    assert from_file.report["signals"]["counted"] > 0
    out = private_run.workdir / "record.json"
    sa(
        "record",
        private_run.bundle,
        "--replay-spec",
        private_run.spec_path,
        "--tsa-config",
        tsa_config,
        "--data-dir",
        data_dir,
        "-o",
        out,
    )
    assert out.read_text() == from_file.to_json()


def test_sep_3_12_withholding_the_reveals_turns_every_signal_into_a_miss(
    private_run: Run, tmp_path: Path, tsa_config: Path, data_dir: Path
) -> None:
    bare = tmp_path / "bare.json"
    sa("provider", "export", "--state", private_run.state, "-o", bare)
    bundle = read_bundle(bare)
    assert bundle.reveals == () and len(check_bundle(bundle).unrevealed) == n_signals(private_run)
    out = tmp_path / "record.json"
    sa(
        "record",
        bare,
        "--replay-spec",
        private_run.spec_path,
        "--tsa-config",
        tsa_config,
        "--data-dir",
        data_dir,
        "-o",
        out,
        check=False,
    )
    # nothing can be scored without a reveal: the CLI says so instead of printing an empty record
    res = sa(
        "record",
        bare,
        "--replay-spec",
        private_run.spec_path,
        "--tsa-config",
        tsa_config,
        "--data-dir",
        data_dir,
        check=False,
    )
    assert res.returncode == 2 and "reveals" in res.stderr
