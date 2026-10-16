"""TsaAnchor: offline verifier, k-of-n by organisation, stamper rate limit, real saved tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from core_tsa_lab import LocalCa, LocalTsa, refusal, respond, store_of
from sa_core.anchors import AnchorError, anchored_time, verifier_map
from sa_core.models import AnchorProof
from sa_core.tsa import (
    DEFAULT_MIN_GAP_S,
    DEFAULT_TRUST_DIR,
    ENDPOINTS,
    Endpoint,
    TrustStore,
    TsaAnchor,
    TsaError,
    TsaStamper,
    k_of_n,
    verify_token,
)

NOW = 1_800_000_000.0
POLICY = "1.2.3.4.5"
D = hashlib.sha256(b"signal event id").digest()
DATA = Path(__file__).parent / "data" / "tsa"
DAY = 86_400


def clock() -> float:
    return NOW


class Fleet:
    """Three organisations, each with its own CA and TSA."""

    def __init__(self, skews: tuple[float, float, float] = (0, 0, 0)) -> None:
        self.orgs = ("alpha", "bravo", "charlie")
        self.cas = [LocalCa(o) for o in self.orgs]
        self.tsas = [
            LocalTsa(ca, clock, POLICY, skew_s=s) for ca, s in zip(self.cas, skews, strict=True)
        ]
        self.trust: TrustStore = store_of(
            *(ca.pin(o, POLICY) for ca, o in zip(self.cas, self.orgs, strict=True))
        )

    def tokens(self, digest: bytes = D) -> list[bytes]:
        return [t.issue(digest) for t in self.tsas]


# ---------------------------------------------------------------- verifier


def test_valid_two_of_three_is_accepted_and_the_kth_earliest_bound_is_the_time() -> None:
    f = Fleet(skews=(0, 3, 7))
    r = k_of_n(f.tokens(), D, f.trust, k=2)
    assert r.accepted
    assert r.operators == ("alpha", "bravo", "charlie")
    assert r.existed_before == int(NOW) + 3 + 1  # bravo: genTime + 3 s + 1 s accuracy
    assert all(x.accuracy_s == 1.0 for x in r.results)


def test_one_valid_token_is_rejected_by_two_of_three() -> None:
    f = Fleet()
    r = k_of_n(f.tokens()[:1], D, f.trust, k=2)
    assert not r.accepted
    assert r.existed_before is None


def test_k_below_one_is_refused() -> None:
    with pytest.raises(TsaError, match="k must be at least 1"):
        k_of_n([], D, Fleet().trust, k=0)


def test_token_for_a_different_hash_is_rejected() -> None:
    f = Fleet()
    other = hashlib.sha256(b"another event").digest()
    with pytest.raises(TsaError, match="different data"):
        verify_token(f.tsas[0].issue(other), D, f.trust)
    r = k_of_n(
        [f.tsas[0].issue(other), f.tsas[1].issue(other), f.tsas[2].issue(D)], D, f.trust, k=2
    )
    assert not r.accepted
    assert len(r.rejected) == 2


def test_token_from_an_unpinned_ca_is_rejected() -> None:
    f = Fleet()
    rogue = LocalTsa(LocalCa("mallory"), clock, POLICY)
    with pytest.raises(TsaError, match="no path"):
        verify_token(rogue.issue(D), D, f.trust)
    r = k_of_n([rogue.issue(D), f.tsas[0].issue(D)], D, f.trust, k=2)
    assert not r.accepted
    assert r.operators == ("alpha",)


def test_impostor_with_a_pinned_name_but_its_own_key_is_rejected() -> None:
    f = Fleet()
    tok = LocalTsa(LocalCa("alpha"), clock, POLICY).issue(D)
    with pytest.raises(TsaError, match="no path"):
        verify_token(tok, D, f.trust)


def test_two_tokens_from_one_organisation_count_once() -> None:
    f = Fleet()
    second_alpha = LocalTsa(f.cas[0], clock, POLICY)
    toks = [f.tsas[0].issue(D), second_alpha.issue(D)]
    r = k_of_n(toks, D, f.trust, k=2)
    assert not r.accepted
    assert r.operators == ("alpha",)
    assert any("counted once" in x for x in r.rejected)
    assert k_of_n([*toks, f.tsas[1].issue(D)], D, f.trust, k=2).accepted


def test_digicert_and_quovadis_are_one_operator_as_are_sectigo_and_entrust() -> None:
    """Two URLs, two roots, one organisation: the operator label is what k-of-n counts."""
    ca1, ca2, ca3 = LocalCa("d1"), LocalCa("d2"), LocalCa("other")
    trust = store_of(
        ca1.pin("digicert", POLICY, "digicert"),
        ca2.pin("digicert", POLICY, "digicert-quovadis"),
        ca3.pin("other", POLICY),
    )
    toks = [LocalTsa(c, clock, POLICY).issue(D) for c in (ca1, ca2)]
    assert not k_of_n(toks, D, trust, k=2).accepted
    assert k_of_n([*toks, LocalTsa(ca3, clock, POLICY).issue(D)], D, trust, k=2).accepted
    by_url = {e.name: e.operator for e in ENDPOINTS}
    assert by_url["quovadis"] == by_url["digicert"] == "digicert"
    assert by_url["entrust"] == by_url["sectigo"] == "sectigo"
    bundle = {r.entry.name: r.entry.operator for r in TrustStore.load().roots}
    assert bundle["digicert-quovadis"] == bundle["digicert"] == "digicert"
    assert bundle["sectigo-qualified"] == bundle["sectigo"] == "sectigo"


def test_kth_earliest_holds_against_k_minus_one_liars() -> None:
    one_liar = Fleet(skews=(-10 * DAY, 0, 2))  # alpha backdates ten days
    honest = int(NOW) + 1
    assert k_of_n(one_liar.tokens(), D, one_liar.trust, k=2).existed_before == honest
    fooled = k_of_n(one_liar.tokens(), D, one_liar.trust, k=1).existed_before
    assert fooled is not None
    assert fooled < honest - 9 * DAY  # k=1 is fooled
    two = Fleet(skews=(-10 * DAY, -10 * DAY, 0))
    assert k_of_n(two.tokens(), D, two.trust, k=2).existed_before == int(NOW) - 10 * DAY + 1
    assert k_of_n(two.tokens(), D, two.trust, k=3).existed_before == int(NOW) + 1


def test_certificate_expired_today_but_valid_at_gentime_still_verifies() -> None:
    ca = LocalCa(
        "old",
        not_before=datetime(2000, 1, 1, tzinfo=UTC),
        not_after=datetime(2030, 1, 1, tzinfo=UTC),
    )
    tsa = LocalTsa(
        ca,
        clock,
        POLICY,
        not_before=datetime(2024, 1, 1, tzinfo=UTC),
        not_after=datetime(2025, 12, 31, tzinfo=UTC),
    )
    res = verify_token(
        tsa.issue(D, gen_unix=datetime(2025, 6, 1, tzinfo=UTC).timestamp()),
        D,
        store_of(ca.pin("old", POLICY)),
    )
    assert res.gen_time.year == 2025


def test_certificate_not_valid_at_gentime_is_rejected() -> None:
    ca = LocalCa("old")
    tsa = LocalTsa(
        ca,
        clock,
        POLICY,
        not_before=datetime(2020, 1, 1, tzinfo=UTC),
        not_after=datetime(2021, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(TsaError, match="not valid at the token's genTime"):
        verify_token(
            tsa.issue(D, gen_unix=datetime(2026, 6, 1, tzinfo=UTC).timestamp()),
            D,
            store_of(ca.pin("old", POLICY)),
        )


def test_root_not_valid_at_gentime_is_rejected() -> None:
    ca = LocalCa(
        "short",
        not_before=datetime(2020, 1, 1, tzinfo=UTC),
        not_after=datetime(2021, 1, 1, tzinfo=UTC),
    )
    tsa = LocalTsa(ca, clock, POLICY)
    with pytest.raises(TsaError, match="pinned root 'short' was not valid"):
        verify_token(tsa.issue(D), D, store_of(ca.pin("short", POLICY)))


def test_wrong_policy_oid_is_rejected() -> None:
    ca = LocalCa("alpha")
    with pytest.raises(TsaError, match="policy OID 1.9.9 is not accepted"):
        verify_token(
            LocalTsa(ca, clock, policy="1.9.9").issue(D), D, store_of(ca.pin("alpha", POLICY))
        )


def test_signer_without_the_timestamping_eku_is_rejected() -> None:
    ca = LocalCa("alpha")
    tok = LocalTsa(ca, clock, POLICY, eku_timestamping=False).issue(D)
    with pytest.raises(TsaError, match="extended key usage"):
        verify_token(tok, D, store_of(ca.pin("alpha", POLICY)))


def test_tampered_and_forged_tokens_are_rejected() -> None:
    f = Fleet()
    tok = bytearray(f.tsas[0].issue(D))
    tok[bytes(tok).find(b"Z") - 3] ^= 0x01  # inside TSTInfo.genTime: messageDigest breaks
    with pytest.raises(TsaError):
        verify_token(bytes(tok), D, f.trust)
    forged = LocalTsa(f.cas[0], clock, POLICY)
    forged.cert = f.tsas[0].cert  # alpha's certificate, another private key
    with pytest.raises(TsaError, match="signature does not verify"):
        verify_token(forged.issue(D), D, f.trust)
    assert verify_token(f.tsas[0].issue(D), D, f.trust).operator == "alpha"


def test_garbage_is_rejected_with_a_readable_error() -> None:
    with pytest.raises(TsaError, match="does not parse"):
        verify_token(b"not a token", D, Fleet().trust)


# ---------------------------------------------------------------- trust bundle


def test_packaged_bundle_is_versioned_and_loads() -> None:
    trust = TrustStore.load()
    assert trust.version == "2026-10-12"
    assert len(trust.roots) == 12
    assert {"digicert", "sectigo", "globalsign"} <= trust.operators()


def test_bundle_with_an_altered_root_is_refused(tmp_path: Path) -> None:
    dst = tmp_path / "trust"
    shutil.copytree(DEFAULT_TRUST_DIR, dst)
    spec = json.loads((dst / "tsas.json").read_text())
    spec["entries"][0]["root_sha256"] = "00" * 32
    (dst / "tsas.json").write_text(json.dumps(spec))
    with pytest.raises(TsaError, match="was altered or replaced"):
        TrustStore.load(dst)


def test_unversioned_or_missing_bundle_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TsaError, match="not found"):
        TrustStore.load(tmp_path)
    (tmp_path / "tsas.json").write_text("[]")
    with pytest.raises(TsaError, match="not a versioned bundle"):
        TrustStore.load(tmp_path)
    (tmp_path / "tsas.json").write_text(
        json.dumps(
            {
                "bundle_version": "x",
                "entries": [
                    {
                        "name": "n",
                        "operator": "o",
                        "root_file": "missing.pem",
                        "root_sha256": "00",
                        "policies": [],
                        "root_source": "t",
                    }
                ],
            }
        )
    )
    with pytest.raises(TsaError, match="missing"):
        TrustStore.load(tmp_path)


# ---------------------------------------------------------------- real tokens, saved 2026-10-12

TRUSTED = [e.name for e in ENDPOINTS if (DATA / f"{e.name}.tst").exists()]


def _saved(name: str) -> tuple[bytes, bytes]:
    return (DATA / f"{name}.tst").read_bytes(), bytes.fromhex((DATA / f"{name}.digest").read_text())


@pytest.mark.parametrize("name", TRUSTED)
def test_saved_real_token_verifies_offline(name: str) -> None:
    tok, digest = _saved(name)
    r = verify_token(tok, digest, TrustStore.load())
    assert r.gen_time.year == 2026
    assert r.signer_eku_critical


def test_the_saved_real_tokens_cover_the_endpoint_list() -> None:
    assert len(TRUSTED) >= 12


def test_untrusted_real_tsa_is_rejected() -> None:
    """MeSince answers with a well-formed token whose root is self-issued and not pinned."""
    tok, digest = _saved("mesign")
    with pytest.raises(TsaError, match="no path"):
        verify_token(tok, digest, TrustStore.load())


def test_real_token_for_a_different_hash_or_with_a_flipped_byte_is_rejected() -> None:
    trust = TrustStore.load()
    tok, digest = _saved("digicert")
    with pytest.raises(TsaError, match="different data"):
        verify_token(tok, hashlib.sha256(b"something else").digest(), trust)
    bad = bytearray(_saved("globalsign")[0])
    bad[len(bad) // 2] ^= 0x01
    with pytest.raises(TsaError):
        verify_token(bytes(bad), _saved("globalsign")[1], trust)
    assert verify_token(tok, digest, trust).operator == "digicert"


def test_real_two_of_three_from_distinct_organisations() -> None:
    trust = TrustStore.load()
    # the saved tokens cover different digests; re-key by stamping nothing: verify each alone,
    # then check the real operators are the labels the k-of-n rule counts
    ops = {n: verify_token(*_saved(n), trust).operator for n in ("digicert", "quovadis", "entrust")}
    assert ops == {"digicert": "digicert", "quovadis": "digicert", "entrust": "sectigo"}


# ---------------------------------------------------------------- TsaAnchor


def anchor_for(f: Fleet, k: int = 2) -> TsaAnchor:
    return TsaAnchor(f.trust, k=k)


def proof_for(f: Fleet, digests: list[bytes]) -> list[AnchorProof]:
    return stamper_for(f, FakeNet(f)).stamp_batch(digests)


class FakeNet:
    """httpx transport that answers like the fleet's TSAs; records every request."""

    def __init__(self, f: Fleet, urls: tuple[str, ...] = ("a", "b", "c")) -> None:
        self.f = f
        self.hosts = {f"http://{u}.tsa.test": t for u, t in zip(urls, f.tsas, strict=True)}
        self.calls: list[str] = []
        self.override: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        self.calls.append(url)
        if url in self.override:
            return self.override[url]
        return httpx.Response(200, content=respond(self.hosts[url], request.content))


