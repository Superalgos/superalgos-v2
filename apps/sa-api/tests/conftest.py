"""Fixtures: a live in-process relay seeded with a signed world, and a TSA farm for real anchor proofs."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api_support import ThreadedRelay
from sa_api.app import create_app
from sa_api.config import ApiConfig
from sa_api.service import Service
from sa_core.tsa_lab import tsa_farm
from world import MINUTE, StubCandles, World, candles_around, farm_stamper


@pytest.fixture(scope="session")
def farm_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    with tsa_farm(tmp_path_factory.mktemp("farm"), farm_gap_s=1.0) as cfg:
        yield cfg


@pytest.fixture
def relay() -> Iterator[ThreadedRelay]:
    with ThreadedRelay() as r:
        yield r


@pytest.fixture
def world(tmp_path: Path, farm_config: Path, relay: ThreadedRelay) -> World:
    """Signals created a few minutes ago and anchored just now, on a relay that serves them all."""
    w = World(tmp_path, now_ms=int(time.time() * 1000) // MINUTE * MINUTE)
    w.build(farm_stamper(farm_config))
    # anchored "now" while created_ms lies minutes back: widen nothing, the API config below sets the tolerance
    for ev in w.events:
        relay.inject(ev)
    w.add_vote_round()
    for ev in w.events[-3:]:
        relay.inject(ev)
    return w


@pytest.fixture
def config(world: World, relay: ThreadedRelay, farm_config: Path, tmp_path: Path) -> ApiConfig:
    return ApiConfig(
        relays=(relay.url,),
        balances=world.balances,
        tsa_config=farm_config,
        governance_anchor="created-at",
        delay_d_s=0,
        tolerance_s=3600,  # the signals are minutes old when the TSAs stamp them in this fixture
        time_scale=1 / 3600,
        cache_ttl_s=0.0,
        bundles_dir=tmp_path / "bundles",
        backtests_dir=tmp_path / "backtests",
    )


@pytest.fixture
def service(config: ApiConfig, world: World) -> Service:
    return Service(config, candles=StubCandles(candles_around(world.now_ms - 20 * MINUTE, 120)))


@pytest.fixture
def client(config: ApiConfig, service: Service) -> Iterator[TestClient]:
    with TestClient(create_app(config, service)) as c:
        yield c
