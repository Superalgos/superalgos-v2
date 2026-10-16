"""SEP-2.1 and SEP-2.2: Sign-In with Ethereum (EIP-4361). A wallet signs a server-built message; the session is the
recovered address; an expired, replayed, foreign-domain or tampered attempt is rejected."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sa_api import siwe
from sa_api.app import create_app
from sa_api.config import ApiConfig
from sa_api.service import Service
from sa_api.siwe import SESSION_COOKIE, SiweAuth, SiweError
from sa_core.keys import SAKey
from world import World

HOST = "dash.example:8787"
NO_FIXTURES = settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def sign_in(auth: SiweAuth, key: SAKey, host: str = HOST) -> tuple[str, str, str]:
    """(nonce, message, signature) as a wallet would produce them."""
    nonce, message = auth.nonce(key.address, host)
    return nonce, message, key.sign_message(message)


def rejects(
    auth: SiweAuth, message: str, signature: str, status: int, why: str, host: str = HOST
) -> None:
    with pytest.raises(SiweError, match=why) as info:
        auth.verify(message, signature, host)
    assert info.value.status == status


# ---------------------------------------------------------------- the protocol


def test_round_trip_with_a_throwaway_key_recovers_the_address_and_opens_a_session() -> None:
    key, clock = SAKey.generate(), Clock()
    auth = SiweAuth(clock=clock)
    nonce, message, sig = sign_in(auth, key)
    assert message.startswith(f"{HOST} wants you to sign in with your Ethereum account:\n0x")
    for needle in (
        "\nURI: http://" + HOST,
        "\nVersion: 1\n",
        "\nChain ID: 1\n",
        f"\nNonce: {nonce}\n",
        "\nIssued At: ",
        "\nExpiration Time: ",
    ):
        assert needle in message
    parsed = siwe.parse_message(message)
    assert parsed.address.lower() == key.address and parsed.address == siwe.checksum(
        key.address
    )  # EIP-55
    assert 0 < siwe.parse_time(parsed.expiration) - siwe.parse_time(parsed.issued_at) <= 600
    address, token, max_age = auth.verify(message, sig, HOST)
    assert address.lower() == key.address and max_age == 86_400
    session = auth.session(token)
    assert session is not None and session.address == address
    assert auth.session("not-a-token") is None and auth.session(None) is None
    assert (
        auth.logout(token) is True and auth.session(token) is None and auth.logout(token) is False
    )


def test_a_replayed_nonce_is_rejected_even_with_a_perfect_signature() -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message, sig = sign_in(auth, key)
    auth.verify(message, sig, HOST)
    rejects(auth, message, sig, 401, "unknown or already used nonce")


def test_a_failed_attempt_burns_the_nonce() -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message, sig = sign_in(auth, key)
    bad = sig[:-4] + ("0000" if not sig.endswith("0000") else "1111")
    with pytest.raises(SiweError):
        auth.verify(message, bad, HOST)
    rejects(auth, message, sig, 401, "unknown or already used nonce")


def test_an_expired_message_is_rejected_and_the_expiry_is_at_most_ten_minutes() -> None:
    key, clock = SAKey.generate(), Clock()
    auth = SiweAuth(clock=clock)
    _, message, sig = sign_in(auth, key)
    clock.t += 601
    rejects(auth, message, sig, 401, "expired")
    # the last second still works
    _, message, sig = sign_in(auth, key)
    clock.t += 599
    assert auth.verify(message, sig, HOST)[0].lower() == key.address
    with pytest.raises(ValueError, match="nonce_ttl_s=601"):
        SiweAuth(nonce_ttl_s=601)


def test_a_message_signed_for_another_domain_is_rejected() -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message, _ = sign_in(auth, key, host="dash.example:8787")
    phishing = message.replace(
        "dash.example:8787", "evil.example", 1
    )  # same nonce, the owner signs a lookalike
    rejects(
        auth,
        phishing,
        key.sign_message(phishing),
        401,
        "domain 'evil.example' is not this server's",
    )
    # the honest message presented to a different server (other Host) fails the same way
    auth2 = SiweAuth(clock=Clock())
    _, message2, sig2 = sign_in(auth2, key, host="dash.example:8787")
    rejects(
        auth2,
        message2,
        sig2,
        401,
        "domain 'dash.example:8787' is not this server's 'other.example'",
        host="other.example",
    )


def test_a_configured_domain_overrides_the_host_header() -> None:
    key = SAKey.generate()
    auth = SiweAuth(
        domain="app.superalgos.example", uri="https://app.superalgos.example", clock=Clock()
    )
    _, message, sig = sign_in(auth, key, host="127.0.0.1:8787")  # the proxy's view of the Host
    assert (
        message.startswith("app.superalgos.example wants you")
        and "URI: https://app.superalgos.example\n" in message
    )
    assert auth.verify(message, sig, "127.0.0.1:8787")[0].lower() == key.address


def test_a_signature_by_another_key_is_rejected() -> None:
    victim, attacker = SAKey.generate(), SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message = auth.nonce(victim.address, HOST)
    rejects(
        auth,
        message,
        attacker.sign_message(message),
        401,
        "signature recovers 0x.*but the message is for",
    )


# r and s: the v byte is excluded because 0x1b and 0x00 are two spellings of the same recovery id (both are accepted)
@given(index=st.integers(0, 127), digit=st.sampled_from("0123456789abcdef"))
@NO_FIXTURES
def test_any_tampered_signature_is_rejected(index: int, digit: str) -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message, sig = sign_in(auth, key)
    body = sig[2:]
    if body[index] == digit:
        digit = "0" if digit != "0" else "1"
    tampered = "0x" + body[:index] + digit + body[index + 1 :]
    with pytest.raises(SiweError):
        auth.verify(message, tampered, HOST)


@given(index=st.integers(0, 10_000), char=st.characters(min_codepoint=33, max_codepoint=126))
@NO_FIXTURES
def test_any_tampered_message_is_rejected(index: int, char: str) -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message, sig = sign_in(auth, key)
    i = index % len(message)
    if message[i] == char:
        char = "~" if char != "~" else "}"
    tampered = message[:i] + char + message[i + 1 :]
    with pytest.raises(SiweError):
        auth.verify(tampered, sig, HOST)


def test_the_other_recovery_id_recovers_a_different_address_and_is_rejected() -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message, sig = sign_in(auth, key)
    flipped = sig[:-2] + ("1c" if sig.endswith("1b") else "1b")
    rejects(auth, message, flipped, 401, "signature recovers")
    _, message, sig = sign_in(auth, key)  # both spellings of the right id are the same signature
    legacy = sig[:-2] + ("00" if sig.endswith("1b") else "01")
    assert auth.verify(message, legacy, HOST)[0].lower() == key.address


@given(message=st.text(max_size=300), signature=st.text(max_size=200))
@NO_FIXTURES
def test_arbitrary_input_never_opens_a_session(message: str, signature: str) -> None:
    auth = SiweAuth(clock=Clock())
    with pytest.raises(SiweError):
        auth.verify(message, signature, HOST)


def test_an_unrecoverable_signature_is_a_clear_401() -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    _, message = auth.nonce(key.address, HOST)
    rejects(auth, message, "0x" + "00" * 65, 401, "does not recover an address")
    _, message = auth.nonce(key.address, HOST)
    rejects(auth, message, "0x1234", 400, "130 hex digits")


def test_malformed_addresses_and_messages_are_400() -> None:
    auth = SiweAuth(clock=Clock())
    for bad in ("", "0x12", "0x" + "zz" * 20, "1" * 42):
        with pytest.raises(SiweError, match="40 hex digits") as info:
            auth.nonce(bad, HOST)
        assert info.value.status == 400
    rejects(auth, "hello", "0x" + "00" * 65, 400, "not an EIP-4361 sign-in message")


def test_sessions_expire() -> None:
    key, clock = SAKey.generate(), Clock()
    auth = SiweAuth(session_ttl_s=3600, clock=clock)
    _, message, sig = sign_in(auth, key)
    _, token, _ = auth.verify(message, sig, HOST)
    clock.t += 3599
    assert auth.session(token) is not None
    clock.t += 2
    assert auth.session(token) is None


def test_pending_nonces_are_bounded_and_expired_ones_are_purged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(siwe, "MAX_PENDING", 3)
    key, clock = SAKey.generate(), Clock()
    auth = SiweAuth(clock=clock)
    for _ in range(3):
        auth.nonce(key.address, HOST)
    with pytest.raises(SiweError, match="3 sign-ins are pending") as info:
        auth.nonce(key.address, HOST)
    assert info.value.status == 429
    clock.t += 601
    auth.nonce(key.address, HOST)  # the old ones expired and made room


def test_a_second_sign_in_gets_a_different_nonce_and_session() -> None:
    key = SAKey.generate()
    auth = SiweAuth(clock=Clock())
    runs = [sign_in(auth, key) for _ in range(5)]
    assert len({r[0] for r in runs}) == 5 and all(re.fullmatch(r"[0-9a-f]{32}", r[0]) for r in runs)
    tokens = {auth.verify(m, s, HOST)[1] for _, m, s in runs}
    assert len(tokens) == 5


# ---------------------------------------------------------------- over HTTP


@pytest.fixture
def http(config: ApiConfig, world: World, tmp_path: Path) -> TestClient:
    """The API with a demo identity (mid) configured, so signed-out /api/me has something to show."""
    from sa_core.power import sign_allocation

    alloc = sign_allocation(world.mid.sa, 1, 3000, {world.pub.pubkey: 7000})
    home = world.write_follower_home("mid", world.mid, alloc, {})
    cfg = config.model_copy(update={"me_home": home})
    return TestClient(create_app(cfg, Service(cfg)), base_url=f"http://{HOST}")


def login(c: TestClient, key: SAKey) -> dict[str, str]:
    res = c.post("/api/auth/nonce", json={"address": key.address})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"nonce", "message"} and f"Nonce: {body['nonce']}" in body["message"]
    ok = c.post(
        "/api/auth/verify",
        json={"message": body["message"], "signature": key.sign_message(body["message"])},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"address": siwe.checksum(key.address)}
    return {"cookie": ok.headers["set-cookie"]}


def test_http_round_trip_sets_a_strict_httponly_cookie_and_me_follows_the_session(
    http: TestClient, world: World
) -> None:
    demo = http.get("/api/me").json()
    assert demo["signed_in"] is False and demo["sa_address"] == world.mid.address
    mid_key = world.mid.sa
    cookie = login(http, mid_key)["cookie"]
    flags = {part.strip().lower() for part in cookie.split(";")}
    assert {
        "httponly",
        "samesite=strict",
        "path=/",
    } <= flags and "secure" not in flags  # plain http on loopback
    assert any(f.startswith("max-age=") for f in flags) and cookie.startswith(f"{SESSION_COOKIE}=")
    me = http.get("/api/me").json()
    assert me["signed_in"] is True and me["sa_address"] == siwe.checksum(mid_key.address)
    assert me["token_power"] == pytest.approx(
        20_000
    )  # the real address's power via the balance oracle
    assert me["allocations"]["providers"] == {
        world.pub.pubkey: 7000
    }  # its newest valid allocation on the relay
    assert me["allocations"]["governance_bps"] == 3000
    out = http.post("/api/auth/logout")
    assert out.status_code == 200 and out.json() == {}
    assert "max-age=0" in out.headers["set-cookie"].lower()
    assert http.get("/api/me").json()["signed_in"] is False  # back to the demo identity
    assert http.post("/api/auth/logout").status_code == 401  # nothing to end any more


def test_a_session_for_an_address_without_power_shows_zero_not_the_demo_identity(
    http: TestClient,
) -> None:
    key = SAKey.generate()
    login(http, key)
    me = http.get("/api/me").json()
    assert me["sa_address"] == siwe.checksum(key.address) and me["signed_in"] is True
    assert me["token_power"] == 0 and me["allocations"] == {"governance_bps": 0, "providers": {}}


def test_a_forged_or_stale_cookie_is_the_demo_identity(http: TestClient, world: World) -> None:
    http.cookies.set(SESSION_COOKIE, "forged", domain=HOST.split(":", maxsplit=1)[0])
    me = http.get("/api/me").json()
    assert me["signed_in"] is False and me["sa_address"] == world.mid.address


def test_http_errors_are_json_with_the_documented_statuses(http: TestClient) -> None:
    key = SAKey.generate()
    assert http.post("/api/auth/nonce", json={"address": "0x12"}).status_code == 400
    assert http.post("/api/auth/nonce", json={}).status_code == 400
    assert (
        http.post(
            "/api/auth/nonce", content=b"{", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        http.post("/api/auth/nonce", json={"address": key.address, "extra": 1}).status_code == 400
    )
    res = http.post(
        "/api/auth/nonce",
        content=b"address=1",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 415 and "application/json" in res.json()["error"]
    nonce = http.post("/api/auth/nonce", json={"address": key.address}).json()
    sig = key.sign_message(nonce["message"])
    assert http.post("/api/auth/verify", json={"message": nonce["message"]}).status_code == 400
    assert http.post("/api/auth/verify", json={"message": "x", "signature": sig}).status_code == 400
    bad = http.post(
        "/api/auth/verify",
        json={
            "message": nonce["message"],
            "signature": "0x" + ("0" if sig[2] != "0" else "1") + sig[3:],
        },
    )
    assert bad.status_code == 401 and "error" in bad.json()
    replay = http.post("/api/auth/verify", json={"message": nonce["message"], "signature": sig})
    assert replay.status_code == 401 and "already used" in replay.json()["error"]
    assert SESSION_COOKIE not in http.cookies


def test_the_cookie_is_secure_over_https(config: ApiConfig, world: World) -> None:
    cfg = config
    key = SAKey.generate()
    with TestClient(create_app(cfg, Service(cfg)), base_url="https://dash.example") as c:
        nonce = c.post("/api/auth/nonce", json={"address": key.address}).json()
        assert "URI: https://dash.example\n" in nonce["message"]
        res = c.post(
            "/api/auth/verify",
            json={"message": nonce["message"], "signature": key.sign_message(nonce["message"])},
        )
        assert "secure" in {p.strip().lower() for p in res.headers["set-cookie"].split(";")}


def test_config_rejects_a_nonce_lifetime_over_ten_minutes() -> None:
    with pytest.raises(ValueError, match="siwe_nonce_ttl_s"):
        ApiConfig(siwe_nonce_ttl_s=601)
    assert json.loads(ApiConfig().model_dump_json())["siwe_nonce_ttl_s"] == 600
