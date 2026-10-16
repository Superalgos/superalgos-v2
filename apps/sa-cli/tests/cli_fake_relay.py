"""In-process Nostr relay for bot tests: stores events, answers REQ, pushes new events to live subscriptions.

No tiers and no delays (the Go relay is exercised by tests/e2e). `reject` lets a test refuse events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from sa_core.models import NostrEvent
from sa_core.nostr import event_from_json


def matches(ev: NostrEvent, flt: dict[str, Any]) -> bool:
    if "ids" in flt and ev.id not in flt["ids"]:
        return False
    if "kinds" in flt and ev.kind not in flt["kinds"]:
        return False
    if "authors" in flt and ev.pubkey not in flt["authors"]:
        return False
    if "since" in flt and ev.created_at < flt["since"]:
        return False
    return all(
        not key.startswith("#")
        or any(len(t) > 1 and t[0] == key[1:] and t[1] in wanted for t in ev.tags)
        for key, wanted in flt.items()
    )


class FakeRelay:
    def __init__(
        self,
        *,
        reject: Callable[[NostrEvent], str | None] | None = None,
        challenge: str | None = "c1",
    ) -> None:
        self.events: dict[str, NostrEvent] = {}
        self.auth_events: list[NostrEvent] = []
        self.reject = reject
        self.challenge = challenge
        self.url = ""
        self._server: Server | None = None
        self._subs: list[tuple[ServerConnection, str, list[dict[str, Any]]]] = []
        self.conns: set[ServerConnection] = set()

    async def start(self) -> str:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def drop_connections(self) -> None:
        for ws in list(self.conns):
            await ws.close()

    async def inject(self, ev: NostrEvent) -> None:
        """Store an event and push it to matching live subscriptions (as if another client published it)."""
        self.events[ev.id] = ev
        for ws, sid, filters in list(self._subs):
            if any(matches(ev, f) for f in filters):
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps(["EVENT", sid, ev.model_dump(mode="json")]))

    async def _handle(self, ws: ServerConnection) -> None:
        self.conns.add(ws)
        if self.challenge is not None:
            await ws.send(json.dumps(["AUTH", self.challenge]))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == "AUTH":
                    ev = event_from_json(msg[1])
                    self.auth_events.append(ev)
                    await ws.send(json.dumps(["OK", ev.id, True, ""]))
                elif msg[0] == "EVENT":
                    ev = event_from_json(msg[1])
                    why = self.reject(ev) if self.reject else None
                    if why is not None:
                        await ws.send(json.dumps(["OK", ev.id, False, why]))
                        continue
                    new = ev.id not in self.events
                    await ws.send(
                        json.dumps(["OK", ev.id, True, "" if new else "duplicate: already have it"])
                    )
                    if new:
                        await self.inject(ev)
                elif msg[0] == "REQ":
                    sid, filters = msg[1], msg[2:]
                    self._subs.append((ws, sid, filters))
                    for ev in list(self.events.values()):
                        if any(matches(ev, f) for f in filters):
                            await ws.send(json.dumps(["EVENT", sid, ev.model_dump(mode="json")]))
                    await ws.send(json.dumps(["EOSE", sid]))
                elif msg[0] == "CLOSE":
                    self._subs = [s for s in self._subs if not (s[0] is ws and s[1] == msg[1])]
        except Exception:  # connection dropped by the test
            await asyncio.sleep(0)
        finally:
            self.conns.discard(ws)
            self._subs = [s for s in self._subs if s[0] is not ws]
