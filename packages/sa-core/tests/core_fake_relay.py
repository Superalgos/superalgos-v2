"""In-process Nostr relay for tests: stores events, answers REQ, optional NIP-42 gating."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from sa_core.models import NostrEvent
from sa_core.nostr import NostrError, event_from_json, verify_event


def _matches(ev: NostrEvent, flt: dict[str, Any]) -> bool:
    if "ids" in flt and ev.id not in flt["ids"]:
        return False
    if "kinds" in flt and ev.kind not in flt["kinds"]:
        return False
    if "authors" in flt and ev.pubkey not in flt["authors"]:
        return False
    for key, wanted in flt.items():
        if key.startswith("#") and not any(
            len(t) > 1 and t[0] == key[1:] and t[1] in wanted for t in ev.tags
        ):
            return False
    return True


class FakeRelay:
    def __init__(self, *, require_auth: bool = False, challenge: str = "chal-1") -> None:
        self.require_auth = require_auth
        self.challenge = challenge
        self.events: dict[str, NostrEvent] = {}
        self.auth_events: list[NostrEvent] = []
        self.requests: list[list[Any]] = []
        self.drop_ok = False
        self._server: Server | None = None
        self._conns: set[ServerConnection] = set()
        self.url = ""

    async def start(self) -> str:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def broadcast(self, sid: str, ev: NostrEvent) -> None:
        for ws in list(self._conns):
            await ws.send(json.dumps(["EVENT", sid, ev.model_dump(mode="json")]))

    async def _handle(self, ws: ServerConnection) -> None:
        self._conns.add(ws)
        authed = not self.require_auth
        if self.require_auth:
            await ws.send(json.dumps(["AUTH", self.challenge]))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.requests.append(msg)
                if msg[0] == "AUTH":
                    ev = event_from_json(msg[1])
                    ok = False
                    try:
                        verify_event(ev)
                        ok = ev.kind == 22242 and ("challenge", self.challenge) in ev.tags
                    except NostrError:
                        ok = False
                    if ok:
                        authed = True
                        self.auth_events.append(ev)
                    await ws.send(json.dumps(["OK", ev.id, ok, "" if ok else "invalid: bad auth"]))
                elif msg[0] == "EVENT":
                    ev = event_from_json(msg[1])
                    if self.drop_ok:
                        continue
                    if not authed:
                        await ws.send(json.dumps(["OK", ev.id, False, "auth-required: sign in"]))
                        continue
                    self.events[ev.id] = ev
                    await ws.send(json.dumps(["OK", ev.id, True, ""]))
                elif msg[0] == "REQ":
                    sid, filters = msg[1], msg[2:]
                    if not authed:
                        await ws.send(json.dumps(["CLOSED", sid, "auth-required: sign in"]))
                        continue
                    for ev in list(self.events.values()):
                        if any(_matches(ev, f) for f in filters):
                            await ws.send(json.dumps(["EVENT", sid, ev.model_dump(mode="json")]))
                    await ws.send(json.dumps(["EOSE", sid]))
        except Exception:  # connection dropped by the test
            await asyncio.sleep(0)
        finally:
            self._conns.discard(ws)