def stamper_for(f: Fleet, net: FakeNet, k: int = 2, now: list[float] | None = None) -> TsaStamper:
    eps = [
        Endpoint(name=n, url=f"http://{n}.tsa.test", operator=o)
        for n, o in zip("abc", f.orgs, strict=True)
    ]
    t = now if now is not None else [0.0]
    return TsaStamper(eps, f.trust, k=k, clock=lambda: t[0], transport=httpx.MockTransport(net))


def test_stamp_then_verify_offline_round_trip_through_a_merkle_batch() -> None:
    f = Fleet(skews=(0, 3, 7))
    ids = [hashlib.sha256(bytes([i])).digest() for i in range(5)]
    proofs = proof_for(f, ids)
    anchor = anchor_for(f)
    assert [p.digest for p in proofs] == [i.hex() for i in ids]
    for p, i in zip(proofs, ids, strict=True):
        assert p.kind == "tsa"
        bound = anchor.verify(p, i)
        assert (bound.kind, bound.not_after) == ("tsa", int(NOW) + 3 + 1)
    # the same proofs, through the generic combiner that trackrecord.compute uses
    assert anchored_time(ids[2], proofs, {"tsa": anchor}) == int(NOW) + 4


def test_a_single_digest_proof_has_an_empty_path() -> None:
    f = Fleet()
    p = proof_for(f, [D])[0]
    assert json.loads(p.data)["path"] == []
    assert anchor_for(f).verify(p, D).not_after == int(NOW) + 1


