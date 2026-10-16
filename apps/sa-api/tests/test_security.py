"""Security review findings in the API: H1 (9332 needs a delegation), H3 (late and missing anchors are shown and
scored), M2 (proposals need a powerful, delegated proposer), M3 (revoked device keys), L1 (nonce rate limit), L2 (proxy
headers and the Secure cookie), L4 (rule sets judged now)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api_support import ThreadedRelay
from sa_api.app import create_app
from sa_api.config import ApiConfig
from sa_api.main import build_parser, config_from, main
from sa_api.service import Service
from sa_api.siwe import SiweAuth, SiweError
from sa_core import governance
from sa_core.delegation import delegation_tag, revocation_event, sign_delegation, sign_revocation
from sa_core.governance import build_proposal_event
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import ALLOCATION_KIND, Proposal, Tier
from sa_core.nostr import build_event
from sa_core.power import allocation_event, ruleset_event, sign_allocation, sign_ruleset
from world import MINUTE, Actor, StubCandles, World, candles_around, farm_stamper


def get(client: TestClient, path: str) -> Any:
    res = client.get(path)
    assert res.status_code == 200, res.text
    return res.json()


def pub_of(client: TestClient) -> dict[str, Any]:
    found = [p for p in get(client, "/api/providers") if p["mode"] == "public"]
    assert len(found) == 1
    out: dict[str, Any] = found[0]
    return out


def build_world(
    tmp_path: Path, farm_config: Path, relay: ThreadedRelay, *, withhold_last: int = 0
) -> World:
    """The standard world; the TSA attestations of the last ``withhold_last`` public signals never reach the relay."""
    w = World(tmp_path, now_ms=int(time.time() * 1000) // MINUTE * MINUTE)
    w.build(farm_stamper(farm_config))
    hidden = set(w.public_ids[len(w.public_ids) - withhold_last :]) if withhold_last else set()
    for ev in w.events:
        if ev.kind == 1040 and any(t[1] in hidden for t in ev.tags if t[0] == "e"):
            continue
        relay.inject(ev)
    return w


def service_for(
    w: World, relay: ThreadedRelay, farm_config: Path, **kw: Any
) -> tuple[ApiConfig, Service]:
    cfg = ApiConfig(
        relays=(relay.url,), balances=w.balances, tsa_config=farm_config, governance_anchor="created-at", delay_d_s=0,
        tolerance_s=kw.pop("tolerance_s", 3600), cache_ttl_s=0.0, **kw,
    )  # fmt: skip
    return cfg, Service(cfg, candles=StubCandles(candles_around(w.now_ms - 20 * MINUTE, 120)))


# ---------------------------------------------------------------- H3


def test_h3_unanchored_signals_are_scored_as_misses_and_counted_in_the_summary(
    tmp_path: Path, farm_config: Path
) -> None:
    summaries = []
    for name, withheld in (
        ("honest", 0),
        ("withheld", 2),
    ):  # the second round trip loses its anchors
        with ThreadedRelay() as relay:
            w = build_world(tmp_path / name, farm_config, relay, withhold_last=withheld)
            cfg, svc = service_for(w, relay, farm_config)
            with TestClient(create_app(cfg, svc)) as c:
                summaries.append(pub_of(c)["record_summary"])
    honest, s = summaries
    assert (honest["unanchored"], honest["late_anchor"], honest["excluded"]) == (0, 0, 0)
    assert honest["win_rate"] == 1.0
    assert s["unanchored"] == 2 and s["late_anchor"] == 0 and s["excluded"] == 0
    assert s["anchor_coverage"] == pytest.approx(0.5)  # only on-time anchors count
    assert s["trades"] == honest["trades"]  # the round trip is still there: nothing is hidden
    assert s["win_rate"] == pytest.approx(0.5) and s["win_rate"] < honest["win_rate"]
    assert s["pnl_pct"] < honest["pnl_pct"]  # its gain is forfeited


def test_h3_late_anchors_are_counted_and_do_not_count_as_coverage(
    tmp_path: Path, farm_config: Path, relay: ThreadedRelay
) -> None:
    w = build_world(tmp_path / "w", farm_config, relay)
    cfg, svc = service_for(
        w, relay, farm_config, tolerance_s=1
    )  # signals are minutes old when the TSAs stamp them
    with TestClient(create_app(cfg, svc)) as c:
        pub = pub_of(c)
        s = pub["record_summary"]
        rec = get(c, f"/api/providers/{w.pub.pubkey}/record")
    assert s["late_anchor"] == 4 and s["unanchored"] == 0 and s["excluded"] == 0
    assert s["anchor_coverage"] == 0.0 and s["win_rate"] == 0.0 and s["pnl_pct"] == 0.0
    assert rec["verification"]["anchored_pct"] == 0.0  # the detail page agrees with the summary
    assert len(rec["trades"]) == 2  # the trades still show in the list


# ---------------------------------------------------------------- H1 / M3


def test_h1_an_allocation_published_by_an_undelegated_device_does_not_count(
    client: TestClient, relay: ThreadedRelay, world: World
) -> None:
    assert pub_of(client)["followers"] == 2
    eve = (
        SAKey.generate()
    )  # valid EIP-191 content, but the publishing device key is not delegated by eve
    alloc = sign_allocation(eve, 1, 0, {world.pub.pubkey: 10_000})
    relay.inject(
        build_event(
            DeviceKey.generate(),
            ALLOCATION_KIND,
            alloc.model_dump_json(),
            [("p", world.pub.pubkey)],
        )
    )
    # a re-wrap of the whale's own allocation under a stranger's key changes nothing either
    whale_alloc = sign_allocation(
        world.whale.sa, 1, 1000, {world.pub.pubkey: 6000, world.priv.pubkey: 3000}
    )
    relay.inject(
        build_event(DeviceKey.generate(), ALLOCATION_KIND, whale_alloc.model_dump_json(), [])
    )
    foreign = sign_delegation(SAKey.generate(), DeviceKey.generate().pubkey_hex, 10**10)
    relay.inject(
        build_event(
            DeviceKey.generate(),
            ALLOCATION_KIND,
            alloc.model_dump_json(),
            [delegation_tag(foreign)],
        )
    )
    assert pub_of(client)["followers"] == 2


def test_h1_the_same_allocation_with_a_proper_delegation_counts(
    client: TestClient, relay: ThreadedRelay, world: World
) -> None:
    fan = Actor()
    alloc = sign_allocation(fan.sa, 1, 0, {world.pub.pubkey: 10_000})
    relay.inject(allocation_event(fan.device, alloc, fan.delegation))
    assert (
        pub_of(client)["followers"] == 3
    )  # balances: fan holds nothing, but the allocation is counted


def test_m3_a_revoked_device_key_loses_its_allocations_and_its_rule_set(
    client: TestClient, relay: ThreadedRelay, world: World
) -> None:
    before = pub_of(client)
    assert before["followers"] == 2
    relay.inject(
        revocation_event(DeviceKey.generate(), sign_revocation(world.mid.sa, world.mid.pubkey, 0))
    )  # mid's allocation was published by its device key
    after = pub_of(client)
    assert after["followers"] == 1 and after["power_backing"] == pytest.approx(600_000)
    relay.inject(
        revocation_event(DeviceKey.generate(), sign_revocation(world.pub.sa, world.pub.pubkey, 0))
    )
    modes = {p["mode"] for p in get(client, "/api/providers")}
    assert modes == {"private"}  # a provider whose device key was revoked is no longer listed
    # a stranger's revocation of a key they do not control changes nothing
    relay.inject(
        revocation_event(
            DeviceKey.generate(), sign_revocation(SAKey.generate(), world.priv.pubkey, 0)
        )
    )
    assert {p["mode"] for p in get(client, "/api/providers")} == {"private"}


def test_l4_rule_sets_are_judged_now_not_at_publication(
    tmp_path: Path, farm_config: Path, relay: ThreadedRelay
) -> None:
    w = build_world(tmp_path / "w", farm_config, relay)
    now = int(time.time())
    old = Actor()
    short = sign_delegation(
        old.sa, old.pubkey, now - 500
    )  # was valid when published, is expired now
    rules = sign_ruleset(old.sa, old.pubkey, 3600, tiers=[Tier(min_power=0, delay_fraction=1.0)])
    relay.inject(ruleset_event(old.device, rules, short, created_at=now - 1000))
    cfg, svc = service_for(w, relay, farm_config)
    with TestClient(create_app(cfg, svc)) as c:
        keys = {p["pubkey"] for p in get(c, "/api/providers")}
    assert old.pubkey not in keys and w.pub.pubkey in keys


# ---------------------------------------------------------------- M2


def test_m2_proposals_need_a_delegated_proposer_with_enough_power(
    config: ApiConfig, relay: ThreadedRelay, world: World
) -> None:
    now = int(time.time())

    def proposal(title: str) -> Proposal:
        return Proposal(
            title=title,
            options=("Yes", "No"),
            deadline=now + 3600,
            snapshot_time=now,
            relays=("ws://x",),
        )

    nobody = DeviceKey.generate()
    relay.inject(build_event(nobody, 9330, proposal("no delegation").model_dump_json(), [], now))
    poor = Actor()
    relay.inject(build_proposal_event(poor.device, proposal("no power"), poor.delegation, now))
    svc = Service(config)
    titles = [p.title for p in asyncio.run(svc.proposals())]
    assert titles == ["Ship the demo?"]  # only the whale's proposal is tallied
    low_bar = Service(config.model_copy(update={"min_proposer_power": 0}))
    assert sorted(p.title for p in asyncio.run(low_bar.proposals())) == [
        "Ship the demo?",
        "no power",
    ]
    assert ApiConfig().min_proposer_power == 1000


def test_m2_a_proposal_is_only_shown_while_its_proposer_still_qualifies(
    config: ApiConfig, relay: ThreadedRelay, world: World
) -> None:
    """The tally cache is keyed by proposal, not by trust: a changed bar or revoked proposer takes effect on re-tally."""
    svc = Service(config)
    assert len(asyncio.run(svc.proposals())) == 1
    relay.inject(
        revocation_event(
            DeviceKey.generate(), sign_revocation(world.whale.sa, world.whale.pubkey, 0)
        )
    )
    assert asyncio.run(Service(config).proposals()) == []


# ---------------------------------------------------------------- L1


def test_l1_nonce_requests_are_rate_limited_per_client_ip() -> None:
    t = [1_800_000_000.0]
    auth = SiweAuth(nonce_rate_per_min=3, clock=lambda: t[0])
    key = SAKey.generate()
    for _ in range(3):
        auth.nonce(key.address, "h", client="1.2.3.4")
    with pytest.raises(SiweError, match="more than 3 sign-in nonces in a minute") as info:
        auth.nonce(key.address, "h", client="1.2.3.4")
    assert info.value.status == 429
    auth.nonce(key.address, "h", client="5.6.7.8")  # another client is not affected
    t[0] += 61
    auth.nonce(key.address, "h", client="1.2.3.4")  # the window slides
    with pytest.raises(ValueError, match="nonce_rate_per_min"):
        SiweAuth(nonce_rate_per_min=0)


def test_l1_one_client_cannot_exhaust_the_pending_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    from sa_api import siwe

    monkeypatch.setattr(siwe, "MAX_PENDING", 50)
    auth = SiweAuth(nonce_rate_per_min=10, clock=lambda: 1_800_000_000.0)
    key = SAKey.generate()
    for _ in range(10):
        auth.nonce(key.address, "h", client="9.9.9.9")
    for _ in range(90):
        with pytest.raises(SiweError, match="more than 10") as info:
            auth.nonce(key.address, "h", client="9.9.9.9")
        assert info.value.status == 429
    auth.nonce(key.address, "h", client="8.8.8.8")  # everybody else can still sign in


def test_l1_the_rate_limit_table_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from sa_api import siwe

    monkeypatch.setattr(siwe, "MAX_RATE_KEYS", 3)
    t = [1_800_000_000.0]
    auth = SiweAuth(clock=lambda: t[0])
    key = SAKey.generate()
    for ip in ("a", "b", "c"):
        auth.nonce(key.address, "h", client=ip)
    with pytest.raises(SiweError, match="too many clients"):
        auth.nonce(key.address, "h", client="d")
    t[0] += 61  # the old windows are empty: room again
    auth.nonce(key.address, "h", client="d")


def test_l1_over_http_the_nonce_route_answers_429_json(config: ApiConfig) -> None:
    cfg = config.model_copy(update={"nonce_rate_per_min": 2})
    key = SAKey.generate()
    with TestClient(create_app(cfg, Service(cfg))) as c:
        codes = [
            c.post("/api/auth/nonce", json={"address": key.address}).status_code for _ in range(3)
        ]
        assert codes == [200, 200, 429]
        res = c.post("/api/auth/nonce", json={"address": key.address})
        assert res.json()["error"].startswith("testclient asked for more than 2 sign-in nonces")


# ---------------------------------------------------------------- L2


def test_l2_the_secure_cookie_follows_the_scheme_or_a_configured_https_uri(
    config: ApiConfig,
) -> None:
    key = SAKey.generate()

    def cookie_flags(cfg: ApiConfig, base: str) -> set[str]:
        with TestClient(create_app(cfg, Service(cfg)), base_url=base) as c:
            n = c.post("/api/auth/nonce", json={"address": key.address}).json()
            res = c.post(
                "/api/auth/verify",
                json={"message": n["message"], "signature": key.sign_message(n["message"])},
            )
            assert res.status_code == 200, res.text
            return {p.strip().lower() for p in res.headers["set-cookie"].split(";")}

    assert "secure" not in cookie_flags(config, "http://dash.example")
    behind_proxy = config.model_copy(
        update={"siwe_uri": "https://dash.example", "siwe_domain": "dash.example"}
    )
    assert "secure" in cookie_flags(
        behind_proxy, "http://dash.example"
    )  # proxy forwards plain http; the URI says https


def test_l2_proxy_headers_are_off_by_default_and_passed_to_uvicorn_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("sa_api.main.uvicorn.run", lambda app, **kw: seen.update(kw))
    assert main([]) == 0
    default = dict(seen)
    assert main(["--proxy-headers", "--forwarded-allow-ips", "10.0.0.5,10.0.0.6"]) == 0
    assert (default["proxy_headers"], default["forwarded_allow_ips"]) == (False, None)
    assert (seen["proxy_headers"], seen["forwarded_allow_ips"]) == (True, "10.0.0.5,10.0.0.6")
    cfg = config_from(build_parser().parse_args(["--proxy-headers"]))
    assert cfg.proxy_headers is True and cfg.forwarded_allow_ips == "127.0.0.1"
    assert "Secure" in build_parser().format_help()  # the help says the cookie flag depends on it


def test_l2_a_forwarded_allow_list_without_proxy_headers_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sa_api.main.uvicorn.run", lambda app, **kw: None)
    assert main(["--forwarded-allow-ips", "10.0.0.5"]) == 0
    assert "ignored" in capsys.readouterr().err


def _count_collects(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    from sa_api import service as service_mod

    calls: list[list[int]] = []
    real = governance.collect

    async def counting(relays: Any, filters: Any, **kw: Any) -> Any:
        calls.append([k for f in filters for k in f["kinds"]])
        return await real(relays, filters, **kw)

    monkeypatch.setattr(service_mod, "collect", counting)
    return calls


def test_m2_spam_proposals_cost_no_vote_queries_and_tallies_are_cached_per_proposal(
    config: ApiConfig, relay: ThreadedRelay, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    for i in range(30):  # a flood of "official" proposals from throwaway keys
        spam = Proposal(
            title=f"spam {i}",
            options=("a", "b"),
            deadline=now + 3600,
            snapshot_time=now,
            relays=("ws://x",),
        )
        relay.inject(build_event(DeviceKey.generate(), 9330, spam.model_dump_json(), [], now + i))
    calls = _count_collects(monkeypatch)
    svc = Service(config.model_copy(update={"cache_ttl_s": 10.0}))
    first = asyncio.run(svc._tally_proposals())
    assert [p.title for p in first] == ["Ship the demo?"]
    # one listing, the four shared kinds once, one vote query: independent of how many proposals were spammed
    assert calls == [[9330], [9332, 9333, 9334, 1040], [9331]]
    again = asyncio.run(svc._tally_proposals())
    assert again == first and calls[3:] == [
        [9330]
    ]  # only the listing: rejections and the tally are cached per id


def test_m2_at_most_max_proposals_tallied_are_tallied(
    config: ApiConfig, relay: ThreadedRelay, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sa_api import service as service_mod

    now = int(time.time())
    for i in range(5):
        p = Proposal(
            title=f"p{i}",
            options=("a", "b"),
            deadline=now + 3600,
            snapshot_time=now,
            relays=("ws://x",),
        )
        relay.inject(
            build_proposal_event(world.whale.device, p, world.whale.delegation, now + 10 + i)
        )
    monkeypatch.setattr(service_mod, "MAX_PROPOSALS_TALLIED", 3)
    titles = [p.title for p in asyncio.run(Service(config)._tally_proposals())]
    assert titles == ["p4", "p3", "p2"]  # the newest three
