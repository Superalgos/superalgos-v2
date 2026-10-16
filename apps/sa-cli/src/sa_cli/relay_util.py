"""Small relay helpers shared by commands."""

from __future__ import annotations

import asyncio

from websockets.exceptions import WebSocketException

from sa_cli.home import CliError
from sa_core.delegation import delegation_tag
from sa_core.keys import DeviceKey
from sa_core.models import Delegation, NostrEvent
from sa_core.nostr import NostrError, RelayClient


async def publish_event(
    url: str, device: DeviceKey, delegation: Delegation, event: NostrEvent
) -> None:
    """Publish one event over an authenticated connection; a refusal becomes a ``CliError`` with the reason."""
    try:
        async with RelayClient(
            url, auth_key=device, auth_tags=[delegation_tag(delegation)]
        ) as relay:
            res = await relay.publish(event)
    except (NostrError, OSError, WebSocketException) as exc:
        raise CliError(str(exc)) from exc
    if not res.accepted:
        raise CliError(f"relay {url} refused the event (kind {event.kind}): {res.message}")


def publish_sync(url: str, device: DeviceKey, delegation: Delegation, event: NostrEvent) -> None:
    asyncio.run(publish_event(url, device, delegation, event))
