"""The API against a live in-process relay holding a signed world (public + private provider, followers, a vote)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from sa_api.app import create_app
from sa_api.config import ApiConfig, ConfigError
from sa_api.service import Service, follower_outcome, signal_status
from sa_core.models import Reveal, Signal
from sa_core.power import sign_allocation
from sa_core.trackrecord import Candle
from world import MINUTE, StubCandles, World, candles_around, make_report, write_ledger


def get(client: TestClient, path: str) -> Any:
    res = client.get(path)
    assert res.status_code == 200, res.text
    return res.json()


def by_mode(client: TestClient, mode: str) -> dict[str, Any]:
    found = [p for p in get(client, "/api/providers") if p["mode"] == mode]
    assert len(found) == 1
    result: dict[str, Any] = found[0]
    return result


def test_providers_come_from_the_relay_rule_sets_with_real_followers_and_power(
    client: TestClient, world: World
) -> None:
    pub = by_mode(client, "public")
    assert pub["pubkey"] == world.pub.pubkey
    assert pub["sa_address"] == world.pub.address
    assert pub["name"] == "atlas-ema"  # the strategy id of its chain, until a label is configured
    assert pub["timeframe_s"] == 3600
    assert pub["tiers"] == [
        {"min_power": 100_000, "delay_fraction": 0.0},
        {"min_power": 10_000, "delay_fraction": 0.5},
        {"min_power": 0, "delay_fraction": 2.0},
    ]  # best tier first: release[].tier indexes this list
    assert pub["followers"] == 2  # whale and mid allocate to it
    assert pub["power_backing"] == pytest.approx(1_000_000 * 0.6 + 20_000 * 0.7)
    priv = by_mode(client, "private")
    assert priv["tiers"] == [{"min_power": 10_000, "delay_fraction": 0.0}]
    assert priv["followers"] == 1
    assert priv["power_backing"] == pytest.approx(300_000)
    order = [p["pubkey"] for p in get(client, "/api/providers")]
    assert order[0] == world.pub.pubkey  # most power behind it first


def test_single_provider_endpoint_matches_the_list_item(client: TestClient, world: World) -> None:
    one = get(client, f"/api/providers/{world.pub.pubkey}")
    assert one == by_mode(client, "public")
    res = client.get("/api/providers/" + "0" * 64)
    assert res.status_code == 404 and "provider not found" in res.json()["error"]


def test_record_summary_is_computed_by_the_track_record_code(client: TestClient) -> None:
    pub = by_mode(client, "public")
    s = pub["record_summary"]
    assert s["trades"] == 2
    assert s["completeness"] == 1.0
    assert s["anchor_coverage"] == 1.0
    assert s["signatures_ok"] is True
    assert 0 <= s["win_rate"] <= 1


def test_record_has_equity_trades_and_verification(client: TestClient, world: World) -> None:
    rec = get(client, f"/api/providers/{world.pub.pubkey}/record")
    assert rec["verification"] == {
        "signatures_ok": True,
        "chain_complete": True,
        "anchored_pct": 100.0,
    }
    assert rec["equity"] and all(p["t"] > 10**12 for p in rec["equity"])
    assert rec["equity"][0]["equity"] == 10_000.0
    assert len(rec["trades"]) == 2
    assert {t["side"] for t in rec["trades"]} == {"buy"}
    assert rec["gaps"] == [] and rec["unrevealed_commitments"] == []


def test_a_dropped_signal_shows_as_a_gap_and_an_incomplete_chain(
    tmp_path: Path, farm_config: Path, relay: object
) -> None:
    from api_support import ThreadedRelay
    from world import farm_stamper

    assert isinstance(relay, ThreadedRelay)
    import time

    w = World(tmp_path, now_ms=int(time.time() * 1000) // MINUTE * MINUTE)
    w.build(farm_stamper(farm_config), gap=True)
    for ev in w.events:
        relay.inject(ev)
    cfg = ApiConfig(
        relays=(relay.url,), balances=w.balances, tsa_config=farm_config, tolerance_s=3600, cache_ttl_s=0.0,
    )  # fmt: skip
    svc = Service(cfg, candles=StubCandles(candles_around(w.now_ms - 20 * MINUTE, 120)))
    with TestClient(create_app(cfg, svc)) as c:
        rec = get(c, f"/api/providers/{w.pub.pubkey}/record")
        assert rec["gaps"] == [{"after_seq": 2, "missing": 1}]
        assert rec["verification"]["chain_complete"] is False
        summary = by_mode(c, "public")["record_summary"]
        assert summary["completeness"] < 1


def test_private_provider_without_reveals_counts_commitments_as_misses(
    client: TestClient, world: World
) -> None:
    rec = get(client, f"/api/providers/{world.priv.pubkey}/record")
    ids = {u["id"] for u in rec["unrevealed_commitments"]}
    assert ids == set(world.private_ids)
    assert {u["seq"] for u in rec["unrevealed_commitments"]} == {1, 2}
    assert rec["trades"] == []
    assert get(client, f"/api/providers/{world.priv.pubkey}/signals?limit=10") == []


def test_a_bundle_with_reveals_turns_private_commitments_into_a_record(
    client: TestClient, world: World, config: ApiConfig
) -> None:
    from sa_core.bundle import export_bundle, write_bundle

    commit_events = [e for e in world.events if e.id in world.private_ids]
    reveals = [Reveal(commitment_id=r.commitment_id, plaintext=r.plaintext) for r in world.reveals]
    assert config.bundles_dir is not None
    config.bundles_dir.mkdir()
    write_bundle(
        config.bundles_dir / "fox.json",
        export_bundle(world.priv.pubkey, commit_events, [], reveals),
    )
    (config.bundles_dir / "junk.json").write_text("{not json")
    rec = get(client, f"/api/providers/{world.priv.pubkey}/record")
    assert rec["unrevealed_commitments"] == []
    assert len(rec["trades"]) == 1
    sigs = get(client, f"/api/providers/{world.priv.pubkey}/signals?limit=10")
    assert [s["private"] for s in sigs] == [True, True]
    assert [s["seq"] for s in sigs] == [2, 1]
    assert all("release" not in s for s in sigs)


def test_signals_carry_one_provider_status_and_nothing_about_followers(
    client: TestClient, world: World
) -> None:
    sigs = get(client, f"/api/providers/{world.pub.pubkey}/signals?limit=3")
    assert [s["seq"] for s in sigs] == [4, 3, 2]
    s = sigs[0]
    assert s["private"] is False and s["id"] in world.public_ids
    assert s["anchored_ms"] is not None and s["anchored_ms"] > s["created_ms"]
    assert [x["status"] for x in sigs] == ["out"] * 3
    assert all(
        "release" not in x for x in sigs
    )  # no per-tier or per-follower data on the provider's view


def test_a_signal_without_a_time_stamp_says_it_waits_for_one(
    config: ApiConfig, world: World
) -> None:
    cfg = config.model_copy(update={"tsa_config": None, "tolerance_s": 0})
    with TestClient(
        create_app(
            cfg, Service(cfg, candles=StubCandles(candles_around(world.now_ms - 20 * MINUTE, 120)))
        )
    ) as c:
        sigs = get(c, f"/api/providers/{world.pub.pubkey}/signals")
    for s in sigs:
        assert (s["anchored_ms"] is None) == (s["status"] == "waiting_for_time_stamp")
        assert s["status"] in ("out", "waiting_for_time_stamp")


@given(st.one_of(st.none(), st.integers(min_value=1)))
def test_status_is_out_exactly_when_a_time_stamp_exists(anchored_ms: int | None) -> None:
    assert (signal_status(anchored_ms) == "out") is (anchored_ms is not None)
    assert signal_status(anchored_ms) in ("out", "waiting_for_time_stamp")


def dev_config(config: ApiConfig, world: World, **update: Any) -> ApiConfig:
    from sa_api.config import FollowerSource

    sig_id = world.public_ids[3]
    alloc = sign_allocation(world.mid.sa, 1, 3000, {world.pub.pubkey: 7000})
    home = world.write_follower_home("mid", world.mid, alloc, {sig_id: 1_234_567_890_123})
    broken = world.root / "broken-home"
    broken.mkdir()
    return config.model_copy(
        update={
            "followers": (FollowerSource(home=home), FollowerSource(home=broken)),
            "dev_views": True,
            **update,
        }
    )


def test_dev_views_off_hide_the_timeline_route_and_report_the_flag(
    config: ApiConfig, world: World
) -> None:
    cfg = dev_config(config, world, dev_views=False)
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert get(c, "/api/network")["dev_views"] is False
        res = c.get(f"/api/dev/providers/{world.pub.pubkey}/releases")
        assert res.status_code == 404 and "route not found" in res.json()["error"]
        for path in (
            f"/api/providers/{world.pub.pubkey}/signals",
            f"/api/providers/{world.pub.pubkey}/export?format=csv",
        ):
            assert "release" not in c.get(path).text
        assert "1234567890123" not in c.get(f"/api/providers/{world.pub.pubkey}/signals").text


def test_default_config_has_dev_views_off() -> None:
    assert ApiConfig().dev_views is False


def test_dev_views_on_serve_the_tier_schedule_and_observed_receipts(
    config: ApiConfig, world: World
) -> None:
    cfg = dev_config(config, world)
    sig_id = world.public_ids[3]
    svc = Service(cfg, candles=StubCandles(candles_around(world.now_ms - 20 * MINUTE, 120)))
    with TestClient(create_app(cfg, svc)) as c:
        assert get(c, "/api/network")["dev_views"] is True
        sigs = get(c, f"/api/dev/providers/{world.pub.pubkey}/releases")
        newest = next(s for s in sigs if s["id"] == sig_id)
        assert newest["status"] == "out"
        # mid has 14,000 SA with the provider: tier index 1 (>= 10,000, delay 0.5 s here); that tier shows the real
        # receipt time and the other tiers follow from the receipt time it implies
        rel = {r["tier"]: r["at_ms"] for r in newest["release"]}
        assert rel[1] == 1_234_567_890_123
        assert {r["tier"]: r["observed"] for r in newest["release"]} == {
            0: False,
            1: True,
            2: False,
        }
        assert rel[0] == 1_234_567_890_123 - 500 and rel[2] == 1_234_567_890_123 - 500 + 2000
        older = next(s for s in sigs if s["id"] == world.public_ids[0])
        assert {r["tier"]: r["at_ms"] for r in older["release"]}[1] != 1_234_567_890_123
        assert [r["observed"] for r in older["release"]] == [
            False
        ] * 3  # computed from the schedule
        ats = [r["at_ms"] for r in older["release"]]
        assert ats == sorted(ats)
        # relay time_scale 1/3600 and timeframe 3600 s: tier 0 now, tier 1 0.5 s later, tier 2 (2x) 2 s later
        r0 = {r["tier"]: r["at_ms"] for r in older["release"]}
        assert r0[1] - r0[0] == 500 and r0[2] - r0[0] == 2000 and r0[0] == older["created_ms"]
        assert c.get(f"/api/dev/providers/{'1' * 64}/releases").status_code == 404


def sample_signal(seq: int, instrument: str = "BINANCE:BTCUSDT") -> str:
    return Signal(
        strategy_id="s", seq=seq, prev=None if seq == 1 else "a" * 64, instrument=instrument,
        side="buy", action="open", size_fraction=0.5, order_type="market", timeframe=3600,
        created_ms=1_700_000_000_000 + seq,
    ).model_dump_json()  # fmt: skip


def follower_client(config: ApiConfig, world: World, rows: list[dict[str, Any]]) -> TestClient:
    alloc = sign_allocation(world.mid.sa, 1, 3000, {world.pub.pubkey: 7000})
    home = world.write_follower_home("me", world.mid, alloc, {})
    (home / "follower" / "ledger.sqlite").unlink()
    write_ledger(home, rows)
    cfg = config.model_copy(update={"me_home": home})
    return TestClient(create_app(cfg, Service(cfg)))


def test_my_signals_come_from_the_followers_own_ledger_with_tier_delay_and_outcome(
    config: ApiConfig, world: World
) -> None:
    pk = world.pub.pubkey
    other = "9" * 64
    rows: list[dict[str, Any]] = [
        {"event_id": "e1", "provider": pk, "received_ms": 1_000, "ref": "r1", "seq": 1,
         "created_ms": 900, "signal_json": sample_signal(1), "executed": True, "exec_reason": "working order"},
        {"event_id": "e2", "provider": pk, "received_ms": 3_000, "ref": "r2", "seq": 2,
         "created_ms": 2_900, "signal_json": sample_signal(2), "executed": False,
         "exec_reason": "not enough cash for the minimum quantity"},
        {"event_id": "e3", "provider": pk, "received_ms": 2_000, "status": "rejected",
         "reason": "gap: seq 4 does not follow 2"},
        {"event_id": "e4", "provider": other, "received_ms": 500, "ref": "r4", "seq": 1,
         "signal_json": "not json", "status": "pending"},
    ]  # fmt: skip
    with follower_client(config, world, rows) as c:
        got = get(c, "/api/me/signals")
        assert [g["received_ms"] for g in got] == [3_000, 2_000, 1_000, 500]  # newest first
        by_id = {g["id"]: g for g in got}
        executed, no_cash = by_id["r1"], by_id["r2"]
        # mid has 14,000 SA with the provider: tier index 1, delay 0.5 s at time scale 1/3600
        assert (executed["tier"], executed["delay_ms"]) == (1, 500)
        assert (executed["outcome"], executed["reason"]) == ("executed", "working order")
        assert (executed["instrument"], executed["side"], executed["action"]) == (
            "BINANCE:BTCUSDT",
            "buy",
            "open",
        )
        assert no_cash["outcome"] == "rejected" and "not enough cash" in no_cash["reason"]
        rej = by_id["e3"]
        assert (rej["outcome"], rej["reason"], rej["seq"], rej["instrument"]) == (
            "rejected",
            "gap: seq 4 does not follow 2",
            None,
            None,
        )
        unknown = by_id[
            "r4"
        ]  # a provider whose rule set the relay does not hold: no tier, no delay
        assert (
            unknown["outcome"],
            unknown["tier"],
            unknown["delay_ms"],
            unknown["instrument"],
        ) == ("pending", None, None, None)
        assert len(get(c, "/api/me/signals?limit=2")) == 2
        assert c.get("/api/me/signals?limit=0").status_code == 422


def test_my_signals_list_no_other_follower_and_are_empty_for_another_wallet(
    config: ApiConfig, world: World
) -> None:
    rows: list[dict[str, Any]] = [
        {"event_id": "e1", "provider": world.pub.pubkey, "received_ms": 1_000, "ref": "r1"}
    ]
    with follower_client(config, world, rows) as c:
        got = get(c, "/api/me/signals")
        assert len(got) == 1
        assert set(got[0]) == {
            "id", "provider", "seq", "received_ms", "created_ms", "tier", "delay_ms",
            "outcome", "reason", "instrument", "side", "action",
        }  # fmt: skip
        assert "follower" not in json.dumps(got) and "release" not in got[0]


def test_my_signals_without_a_ledger_are_empty_and_without_identity_a_404(
    config: ApiConfig, world: World, client: TestClient
) -> None:
    res = client.get("/api/me/signals")
    assert res.status_code == 404 and "--me-home" in res.json()["error"]
    home = world.write_follower_home("noledger", world.mid, None, {})
    (home / "follower" / "ledger.sqlite").unlink()
    cfg = config.model_copy(update={"me_home": home})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert get(c, "/api/me/signals") == []
    empty = world.root / "empty"
    empty.mkdir()
    cfg = config.model_copy(update={"me_home": empty})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        res = c.get("/api/me/signals")
        assert res.status_code == 404 and "identity.json" in res.json()["error"]


def test_my_signals_of_a_wallet_session_for_another_address_are_empty(
    config: ApiConfig, world: World
) -> None:
    import asyncio

    rows: list[dict[str, Any]] = [
        {"event_id": "e1", "provider": world.pub.pubkey, "received_ms": 1_000, "ref": "r1"}
    ]
    with follower_client(config, world, rows):
        pass  # writes the ledger under <root>/me
    cfg = config.model_copy(update={"me_home": world.root / "me"})
    svc = Service(cfg)
    assert len(asyncio.run(svc.my_signals(10, world.mid.address))) == 1
    assert asyncio.run(svc.my_signals(10, "0x" + "1" * 40)) == []


def test_a_queued_close_is_pending_until_the_exchange_places_or_the_follower_drops_it(
    config: ApiConfig, world: World
) -> None:
    from sa_engine.network.executor import Execution
    from sa_engine.network.ledger import Ledger

    pk = world.pub.pubkey
    queued = "queued for the Binance testnet node; not at the exchange yet"
    rows: list[dict[str, Any]] = [
        {"event_id": f"e{i}", "provider": pk, "received_ms": i * 1_000, "ref": f"r{i}", "seq": i,
         "signal_json": sample_signal(i), "executed": False, "queued": True, "exec_reason": queued}
        for i in (1, 2, 3)
    ]  # fmt: skip
    alloc = sign_allocation(world.mid.sa, 1, 3000, {pk: 7000})
    home = world.write_follower_home("me", world.mid, alloc, {})
    (home / "follower" / "ledger.sqlite").unlink()
    write_ledger(home, rows)
    cfg = config.model_copy(update={"me_home": home})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        by_id = {g["id"]: g for g in get(c, "/api/me/signals")}
        assert {k: (v["outcome"], v["reason"]) for k, v in by_id.items()} == {
            f"r{i}": ("pending", queued) for i in (1, 2, 3)
        }  # waiting, not rejected
        ledger = Ledger(home / "follower" / "ledger.sqlite")
        ledger.set_execution(
            Execution("e2", True, reason="accepted by the Binance testnet", venue_order_id="7")
        )
        ledger.set_execution(
            Execution("e3", False, reason="timeout: the entry order did not fill; close dropped")
        )
        ledger.close()
        by_id = {g["id"]: g for g in get(c, "/api/me/signals")}
        assert (by_id["r1"]["outcome"], by_id["r1"]["reason"]) == ("pending", queued)
        assert (by_id["r2"]["outcome"], by_id["r2"]["reason"]) == (
            "executed",
            "accepted by the Binance testnet",
        )
        assert by_id["r3"]["outcome"] == "rejected" and "close dropped" in by_id["r3"]["reason"]


def test_my_signals_survive_a_corrupt_ledger(config: ApiConfig, world: World) -> None:
    home = world.write_follower_home("bad", world.mid, None, {})
    (home / "follower" / "ledger.sqlite").write_bytes(b"not a database")
    cfg = config.model_copy(update={"me_home": home})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert get(c, "/api/me/signals") == []


@given(
    status=st.sampled_from(["accepted", "rejected", "pending"]),
    placed=st.booleans(),
    queued=st.booleans(),
    reason=st.text(max_size=20),
    exec_reason=st.text(max_size=20),
)
def test_follower_outcome_is_executed_only_for_a_placed_accepted_signal(
    status: str, placed: bool, queued: bool, reason: str, exec_reason: str
) -> None:
    outcome, why = follower_outcome(status, placed, queued, reason, exec_reason)
    assert (outcome == "executed") is (status == "accepted" and placed)
    assert (outcome == "pending") is (
        status == "pending" or (status == "accepted" and queued and not placed)
    )
    if status == "rejected":
        assert why == reason


def test_unknown_provider_signals_and_record_are_404(client: TestClient) -> None:
    for path in ("record", "signals"):
        res = client.get(f"/api/providers/{'1' * 64}/{path}")
        assert res.status_code == 404 and res.json()["error"].startswith("provider not found")
    assert client.get("/api/providers/x/signals?limit=0").status_code == 422


def test_backtests_come_from_engine_report_files(client: TestClient, config: ApiConfig) -> None:
    assert config.backtests_dir is not None
    config.backtests_dir.mkdir()
    report = make_report()
    (config.backtests_dir / "ema-week.json").write_text(report.model_dump_json())
    (config.backtests_dir / "broken.json").write_text("{}")
    items = get(client, "/api/backtests")
    assert [b["id"] for b in items] == ["ema-week"]
    b = items[0]
    assert (b["strategy_name"], b["start"], b["end"], b["trades"], b["win_rate"], b["max_dd_pct"]) == (
        "ema", "2026-09-01", "2026-09-08", 4, 0.75, 2.9,
    )  # fmt: skip
    assert len(b["equity"]) == 300 and b["equity"][-1]["equity"] == 10_999.0
    assert get(client, "/api/backtests/ema-week") == b
    assert client.get("/api/backtests/nope").status_code == 404


def test_backtests_are_empty_without_a_directory(config: ApiConfig, world: World) -> None:
    cfg = config.model_copy(update={"backtests_dir": None})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert get(c, "/api/backtests") == []


def test_me_shows_the_configured_identity_with_balances_and_allocations(
    config: ApiConfig, world: World
) -> None:
    alloc = sign_allocation(world.mid.sa, 1, 3000, {world.pub.pubkey: 7000})
    home = world.write_follower_home("me", world.mid, alloc, {})
    cfg = config.model_copy(update={"me_home": home})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        me = get(c, "/api/me")
        assert me == {
            "sa_address": world.mid.address,
            "signed_in": False,
            "token_power": 20_000.0,
            "balances": [{"chain": "fixture", "balance": 20_000.0}],
            "allocations": {"governance_bps": 3000, "providers": {world.pub.pubkey: 7000}},
        }


def test_me_without_identity_is_a_404_that_says_what_to_do(
    client: TestClient, config: ApiConfig, world: World
) -> None:
    res = client.get("/api/me")
    assert res.status_code == 404 and "--me-home" in res.json()["error"]
    empty = world.root / "empty"
    empty.mkdir()
    cfg = config.model_copy(update={"me_home": empty})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        res = c.get("/api/me")
        assert res.status_code == 404 and "identity.json" in res.json()["error"]


def test_me_without_allocation_has_zero_shares(config: ApiConfig, world: World) -> None:
    home = world.write_follower_home("noalloc", world.whale, None, {})
    cfg = config.model_copy(update={"me_home": home})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        me = get(c, "/api/me")
        assert me["allocations"] == {"governance_bps": 0, "providers": {}}
        assert me["token_power"] == 1_000_000.0


def test_governance_tally_weights_votes_by_governance_power(
    client: TestClient, world: World
) -> None:
    props = get(client, "/api/governance/proposals")
    assert len(props) == 1
    p = props[0]
    assert (
        p["id"] == world.proposal_id
        and p["title"] == "Ship the demo?"
        and p["options"] == ["Yes", "No"]
    )
    # whale: 10% of 1,000,000 SA on governance votes Yes; mid: 30% of 20,000 votes No
    assert p["tally"] == {"Yes": 100_000.0, "No": 6_000.0}
    assert p["complete"] is True and len(p["relays_answered"]) == 1
    assert p["deadline"].endswith("Z") and "T" in p["deadline"]


def test_network_reports_relays_and_chains(client: TestClient, config: ApiConfig) -> None:
    net = get(client, "/api/network")
    assert net["relays"] == [{"url": config.relays[0], "ok": True}]
    assert net["chains"] == [{"name": "fixture", "ok": True, "block": None}]
    assert net["time_scale"] == config.time_scale == pytest.approx(1 / 3600)


def test_network_marks_a_dead_relay_not_ok(config: ApiConfig, world: World) -> None:
    cfg = config.model_copy(update={"relays": ("ws://127.0.0.1:1",)})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert get(c, "/api/network")["relays"] == [{"url": "ws://127.0.0.1:1", "ok": False}]
        assert get(c, "/api/providers") == []  # nothing readable: an empty list, not an error


AUTH_ROUTES = {"/api/auth/nonce", "/api/auth/verify", "/api/auth/logout"}
DESIGNER_ROUTES = {
    "/api/strategy/validate",
    "/api/backtests/run",
}  # F4: validate is pure, run is loopback-only


def test_only_the_sign_in_routes_change_state_and_none_answers_2xx_unauthenticated(
    client: TestClient, config: ApiConfig
) -> None:
    """SEP-1.8: the only non-GET routes are /api/auth/* and the two strategy-designer routes (tests/test_f4_designer.py
    covers those); nothing else accepts a write verb, and the ones that do
    answer 2xx only for a valid signature (verify) or a valid session (logout). `nonce` is the unauthenticated start of
    the sign-in: it only reserves one bounded single-use nonce and changes nothing a request can use without a signature."""
    app = create_app(config)
    writes = {
        (m, r.path)  # type: ignore[attr-defined]
        for r in app.routes
        for m in getattr(r, "methods", set())
        if m not in {"GET", "HEAD"}
    }
    assert writes == {("POST", p) for p in AUTH_ROUTES | DESIGNER_ROUTES}
    for path in (
        "/api/providers",
        "/api/me",
        "/api/backtests",
        "/api/network",
        "/api/governance/proposals",
        "/api/auth",
        "/api/auth/other",
    ):
        for verb in ("post", "put", "patch", "delete"):
            assert getattr(client, verb)(path).status_code in (404, 405), f"{verb} {path}"
    for path in AUTH_ROUTES:
        for verb in ("put", "patch", "delete"):
            assert getattr(client, verb)(path).status_code == 405, f"{verb} {path}"
    garbage = {"message": "x", "signature": "0x" + "00" * 65}
    assert client.post("/api/auth/verify", json=garbage).status_code in (400, 401)
    assert client.post("/api/auth/logout").status_code == 401
    assert client.get("/api/nothing/here").json() == {"error": "route not found: /api/nothing/here"}


def test_json_dump_of_a_response_is_stable(client: TestClient) -> None:
    a = json.dumps(client.get("/api/providers").json(), sort_keys=True)
    b = json.dumps(client.get("/api/providers").json(), sort_keys=True)
    assert a == b


def test_labels_name_providers(config: ApiConfig, world: World) -> None:
    cfg = config.model_copy(update={"labels": {world.pub.pubkey: "Atlas EMA"}})
    with TestClient(
        create_app(
            cfg, Service(cfg, candles=StubCandles(candles_around(world.now_ms - 20 * MINUTE, 120)))
        )
    ) as c:
        assert by_mode(c, "public")["name"] == "Atlas EMA"


def test_a_provider_without_candles_still_shows_its_verification(
    config: ApiConfig, world: World
) -> None:
    from sa_engine.network.replay import ReplayError

    class NoCandles:
        def load(
            self, provider: str, instruments: list[str], first_ms: int, last_ms: int
        ) -> dict[str, list[Candle]]:
            raise ReplayError("no candles here; pass --replay-spec")

    svc = Service(config, candles=NoCandles())
    with TestClient(create_app(config, svc)) as c:
        rec = get(c, f"/api/providers/{world.pub.pubkey}/record")
        assert rec["equity"] == [] and rec["trades"] == []
        assert rec["verification"]["chain_complete"] is True
        assert by_mode(c, "public")["record_summary"]["trades"] == 0


def test_web_dist_is_served_at_root(config: ApiConfig, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Superalgos v2</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    cfg = config.model_copy(update={"web_dist": dist})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert "Superalgos v2" in c.get("/").text
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/api/network").status_code == 200  # the API wins over the static mount


def test_web_cache_headers_revalidate_index_and_pin_hashed_assets(
    config: ApiConfig, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Superalgos v2</title>")
    (dist / "favicon.svg").write_text("<svg/>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    cfg = config.model_copy(update={"web_dist": dist})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        for path in ("/", "/index.html", "/favicon.svg"):
            assert c.get(path).headers["cache-control"] == "no-cache", path
        asset = c.get("/assets/index-abc123.js")
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        # a conditional request is answered 304 and keeps the policy
        again = c.get("/", headers={"if-none-match": c.get("/").headers["etag"]})
        assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"
        assert (
            "cache-control" not in c.get("/api/network").headers
            or "immutable" not in c.get("/api/network").headers["cache-control"]
        )


def test_build_id_header_follows_the_served_index(config: ApiConfig, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    index = dist / "index.html"
    index.write_text('<!doctype html><head><meta name="sa-build" content="abc123"></head>')
    cfg = config.model_copy(update={"web_dist": dist})
    with TestClient(create_app(cfg, Service(cfg))) as c:
        assert c.get("/api/network").headers["x-sa-build"] == "abc123"
        assert c.head("/").headers["x-sa-build"] == "abc123"
        # a deploy replaces index.html: the very next response reports the new build
        index.write_text('<head><meta name="sa-build" content="def456"></head>')
        assert c.get("/api/network").headers["x-sa-build"] == "def456"
        # an index without the tag (old or dev build) reports nothing
        index.write_text("<!doctype html><title>x</title>")
        assert "x-sa-build" not in c.get("/api/network").headers


def test_no_web_dist_means_no_build_header(config: ApiConfig) -> None:
    with TestClient(create_app(config, Service(config))) as c:
        assert "x-sa-build" not in c.get("/api/network").headers


def test_missing_web_dist_is_a_configuration_error(config: ApiConfig, tmp_path: Path) -> None:
    cfg = config.model_copy(update={"web_dist": tmp_path / "nope"})
    with pytest.raises(ConfigError, match="bun run build"):
        create_app(cfg, Service(cfg))


def test_the_relay_cache_is_reused_within_the_ttl(config: ApiConfig, world: World) -> None:
    import asyncio

    calls: list[int] = []

    async def fetcher(relays: object) -> object:
        from sa_api.relay_data import fetch_snapshot

        calls.append(1)
        return await fetch_snapshot(config.relays)

    cfg = config.model_copy(update={"cache_ttl_s": 60.0})
    svc = Service(cfg, fetcher=fetcher)

    async def go() -> None:
        await svc.providers()
        await svc.providers()

    asyncio.run(go())
    assert len(calls) == 1


def test_when_every_relay_goes_down_the_last_good_snapshot_is_kept(
    config: ApiConfig, world: World
) -> None:
    import asyncio

    from sa_api.relay_data import Snapshot, fetch_snapshot

    state = {"down": False}

    async def fetcher(relays: object) -> Snapshot:
        return (
            Snapshot(taken_at=0.0, unreachable={"x": "down"})
            if state["down"]
            else await fetch_snapshot(config.relays)
        )

    svc = Service(
        config,
        fetcher=fetcher,
        candles=StubCandles(candles_around(world.now_ms - 20 * MINUTE, 120)),
    )

    async def go() -> int:
        before = len(await svc.providers())
        state["down"] = True
        return before + 100 * len(await svc.providers())

    assert asyncio.run(go()) == 2 + 200


def test_proposals_are_tallied_once_per_ttl(
    config: ApiConfig, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    cfg = config.model_copy(update={"cache_ttl_s": 60.0})
    svc = Service(cfg)
    calls: list[int] = []
    real = svc._tally_proposals

    async def counting() -> list[object]:
        calls.append(1)
        return await real()  # type: ignore[return-value]

    monkeypatch.setattr(svc, "_tally_proposals", counting)

    async def go() -> int:
        first = await svc.proposals()
        second = await svc.proposals()
        assert first == second
        return len(first)

    assert asyncio.run(go()) == 1
    assert len(calls) == 1
