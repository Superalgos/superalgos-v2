"""Shared fixtures: an isolated key directory, throwaway passphrase, an in-process relay on its own thread."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cli_support import Cli, ThreadedRelay


@pytest.fixture
def relay() -> Iterator[ThreadedRelay]:
    with ThreadedRelay() as r:
        yield r


@pytest.fixture
def cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(tmp_path, capsys)


@pytest.fixture
def demo(cli: Cli) -> Cli:
    """A home with a device key and a demo identity holding 1,000 fixture SA."""
    cli.ok("keys", "init")
    cli.ok("identity", "--demo", "--tokens", "1000", "--balances", cli.root / "balances.json")
    return cli
