"""e2e fixtures: the real sarelay binary, a local TSA farm, and the two shared network runs.

Run with `make e2e` (offline apart from the Binance archive download on first use) or
`SA_ONLINE=1 make e2e` to include the live-TSA test. Timings are printed at the end of the run.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(
    0, str(ROOT / "packages" / "sa-core" / "tests")
)  # tsa_lab: throwaway-CA RFC 3161 tokens

from e2e_net import TIMINGS, build_relay  # noqa: E402
from e2e_scenario import FollowerSpec, Run, run_network  # noqa: E402
from e2e_tsa import tsa_farm  # noqa: E402

SPEED = 7200.0  # market seconds per wall second: a 7-day week replays in 84 s
WEEK = ("2026-09-01", "2026-09-08")  # real BTCUSDT 1m candles from data.binance.vision
TWO_DAYS = ("2026-09-01", "2026-09-03")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("SA_ONLINE") != "1":
        skip = pytest.mark.skip(reason="touches public TSAs: set SA_ONLINE=1")
        for item in items:
            if "online" in item.keywords:
                item.add_marker(skip)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if TIMINGS:
        terminalreporter.section("e2e timings (seconds)")
        for name, value in TIMINGS.items():
            terminalreporter.write_line(f"{name:<48} {value:8.2f}")


@pytest.fixture(scope="session")
def relay_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_relay(tmp_path_factory.mktemp("bin"))


@pytest.fixture(scope="session")
def data_dir() -> Path:
    path = Path(os.environ.get("SA_DATA_DIR", ROOT / ".sa-data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def tsa_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    with tsa_farm(tmp_path_factory.mktemp("tsa")) as cfg:
        yield cfg


@pytest.fixture(scope="session")
def public_run(
    relay_binary: Path, tsa_config: Path, data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Run]:
    """A week of real BTCUSDT 1m data replayed fast by one provider; followers at T0, T2 and zero power."""
    run = run_network(
        tmp_path_factory.mktemp("public"), relay_binary, label="public", speed=SPEED, window=WEEK, tsa_config=tsa_config, data_dir=data_dir,
        followers=[FollowerSpec("t0", 1_000_000), FollowerSpec("t2", 20_000), FollowerSpec("zero", 0, allocate=False)],
    )  # fmt: skip
    try:
        yield run
    finally:
        run.relay.stop()


@pytest.fixture(scope="session")
def private_run(
    relay_binary: Path, tsa_config: Path, data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Run]:
    """A private provider (threshold 10,000 SA): two entitled followers, one below the threshold, one with nothing."""
    run = run_network(
        tmp_path_factory.mktemp("private"), relay_binary, label="private", speed=SPEED, window=TWO_DAYS, tsa_config=tsa_config,
        data_dir=data_dir, private_min=10_000, reveal=True,
        followers=[
            FollowerSpec("rich", 1_000_000), FollowerSpec("mid", 20_000),
            FollowerSpec("low", 5_000), FollowerSpec("zero", 0, allocate=False),
        ],
    )  # fmt: skip
    try:
        yield run
    finally:
        run.relay.stop()
