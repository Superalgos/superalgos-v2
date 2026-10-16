"""Helpers shared by the API tests (a uniquely named module: bare `pytest` at the repo root runs every package's tests)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from api_fake_relay import FakeRelay


class ThreadedRelay:
    def __init__(self) -> None:
        self.relay = FakeRelay(challenge=None)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> ThreadedRelay:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.relay.start(), self.loop).result(10)
        return self

    def __exit__(self, *_: object) -> None:
        asyncio.run_coroutine_threadsafe(self.relay.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    @property
    def url(self) -> str:
        return self.relay.url

    def inject(self, ev: Any) -> None:
        asyncio.run_coroutine_threadsafe(self.relay.inject(ev), self.loop).result(10)