def test_proof_for_another_digest_or_kind_or_tampered_data_fails() -> None:
    f = Fleet()
    anchor = anchor_for(f)
    p = proof_for(f, [D, hashlib.sha256(b"x").digest()])[0]
    other = hashlib.sha256(b"other").digest()
    with pytest.raises(AnchorError, match="not this digest"):
        anchor.verify(p, other)
    with pytest.raises(AnchorError, match="proof kind"):
        anchor.verify(AnchorProof(kind="ots", digest=D.hex(), data=p.data), D)
    data = json.loads(p.data)
    data["path"][0]["sibling"] = "00" * 32
    with pytest.raises(AnchorError, match="Merkle path"):
        anchor.verify(AnchorProof(kind="tsa", digest=D.hex(), data=json.dumps(data)), D)
    for bad in (
        "not json",
        json.dumps({**json.loads(p.data), "v": 9}),
        json.dumps({**json.loads(p.data), "tokens": ["!!"]}),
    ):
        with pytest.raises(AnchorError, match="malformed|version"):
            anchor.verify(AnchorProof(kind="tsa", digest=D.hex(), data=bad), D)


def test_proof_with_only_one_organisation_is_not_accepted() -> None:
    f = Fleet()
    p = proof_for(f, [D])[0]
    data = json.loads(p.data)
    data["tokens"] = data["tokens"][:1]
    with pytest.raises(AnchorError, match="1 distinct operator"):
        anchor_for(f).verify(AnchorProof(kind="tsa", digest=D.hex(), data=json.dumps(data)), D)
    assert anchor_for(f, k=1).verify(
        AnchorProof(kind="tsa", digest=D.hex(), data=json.dumps(data)), D
    )


