"""Async relay client against an in-process relay: publish, query, subscribe, NIP-42 AUTH."""

from __future__ import annotations

import asyncio

import pytest

from core_fake_relay import FakeRelay
from sa_core.delegation import delegation_tag, sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.nostr import NostrError, RelayClient, build_event


async def test_publish_query_round_trip(device: DeviceKey) -> None:
    relay = FakeRelay()
    url = await relay.start()
    try:
        ev = build_event(device, 9317, "x", [("e", "ab" * 32)])
        async with RelayClient(url) as client:
            res = await client.publish(ev)
            assert res.accepted
            assert await client.query([{"ids": [ev.id]}]) == [ev]
            assert await client.query([{"kinds": [1]}]) == []
            assert await client.query([{"#e": ["ab" * 32]}]) == [ev]
    finally:
        await relay.stop()


async def test_subscribe_streams_stored_and_new_events(device: DeviceKey) -> None:
    relay = FakeRelay()
    url = await relay.start()
    try:
        first = build_event(device, 9317, "one")
        relay.events[first.id] = first
        async with RelayClient(url) as client:
            stream = client.subscribe([{"kinds": [9317]}])
            got = await asyncio.wait_for(anext(stream), 3)
            assert got == first
            second = build_event(device, 9317, "two")
            relay.events[second.id] = second
            sid = next(m[1] for m in relay.requests if m[0] == "REQ")
            await relay.broadcast(sid, second)
            assert await asyncio.wait_for(anext(stream), 3) == second
            await stream.aclose()  # type: ignore[attr-defined]
    finally:
        await relay.stop()


async def test_nip42_auth_with_delegation_tag_then_publish_succeeds(device: DeviceKey) -> None:
    relay = FakeRelay(require_auth=True)
    url = await relay.start()
    try:
        d = sign_delegation(SAKey.generate(), device.pubkey_hex, 4_000_000_000)
        ev = build_event(device, 9317, "gated")
        async with RelayClient(url, auth_key=device, auth_tags=[delegation_tag(d)]) as client:
            res = await client.publish(ev)
            assert res.accepted
        assert relay.events[ev.id] == ev
        (auth,) = relay.auth_events
        assert auth.tag_values("sa-delegation") and auth.pubkey == device.pubkey_hex
    finally:
        await relay.stop()


async def test_publish_without_auth_key_is_rejected_with_the_relay_message(
    device: DeviceKey,
) -> None:
    relay = FakeRelay(require_auth=True)
    url = await relay.start()
    try:
        async with RelayClient(url) as client:
            res = await client.publish(build_event(device, 1, "x"))
            assert not res.accepted and res.message.startswith("auth-required")
            with pytest.raises(NostrError, match="no auth_key"):
                await client.authenticate(0.2)
            closed = await client.query([{"kinds": [1]}])
            assert closed == []
    finally:
        await relay.stop()


async def test_silent_relay_times_out_with_a_useful_message(device: DeviceKey) -> None:
    relay = FakeRelay()
    relay.drop_ok = True
    url = await relay.start()
    try:
        async with RelayClient(url) as client:
            with pytest.raises(NostrError, match="did not answer OK"):
                await client.publish(build_event(device, 1, "x"), timeout=0.2)
    finally:
        await relay.stop()


async def test_unreachable_relay_is_a_nostr_error() -> None:
    with pytest.raises(NostrError, match="cannot connect to relay ws://127.0.0.1:9"):
        async with RelayClient("ws://127.0.0.1:9", connect_timeout=0.5):
            pass


async def test_auth_never_completing_times_out(device: DeviceKey) -> None:
    relay = FakeRelay()  # no challenge is ever sent
    url = await relay.start()
    try:
        async with RelayClient(url, auth_key=device) as client:
            with pytest.raises(NostrError, match="did not complete AUTH"):
                await client.authenticate(0.2)
    finally:
        await relay.stop()
