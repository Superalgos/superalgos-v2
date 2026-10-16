"""Helpers shared by the CLI tests (a uniquely named module: bare `pytest` at the repo root runs every package's tests)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from cli_fake_relay import FakeRelay
from sa_cli.home import Home
from sa_cli.main import main
from sa_core.models import NostrEvent

PASSPHRASE = "cli-test-passphrase"


class ThreadedRelay:
    """FakeRelay running on a background event loop, so synchronous `main()` calls can talk to it."""

    def __init__(self, **kw: Any) -> None:
        self.relay = FakeRelay(**kw)
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

    def inject(self, ev: NostrEvent) -> None:
        asyncio.run_coroutine_threadsafe(self.relay.inject(ev), self.loop).result(10)

    def events(self, kind: int | None = None) -> list[NostrEvent]:
        return [e for e in list(self.relay.events.values()) if kind is None or e.kind == kind]


class Cli:
    """`sa` in-process with its own home directory and environment."""

    def __init__(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.root, self.capsys = root, capsys
        self.env = {"SA_PASSPHRASE": PASSPHRASE, "SA_SCRYPT_N": "1024"}

    def home(self, name: str = "home") -> Home:
        return Home(self.root / name, environ=self.env)

    def run(
        self, *args: object, home: str = "home", env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        h = Home(self.root / home, environ={**self.env, **(env or {})}, interactive=lambda: False)
        code = main([str(a) for a in args], home=h)
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def ok(self, *args: object, home: str = "home") -> str:
        code, out, err = self.run(*args, home=home)
        assert code == 0, f"sa {' '.join(map(str, args))} -> {code}\n{out}\n{err}"
        return out

    def fail(self, *args: object, home: str = "home", code: int = 2) -> str:
        got, out, err = self.run(*args, home=home)
        assert got == code, f"expected exit {code}, got {got}\n{out}\n{err}"
        assert err.startswith("sa: error: "), err
        return err