def test_verify_only_anchor_cannot_stamp() -> None:
    with pytest.raises(AnchorError, match="verify-only"):
        anchor_for(Fleet()).stamp(D)


def test_anchor_with_a_stamper_stamps_one_digest() -> None:
    f = Fleet()
    anchor = TsaAnchor(f.trust, stamper=stamper_for(f, FakeNet(f)))
    assert anchor.verify(anchor.stamp(D), D).not_after == int(NOW) + 1


@given(n=st.integers(1, 12), data=st.data())
def test_every_leaf_of_a_batch_verifies_and_no_other_digest_does(
    n: int, data: st.DataObject
) -> None:
    f = Fleet()
    ids = [hashlib.sha256(b"leaf%d" % i).digest() for i in range(n)]
    proofs = proof_for(f, ids)
    i = data.draw(st.integers(0, n - 1))
    anchor = anchor_for(f)
    assert anchor.verify(proofs[i], ids[i]).not_after == int(NOW) + 1
    outsider = hashlib.sha256(b"outsider").digest()
    with pytest.raises(AnchorError):
        anchor.verify(AnchorProof(kind="tsa", digest=outsider.hex(), data=proofs[i].data), outsider)


# ---------------------------------------------------------------- stamper


def test_stamper_needs_k_distinct_operators_and_a_polite_gap() -> None:
    f = Fleet()
    eps = [
        Endpoint(name="x", url="http://x", operator="o"),
        Endpoint(name="y", url="http://y", operator="o"),
    ]
    with pytest.raises(TsaError, match="needs at least 2 distinct operators"):
        TsaStamper(eps, f.trust, k=2)
    eps2 = [
        Endpoint(name="x", url="http://x", operator="o"),
        Endpoint(name="y", url="http://y", operator="p"),
    ]
    with pytest.raises(TsaError, match="below 15"):
        TsaStamper(eps2, f.trust, k=2, min_gap_s=5)
    assert DEFAULT_MIN_GAP_S == 15.0


