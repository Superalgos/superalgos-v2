"""The contract test: the shapes the web mock serves (``web/mock/contract.json``, same keys, types, units) against
live responses of the real API. If this fails, either the API or ``web/src/api/types.ts`` and the mock changed alone.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api_support import ThreadedRelay
from market_support import DAY_END, FakeBinance
from sa_api.app import create_app
from sa_api.config import ApiConfig, FollowerSource
from sa_api.market import MarketService
from sa_api.service import Service
from sa_core.models import Bundle
from sa_core.power import sign_allocation
from world import (
    MINUTE,
    StubCandles,
    World,
    candles_around,
    farm_stamper,
    make_report,
    write_ledger,
)

WEB_CONTRACT = Path(__file__).resolve().parents[3] / "web" / "mock" / "contract.json"
# A copy of web/mock/contract.json as of f7/web (106d69e), which added the market chart: the market endpoints, `timeframe`
# and `fills` on backtests, and the two export downloads (sign-in routes, `signed_in`, `observed`, `time_scale` came with
# wave3/web-fix, 84a8856). It is the reference until that branch is merged; then
# `test_the_copy_of_the_web_contract_is_current` fails if the two drift, and the copy can be deleted.
CONTRACT = Path(__file__).resolve().parent / "data" / "web-contract.json"
MAP_KEYS = {
    "providers",
    "tally",
}  # objects whose keys are data (pubkeys, option labels), not schema
EPOCH_MS = 10**11  # anything after 1973 in milliseconds; seconds would fail this
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


# ---------------------------------------------------------------- shapes


def kind(v: object) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int | float):
        return "number"
    if isinstance(v, str):
        return "string"
    raise AssertionError(f"not a JSON scalar: {v!r}")


def shape(v: object, key: str = "") -> dict[str, Any]:
    if isinstance(v, dict):
        if key in MAP_KEYS:
            return {"map": merge_all([shape(x) for x in v.values()])}
        return {"obj": {k: shape(x, k) for k, x in v.items()}}
    if isinstance(v, list):
        return {"arr": merge_all([shape(x, key) for x in v])}
    return {"leaf": {kind(v)}}


def merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    if a is None or b is None:
        return a or b
    assert a.keys() == b.keys(), f"shapes disagree: {a.keys()} vs {b.keys()}"
    (tag,) = a.keys()
    if tag == "leaf":
        return {"leaf": a["leaf"] | b["leaf"]}
    if tag == "obj":
        assert a["obj"].keys() == b["obj"].keys(), (
            f"keys differ: {sorted(a['obj'])} vs {sorted(b['obj'])}"
        )
        return {"obj": {k: merge(a["obj"][k], b["obj"][k]) for k in a["obj"]}}
    return {tag: merge(a[tag], b[tag])}


def merge_all(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    out: dict[str, Any] | None = None
    for it in items:
        out = merge(out, it)
    return out


def conforms(live: dict[str, Any] | None, ref: dict[str, Any] | None, path: str) -> None:
    """``live`` has exactly the keys of ``ref``, and only the JSON types ``ref`` allows."""
    if live is None:  # an empty list or map in the live data: nothing to compare
        return
    assert ref is not None, f"{path}: live data has items where the web fixture has none"
    assert live.keys() == ref.keys(), (
        f"{path}: live is a {next(iter(live))}, the fixture a {next(iter(ref))}"
    )
    (tag,) = live.keys()
    if tag == "leaf":
        assert live["leaf"] <= ref["leaf"], (
            f"{path}: live types {live['leaf']} not within fixture types {ref['leaf']}"
        )
    elif tag == "obj":
        assert live["obj"].keys() == ref["obj"].keys(), (
            f"{path}: live keys {sorted(live['obj'])} != fixture keys {sorted(ref['obj'])}"
        )
        for k in live["obj"]:
            conforms(live["obj"][k], ref["obj"][k], f"{path}.{k}")
    else:
        conforms(live[tag], ref[tag], f"{path}[]")


# ---------------------------------------------------------------- units (checked on the fixture and on live data)


def check_units(endpoint: str, value: object) -> None:
    """Fractions are 0..1, percents 0..100, times epoch ms, tiers best first: on every item of ``value``."""
    for item in items_of(endpoint, value):
        if endpoint in {"GET /api/providers", "GET /api/providers/{pubkey}"}:
            s = item["record_summary"]
            for fraction in ("win_rate", "completeness", "anchor_coverage"):
                assert 0 <= s[fraction] <= 1, f"{fraction}={s[fraction]} is not a 0..1 fraction"
            assert s["max_dd_pct"] >= 0
            assert item["mode"] in ("public", "private")
            mins = [t["min_power"] for t in item["tiers"]]
            assert mins == sorted(mins, reverse=True), (
                "tiers must be best (highest min_power) first"
            )
            assert item["timeframe_s"] > 0 and item["followers"] >= 0 and item["power_backing"] >= 0
        elif endpoint == "GET /api/providers/{pubkey}/record":
            assert 0 <= item["verification"]["anchored_pct"] <= 100, (
                "anchored_pct is a percent, 0..100"
            )
            assert all(pt["t"] > EPOCH_MS for pt in item["equity"]), (
                "equity t is epoch milliseconds"
            )
            for t in item["trades"]:
                assert EPOCH_MS < t["open_t"] <= t["close_t"]
                assert t["side"] in ("buy", "sell")
            assert all(g["after_seq"] >= 0 and g["missing"] >= 1 for g in item["gaps"])
            for u in item["unrevealed_commitments"]:
                assert re.fullmatch(r"[0-9a-f]{64}", u["id"]) and u["committed_ms"] > EPOCH_MS
                assert u["seq"] is None or u["seq"] >= 1
        elif endpoint == "GET /api/providers/{pubkey}/signals":
            assert item["created_ms"] > EPOCH_MS
            assert item["anchored_ms"] is None or item["anchored_ms"] > EPOCH_MS
            assert item["side"] in ("buy", "sell") and item["action"] in ("open", "close", "adjust")
            assert 0 < item["size_fraction"] <= 1
            assert item["status"] in ("out", "waiting_for_time_stamp")
            assert (item["anchored_ms"] is not None) == (item["status"] == "out")
            assert "release" not in item, "per-tier delivery is a dev view"
        elif endpoint == "GET /api/dev/providers/{pubkey}/releases":
            ats = [r["at_ms"] for r in item["release"]]
            assert ats == sorted(ats) and all(t > EPOCH_MS for t in ats)
            assert all(isinstance(r["observed"], bool) for r in item["release"])
        elif endpoint == "GET /api/me/signals":
            assert item["received_ms"] > EPOCH_MS
            assert item["outcome"] in ("executed", "rejected", "pending")
            assert item["delay_ms"] is None or item["delay_ms"] >= 0
            assert item["side"] in (None, "buy", "sell")
        elif endpoint in ("GET /api/backtests", "GET /api/backtests/{id}"):
            assert ISO_DAY.match(item["start"]) and ISO_DAY.match(item["end"])
            assert 0 <= item["win_rate"] <= 1 and item["max_dd_pct"] >= 0
            assert all(p["t"] > EPOCH_MS for p in item["equity"])
            assert item["timeframe"] in ("1m", "5m", "15m", "1h", "4h", "1d")
            ts = [f["t"] for f in item["fills"]]
            assert ts == sorted(ts) and all(t > EPOCH_MS for t in ts), (
                "fills in time order, epoch ms"
            )
            assert all(
                f["side"] in ("buy", "sell") and f["qty"] > 0 and f["price"] > 0
                for f in item["fills"]
            )
            assert all(f.get("kind", "entry") in ("entry", "exit") for f in item["fills"])
        elif endpoint == "GET /api/market/symbols":
            assert re.fullmatch(r"[A-Z0-9]{2,20}", item["instrument"]), "the bare Binance symbol"
            assert item["base"] and item["quote"]
        elif endpoint == "GET /api/market/candles":
            assert item["t"] > EPOCH_MS and item["h"] >= item["l"] and item["v"] >= 0
        elif endpoint == "GET /api/market/indicators":
            for series in item.values():
                assert all(p["t"] > EPOCH_MS for p in series)
        elif endpoint == "GET /api/providers/{pubkey}/export?format=bundle":
            assert {"v", "provider", "events", "reveals", "proofs", "exported_at"} <= set(
                json.loads(item)
            )
        elif endpoint == "GET /api/providers/{pubkey}/export?format=csv":
            assert item.splitlines()[0].startswith(
                "seq,id,created_ms,created_iso,anchored_ms,instrument,"
            )
        elif endpoint in ("GET /api/me", "GET /api/me (signed in)"):
            assert item["signed_in"] is (endpoint == "GET /api/me (signed in)")
            a = item["allocations"]
            assert item["token_power"] >= 0 and 0 <= a["governance_bps"] <= 10_000
            assert sum(a["providers"].values()) + a["governance_bps"] <= 10_000
        elif endpoint == "GET /api/governance/proposals":
            assert ISO_TS.match(item["deadline"]), f"deadline {item['deadline']!r} is not ISO 8601"
            assert set(item["tally"]) <= set(item["options"])
        elif endpoint == "POST /api/auth/nonce":
            assert (
                re.fullmatch(r"[A-Za-z0-9]{8,}", item["nonce"])
                and f"Nonce: {item['nonce']}" in item["message"]
            )
            assert "wants you to sign in with your Ethereum account:" in item["message"]
            assert "Version: 1" in item["message"] and "Issued At: " in item["message"]
        elif endpoint == "POST /api/auth/verify":
            assert re.fullmatch(r"0x[0-9a-fA-F]{40}", item["address"])
        elif endpoint == "POST /api/auth/logout":
            assert item == {}
        elif endpoint == "GET /api/network":
            assert item["time_scale"] > 0 and isinstance(item["dev_views"], bool)
            assert all(isinstance(c["block"], int | type(None)) for c in item["chains"])


def tier_indexes_are_valid(
    providers: list[dict[str, Any]], signals: dict[str, list[dict[str, Any]]]
) -> None:
    """``release[].tier`` (dev views) indexes the provider's own ``tiers`` list."""
    for p in providers:
        for s in signals.get(p["pubkey"], []):
            assert all(0 <= r["tier"] < len(p["tiers"]) for r in s["release"])