def test_no_tsa_is_contacted_more_often_than_every_15_seconds() -> None:
    f = Fleet()
    net = FakeNet(f)
    now = [100.0]
    st_ = stamper_for(f, net, now=now)
    st_.add(D)
    assert st_.flush()  # first batch goes out
    assert len(net.calls) == 3
    st_.add(hashlib.sha256(b"two").digest())
    now[0] += 14.9
    assert st_.flush() == []  # too early: nothing is sent, the batch keeps growing
    assert len(net.calls) == 3
    assert st_.seconds_until_ready() == pytest.approx(0.1)
    with pytest.raises(TsaError, match="rate limit"):
        st_.stamp_batch([D])
    assert len(net.calls) == 3
    now[0] += 0.1
    assert len(st_.flush()) == 1  # ready again
    assert len(net.calls) == 6
    assert st_.pending == []


def test_flush_with_nothing_pending_sends_nothing() -> None:
    net = FakeNet(Fleet())
    assert stamper_for(Fleet(), net).flush() == []
    assert net.calls == []


def test_http_429_stops_that_tsa_for_good_until_resumed() -> None:
    f = Fleet()
    net = FakeNet(f)
    net.override["http://c.tsa.test"] = httpx.Response(429)
    now = [0.0]
    s = stamper_for(f, net, now=now)
    proofs = s.stamp_batch([D])  # a and b still give 2 of 3
    assert anchor_for(f).verify(proofs[0], D)
    assert "c" in s.stopped
    assert "HTTP 429" in s.stopped["c"]
    now[0] += 20
    s.stamp_batch([D])
    assert net.calls.count("http://c.tsa.test") == 1  # never contacted again
    with pytest.raises(TsaError, match="not stopped"):
        s.resume("a")
    del net.override["http://c.tsa.test"]
    s.resume("c")
    now[0] += 20
    s.stamp_batch([D])
    assert net.calls.count("http://c.tsa.test") == 2