# ---------------------------------------------------------------- the test


@pytest.fixture
def reference() -> dict[str, Any]:
    ref: dict[str, Any] = json.loads(CONTRACT.read_text())
    return ref


def get(c: TestClient, path: str) -> Any:
    res = c.get(path)
    assert res.status_code == 200, f"{path}: {res.status_code} {res.text}"
    return res.json()


def test_the_copy_of_the_web_contract_is_current(reference: dict[str, Any]) -> None:
    """After wave3/web-fix is merged, web/mock/contract.json must be this copy, key for key."""
    web: dict[str, Any] = json.loads(WEB_CONTRACT.read_text())
    if "GET /api/market/candles" not in web:
        pytest.skip(
            "web/mock/contract.json predates f7/web (no market endpoints): "
            "the reference is apps/sa-api/tests/data/web-contract.json until that branch is merged"
        )
    assert web == reference, (
        "web/mock/contract.json changed: copy it to apps/sa-api/tests/data/web-contract.json"
    )


def test_the_fixture_itself_meets_the_unit_rules(reference: dict[str, Any]) -> None:
    for endpoint, example in reference.items():
        check_units(endpoint, example)


def test_live_responses_have_the_keys_types_and_units_of_the_web_fixture(
    reference: dict[str, Any], tmp_path: Path, farm_config: Path, relay: ThreadedRelay
) -> None:
    w = World(tmp_path, now_ms=int(time.time() * 1000) // MINUTE * MINUTE)
    w.build(
        farm_stamper(farm_config), reveal_private=False
    )  # the private provider has unrevealed commitments
    gappy = World(tmp_path / "gappy", now_ms=w.now_ms)
    (tmp_path / "gappy").mkdir()
    gappy.build(farm_stamper(farm_config), gap=True)  # a public chain with a hole in it
    for ev in [*w.events, *gappy.events]:
        relay.inject(ev)
    w.add_vote_round()
    relay.inject(w.events[-3])
    relay.inject(w.events[-2])
    relay.inject(w.events[-1])
    (tmp_path / "backtests").mkdir()
    (tmp_path / "backtests" / "ema-week.json").write_text(make_report().model_dump_json())
    alloc = sign_allocation(w.mid.sa, 1, 3000, {w.pub.pubkey: 7000})
    mid_home = w.write_follower_home("mid", w.mid, alloc, {})
    (mid_home / "follower" / "ledger.sqlite").unlink()
    write_ledger(
        mid_home,
        [
            {"event_id": "e1", "provider": w.pub.pubkey, "received_ms": w.now_ms, "ref": "r1", "seq": 1,
             "created_ms": w.now_ms, "executed": True, "exec_reason": "working order"},
            {"event_id": "e2", "provider": w.pub.pubkey, "received_ms": w.now_ms + 1, "status": "rejected",
             "reason": "gap: seq 3 does not follow 1"},
        ],
    )  # fmt: skip
    cfg = ApiConfig(
        relays=(relay.url,), balances=w.balances, tsa_config=farm_config, governance_anchor="created-at", delay_d_s=0,
        tolerance_s=3600, time_scale=1 / 3600, cache_ttl_s=0.0, backtests_dir=tmp_path / "backtests", me_home=mid_home,
        followers=(FollowerSource(home=mid_home),), dev_views=True,
    )  # fmt: skip
    svc = Service(cfg, candles=StubCandles(candles_around(w.now_ms - 20 * MINUTE, 120)))
    live: dict[str, list[Any]] = {k: [] for k in reference}
    binance = FakeBinance()
    market = MarketService(cfg, http=binance.client())
    with TestClient(create_app(cfg, svc, market=market)) as c:
        providers = get(c, "/api/providers")
        live["GET /api/providers"].append(providers)
        signals: dict[str, list[dict[str, Any]]] = {}
        dev_signals: dict[str, list[dict[str, Any]]] = {}
        for p in providers:
            pk = p["pubkey"]
            live["GET /api/providers/{pubkey}"].append(get(c, f"/api/providers/{pk}"))
            live["GET /api/providers/{pubkey}/record"].append(get(c, f"/api/providers/{pk}/record"))
            signals[pk] = get(c, f"/api/providers/{pk}/signals?limit=3")
            live["GET /api/providers/{pubkey}/signals"].append(signals[pk])
            dev_signals[pk] = get(c, f"/api/dev/providers/{pk}/releases?limit=3")
            live["GET /api/dev/providers/{pubkey}/releases"].append(dev_signals[pk])
        # the gappy chain too: its record carries the optional `gaps` list
        gappy_rules = [p for p in get(c, "/api/providers") if p["pubkey"] == gappy.pub.pubkey]
        assert gappy_rules, "the gappy provider should be listed"
        live["GET /api/providers/{pubkey}/record"].append(
            get(c, f"/api/providers/{gappy.pub.pubkey}/record")
        )
        bts = get(c, "/api/backtests")
        live["GET /api/backtests"].append(bts)
        live["GET /api/backtests/{id}"].append(get(c, f"/api/backtests/{bts[0]['id']}"))
        live["GET /api/market/symbols"].append(get(c, "/api/market/symbols?q=eth"))
        live["GET /api/market/candles"].append(
            get(c, f"/api/market/candles?instrument=BTCUSDT&tf=1h&end_ms={DAY_END - 1}&limit=24")
        )
        live["GET /api/market/indicators"].append(
            get(
                c,
                f"/api/market/indicators?instrument=BTCUSDT&tf=1h&end_ms={DAY_END - 1}&limit=24"
                "&spec=ema(5),sma(5),rsi(5),bbands(5,2),atr(5),donchian(5)",
            )
        )
        for fmt in ("bundle", "csv"):
            res = c.get(f"/api/providers/{w.pub.pubkey}/export?format={fmt}")
            assert res.status_code == 200, res.text
            live[f"GET /api/providers/{{pubkey}}/export?format={fmt}"].append(res.text)
        live["GET /api/me"].append(get(c, "/api/me"))
        live["GET /api/me/signals"].append(get(c, "/api/me/signals"))
        # the sign-in routes, with a real signature by the (throwaway-style) SA key of the `mid` identity
        key = w.mid.sa
        nonce = c.post("/api/auth/nonce", json={"address": key.address})
        assert nonce.status_code == 200, nonce.text
        live["POST /api/auth/nonce"].append(nonce.json())
        verified = c.post(
            "/api/auth/verify",
            json={
                "message": nonce.json()["message"],
                "signature": key.sign_message(nonce.json()["message"]),
            },
        )
        assert verified.status_code == 200, verified.text
        live["POST /api/auth/verify"].append(verified.json())
        live["GET /api/me (signed in)"].append(get(c, "/api/me"))
        out = c.post("/api/auth/logout")
        assert out.status_code == 200, out.text
        live["POST /api/auth/logout"].append(out.json())
        live["GET /api/governance/proposals"].append(get(c, "/api/governance/proposals"))
        live["GET /api/network"].append(get(c, "/api/network"))

    assert set(live) == set(reference), "an endpoint of the contract is not covered by this test"
    for endpoint, responses in live.items():
        for resp in responses:
            check_units(endpoint, resp)
        if endpoint in SPECIAL:
            continue  # compared by their own rules below
        if endpoint in ("GET /api/backtests", "GET /api/backtests/{id}"):
            # `kind` ("entry" | "exit") is the API's optional addition to a fill (web/API-CHANGES.md item 15)
            kinds = {
                f.pop("kind") for r in responses for b in items_of(endpoint, r) for f in b["fills"]
            }
            assert kinds == {"entry", "exit"}, f"fills should carry both kinds, got {kinds}"
        ref_shape = merge_all([shape(x) for x in items_of(endpoint, reference[endpoint])])
        live_shape = merge_all([shape(x) for r in responses for x in items_of(endpoint, r)])
        conforms(live_shape, ref_shape, endpoint)
    tier_indexes_are_valid(live["GET /api/providers"][0], dev_signals)

    # indicators: one point shape per kind of series, as in the fixture (the keys of the object are the specs asked for)
    def point_keys(r: dict[str, list[dict[str, Any]]]) -> set[frozenset[str]]:
        return {frozenset(p) for series in r.values() for p in series}

    assert (
        point_keys(live["GET /api/market/indicators"][0])
        == point_keys(reference["GET /api/market/indicators"])
        == {frozenset({"t", "value"}), frozenset({"t", "upper", "mid", "lower"})}
    )
    # the downloads: the bundle has the fixture's top-level keys and parses as a Bundle; the CSV starts with the
    # fixture's header (the API appends columns after it) and its rows have as many cells as the header
    bundle_text = live["GET /api/providers/{pubkey}/export?format=bundle"][0]
    assert (
        json.loads(bundle_text).keys()
        == json.loads(reference["GET /api/providers/{pubkey}/export?format=bundle"]).keys()
    )
    assert Bundle.model_validate_json(bundle_text).provider == w.pub.pubkey
    csv_ref = reference["GET /api/providers/{pubkey}/export?format=csv"].splitlines()[0].split(",")
    csv_live = live["GET /api/providers/{pubkey}/export?format=csv"][0].splitlines()
    assert csv_live[0].split(",")[: len(csv_ref)] == csv_ref and len(csv_live) == 5
    assert all(len(r.split(",")) == len(csv_live[0].split(",")) for r in csv_live)
    # something real was compared, not just empty lists
    assert signals[w.pub.pubkey], "the public provider should have signals"
    assert any(r["gaps"] for r in live["GET /api/providers/{pubkey}/record"])
    assert any(r["unrevealed_commitments"] for r in live["GET /api/providers/{pubkey}/record"])


# endpoints that answer with a list of items; the reference for record/signals is a list of such answers
_LISTS = {
    "GET /api/market/symbols",
    "GET /api/market/candles",
    "GET /api/providers",
    "GET /api/backtests",
    "GET /api/governance/proposals",
    "GET /api/providers/{pubkey}/signals",
    "GET /api/dev/providers/{pubkey}/releases",
    "GET /api/me/signals",
}


SPECIAL = {
    "GET /api/market/indicators",
    "GET /api/providers/{pubkey}/export?format=bundle",
    "GET /api/providers/{pubkey}/export?format=csv",
}


def items_of(endpoint: str, response: Any) -> list[Any]:
    """The objects whose shapes are compared: the list items, or the single object."""
    if endpoint in (
        "GET /api/providers/{pubkey}/signals",
        "GET /api/dev/providers/{pubkey}/releases",
    ):
        flat: list[Any] = []
        for part in response:
            flat.extend(part if isinstance(part, list) else [part])
        return flat
    if endpoint in _LISTS or isinstance(response, list):
        return list(response)
    return [response]