def test_a_tsa_rejection_status_stops_it_and_too_few_operators_raises() -> None:
    f = Fleet()
    net = FakeNet(f)
    net.override["http://b.tsa.test"] = httpx.Response(200, content=refusal())
    net.override["http://c.tsa.test"] = httpx.Response(200, content=refusal("rejection", "no"))
    now = [0.0]
    s = stamper_for(f, net, now=now)
    s.add(D)
    with pytest.raises(TsaError, match="1 distinct operator"):
        s.flush()
    assert set(s.stopped) == {"b", "c"}
    assert s.pending == [D]  # kept for the next attempt
    now[0] += 20
    with pytest.raises(TsaError, match=r"only 1 operator\(s\) active"):
        s.flush()
    assert len(net.calls) == 3  # b and c were not asked again


def test_other_failures_do_not_stop_a_tsa_but_are_reported() -> None:
    f = Fleet()
    net = FakeNet(f)
    net.override["http://c.tsa.test"] = httpx.Response(503)
    s = stamper_for(f, net)
    s.stamp_batch([D])
    assert s.stopped == {}
    assert any("HTTP 503" in e for e in s.last_errors)


def test_a_token_that_does_not_verify_is_never_put_in_a_proof() -> None:
    f = Fleet()
    net = FakeNet(f)
    rogue = LocalTsa(LocalCa("mallory"), clock, POLICY)
    net.hosts["http://c.tsa.test"] = rogue
    s = stamper_for(f, net)
    proof = s.stamp_batch([D])[0]
    assert len(json.loads(proof.data)["tokens"]) == 2
    assert any("no path" in e for e in s.last_errors)


def test_network_error_is_reported_with_the_url() -> None:
    f = Fleet()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    s = TsaStamper(
        [
            Endpoint(name=n, url=f"http://{n}.tsa.test", operator=o)
            for n, o in zip("abc", f.orgs, strict=True)
        ],
        f.trust,
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(TsaError, match="0 distinct operator"):
        s.stamp_batch([D])
    assert "ConnectError" in s.last_errors[0]


def test_bad_digest_lengths_are_refused() -> None:
    s = stamper_for(Fleet(), FakeNet(Fleet()))
    with pytest.raises(TsaError, match="32 bytes"):
        s.add(b"short")


def test_garbled_responses_are_errors() -> None:
    from sa_core.tsa import parse_response

    with pytest.raises(TsaError, match="not a TimeStampResp"):
        parse_response(b"nonsense")
    with pytest.raises(TsaError, match="TSA refused"):
        parse_response(refusal())


# ---------------------------------------------------------------- online


@pytest.mark.online
def test_online_two_of_three_public_tsas_round_trip() -> None:
    anchor = TsaAnchor(stamper=TsaStamper())
    digest = hashlib.sha256(os.urandom(32)).digest()
    proof = anchor.stamp(digest)
    bound = anchor.verify(proof, digest)
    assert abs(bound.not_after - datetime.now(UTC).timestamp()) < 60
    assert base64.b64decode(json.loads(proof.data)["tokens"][0])


# ---------------------------------------------------------------- registered for the track record


def test_verifier_map_registers_tsa_and_rejects_duplicate_kinds() -> None:
    f = Fleet()
    a = anchor_for(f)
    assert verifier_map(a) == {"tsa": a}
    with pytest.raises(AnchorError, match="two anchors claim proof kind 'tsa'"):
        verifier_map(a, anchor_for(f))


def test_trackrecord_compute_accepts_tsa_proofs_and_scores_a_late_one_as_a_miss() -> None:
    from core_helpers import FIXTURE_CANDLES, MINUTE, T0_MS, Provider
    from sa_core.trackrecord import compute

    f = Fleet()
    stamp_time = [float(T0_MS // 1000 + 2)]
    for tsa in f.tsas:
        tsa.clock = lambda: stamp_time[0]
    p = Provider()
    e1 = p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)
    e2 = p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    s1 = stamper_for(f, FakeNet(f))
    proofs = s1.stamp_batch([bytes.fromhex(e1.id)])  # on time
    stamp_time[0] = float(T0_MS // 1000 + 2 * 60 + 600)  # e2 stamped 10 minutes after it claims
    proofs += stamper_for(f, FakeNet(f)).stamp_batch([bytes.fromhex(e2.id)])
    verifiers = verifier_map(anchor_for(f))
    r = json.loads(
        compute(
            p.bundle(tuple(proofs)), {"BINANCE:BTCUSDT": FIXTURE_CANDLES}, verifiers=verifiers
        ).to_json()
    )
    assert r["anchor_coverage"]["anchored"] == 1  # e2 carries a proof, but not an on-time one
    assert r["signals"]["counted"] == 2
    assert r["signals"]["late_anchor"] == 1 and r["signals"]["excluded"] == {}
    assert (
        json.loads(
            compute(p.bundle(tuple(proofs)), {"BINANCE:BTCUSDT": FIXTURE_CANDLES}).to_json()
        )["anchor_coverage"]["anchored"]
        == 0
    )


def test_tsa_farm_serves_three_organisations_and_stamps_with_its_own_config(tmp_path: Path) -> None:
    """The farm `sa demo` and the e2e use: its `--tsa-config` stamps and verifies with k=2, and 404s unknown paths."""
    from sa_core.tsa_lab import FARM_ORGS, tsa_farm

    with tsa_farm(tmp_path / "farm", farm_gap_s=1.0) as config_path:
        cfg = json.loads(config_path.read_text())
        assert [e["operator"] for e in cfg["endpoints"]] == list(FARM_ORGS)
        trust = TrustStore.load(Path(cfg["trust_dir"]))
        endpoints = tuple(Endpoint(**e) for e in cfg["endpoints"])
        stamper = TsaStamper(
            endpoints, trust, k=cfg["k"], min_gap_s=cfg["min_gap_s"]
        )  # loopback: below 15 s is allowed
        digest = bytes(range(32))
        proofs = stamper.stamp_batch([digest])
        assert proofs
        anchor = TsaAnchor(trust, k=cfg["k"])
        assert anchor.verify(proofs[0], digest).kind == "tsa"
        base = cfg["endpoints"][0]["url"].rsplit("/", 1)[0]
        assert httpx.post(f"{base}/nobody", content=b"x").status_code == 404


def test_a_gap_below_fifteen_seconds_is_refused_for_public_endpoints() -> None:
    from sa_core.tsa import DEFAULT_ENDPOINTS

    with pytest.raises(TsaError, match="only loopback TSAs"):
        TsaStamper(DEFAULT_ENDPOINTS, TrustStore.load(), k=2, min_gap_s=1.0)


def test_a_farm_advertised_by_a_container_name_is_a_lab_and_may_be_hit_fast(tmp_path: Path) -> None:
    """In compose the farm is `http://tsa:8080`: not loopback, so the short gap needs `"lab": true`."""
    from sa_core.tsa_lab import tsa_farm

    with tsa_farm(tmp_path / "farm", advertise="tsa", farm_gap_s=1.0) as config_path:
        cfg = json.loads(config_path.read_text())
        assert cfg["lab"] is True and cfg["endpoints"][0]["url"].startswith("http://tsa:")
        endpoints = tuple(Endpoint(**e) for e in cfg["endpoints"])
        trust = TrustStore.load(Path(cfg["trust_dir"]))
        with pytest.raises(TsaError, match="only loopback TSAs"):
            TsaStamper(endpoints, trust, k=2, min_gap_s=1.0)
        assert (
            TsaStamper(endpoints, trust, k=2, min_gap_s=1.0, allow_fast_gap=True).min_gap_s == 1.0
        )
    with tsa_farm(tmp_path / "local", farm_gap_s=1.0) as local:
        assert json.loads(local.read_text())["lab"] is False  # loopback needs no flag
