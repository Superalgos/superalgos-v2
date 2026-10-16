"""Helpers, configuration, relay paging, candle sources and the command line."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import sa_api.candles as candles_module
from api_support import ThreadedRelay
from sa_api import relay_data
from sa_api.anchors import tsa_verifiers
from sa_api.candles import CandleSource
from sa_api.config import ApiConfig, ConfigError
from sa_api.main import build_parser, config_from, main
from sa_api.service import completeness, downsample, gap_runs, iso, tiers_of
from sa_core.bundle import BundleCheck
from sa_core.keys import DeviceKey
from sa_core.models import SIGNAL_KIND, Tier
from sa_core.nostr import build_event
from sa_core.trackrecord import Candle
from sa_engine.network.replay import ReplayError, ReplaySpec


def test_downsample_keeps_first_and_last_and_never_exceeds_the_limit() -> None:
    pts = list(range(1000))
    out = downsample(pts, 50)
    assert len(out) == 50 and out[0] == 0 and out[-1] == 999
    assert out == sorted(set(out))
    assert downsample([1, 2, 3], 50) == [1, 2, 3]
    assert downsample([], 10) == []


def test_gap_runs_group_consecutive_missing_seqs_per_strategy() -> None:
    gaps = ["a:3", "a:4", "a:7", "b:2", "a:5"]
    assert [(g.after_seq, g.missing) for g in gap_runs(gaps)] == [(2, 3), (6, 1), (1, 1)]
    assert gap_runs([]) == []
    assert gap_runs(["a:b:9"])[0].after_seq == 8  # strategy ids may contain colons


def check(**kw: Any) -> BundleCheck:
    base: dict[str, Any] = {
        "complete": False,
        "head_checked": False,
        "items": (),
        "gaps": (),
        "problems": (),
        "unrevealed": (),
    }
    return BundleCheck(**{**base, **kw})


def test_completeness_is_one_only_for_a_complete_chain() -> None:
    assert completeness(check(complete=True)) == 1.0
    assert completeness(check()) == 0.0  # nothing to go on
    only_gap = completeness(check(gaps=("a:2",)))
    assert 0 <= only_gap < 1


def test_iso_is_utc_with_z() -> None:
    assert iso(0) == "1970-01-01T00:00:00Z"


def test_tiers_of_orders_best_first_and_private_has_one_tier() -> None:
    from sa_core.keys import SAKey
    from sa_core.models import PrivateRule
    from sa_core.power import sign_ruleset

    sa = SAKey.generate()
    rules = sign_ruleset(
        sa,
        "a" * 64,
        60,
        tiers=[Tier(min_power=0, delay_fraction=2.0), Tier(min_power=5, delay_fraction=0.0)],
    )
    assert [t.min_power for t in tiers_of(rules)] == [5, 0]
    priv = sign_ruleset(sa, "a" * 64, 60, private=PrivateRule(min_power=7))
    assert [(t.min_power, t.delay_fraction) for t in tiers_of(priv)] == [(7, 0.0)]


# ---------------------------------------------------------------- configuration


def test_config_load_errors_say_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        ApiConfig.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"time_scale": -1}')
    with pytest.raises(ConfigError, match="time_scale"):
        ApiConfig.load(bad)
    bad.write_text('{"nonsense": 1}')
    with pytest.raises(ConfigError, match="nonsense"):
        ApiConfig.load(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"relays": ["ws://x"], "labels": {"a": "b"}}))
    assert ApiConfig.load(good).relays == ("ws://x",)


def test_command_line_overrides_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = build_parser().parse_args(
        ["--relay", "ws://r1", "--relay", "ws://r2", "--web-dist", str(tmp_path)]
    )
    assert args.host == "127.0.0.1" and args.port == 8787  # local only unless asked otherwise
    cfg = config_from(args)
    assert cfg.relays == ("ws://r1", "ws://r2") and cfg.web_dist == tmp_path
    assert config_from(build_parser().parse_args([])).relays == ("ws://127.0.0.1:7781",)
    assert main(["--web-dist", str(tmp_path / "missing")]) == 2
    assert "bun run build" in capsys.readouterr().err


def test_main_serves_with_uvicorn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("sa_api.main.uvicorn.run", lambda app, **kw: seen.update(kw))
    assert main(["--port", "9999"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9999
    assert "read-only" in capsys.readouterr().out


# ---------------------------------------------------------------- anchors


def test_tsa_verifiers_default_and_errors(tmp_path: Path, farm_config: Path) -> None:
    assert "tsa" in tsa_verifiers(None)
    assert "tsa" in tsa_verifiers(farm_config)
    with pytest.raises(ConfigError, match="cannot read the TSA config"):
        tsa_verifiers(tmp_path / "nope.json")
    broken = tmp_path / "tsa.json"
    broken.write_text('{"k": "two"}')
    with pytest.raises(ConfigError, match="TSA config"):
        tsa_verifiers(broken)
    broken.write_text(json.dumps({"trust_dir": str(tmp_path / "empty")}))
    with pytest.raises(ConfigError, match="trust bundle"):
        tsa_verifiers(broken)


# ---------------------------------------------------------------- candles


def candle(i: int) -> Candle:
    return Candle(
        open_time_ms=i * 60_000, open=Decimal(1), high=Decimal(2), low=Decimal(1), close=Decimal(1)
    )


def test_candles_come_from_the_replay_spec_and_are_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / "replay.json"
    ReplaySpec(
        instrument="BINANCE:BTCUSDT",
        start="2026-09-01",
        end="2026-09-02",
        speed=100,
        wall0_ms=1_700_000_000_000,
    ).save(spec)
    calls: list[int] = []

    def fake(s: ReplaySpec, **kw: Any) -> list[Candle]:
        calls.append(1)
        return [candle(1), candle(2)]

    monkeypatch.setattr(candles_module, "load_replay_candles", fake)
    src = CandleSource(ApiConfig(replay_specs={"p" * 64: spec}))
    first = src.load("p" * 64, ["BINANCE:BTCUSDT"], 0, 1)
    assert list(first) == ["BINANCE:BTCUSDT"] and len(first["BINANCE:BTCUSDT"]) == 2
    src.load("p" * 64, ["BINANCE:BTCUSDT"], 0, 1)
    assert len(calls) == 1  # same spec file, same mtime: loaded once
    gone = CandleSource(ApiConfig(replay_specs={"q" * 64: tmp_path / "nope.json"}))
    with pytest.raises(ReplayError, match="has the provider started"):
        gone.load("q" * 64, ["BINANCE:BTCUSDT"], 0, 1)


def test_candles_without_a_replay_come_from_binance_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, int, int]] = []

    def fake(symbol: str, start_ms: int, end_ms: int, **kw: Any) -> list[Candle]:
        asked.append((symbol, start_ms, end_ms))
        return [candle(1)]

    monkeypatch.setattr(candles_module, "fetch_rest_candles", fake)
    src = CandleSource(ApiConfig())
    out = src.load("x" * 64, ["BINANCE:ETHUSDT"], 1_700_000_000_000, 1_700_000_600_000)
    assert out["BINANCE:ETHUSDT"] == [candle(1)]
    assert (
        asked[0][0] == "ETHUSDT" and asked[0][1] % 60_000 == 0 and asked[0][1] < 1_700_000_000_000
    )
    src.load("x" * 64, ["BINANCE:ETHUSDT"], 1_700_000_000_000, 1_700_000_600_000)
    assert len(asked) == 1  # cached for a few minutes


# ---------------------------------------------------------------- relay reads


def test_snapshot_pages_through_a_relay_and_reports_unreachable_ones(
    relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = DeviceKey.generate()
    events = [
        build_event(key, SIGNAL_KIND, f'{{"n": {i}}}', [], 1_700_000_000 + i) for i in range(7)
    ]
    for ev in events:
        relay.inject(ev)
    monkeypatch.setattr(relay_data, "PAGE", 3)

    async def go() -> relay_data.Snapshot:
        return await relay_data.fetch_snapshot([relay.url, "ws://127.0.0.1:1"], kinds=[SIGNAL_KIND])

    snap = asyncio.run(go())
    assert snap.answered == [relay.url]
    assert (
        "ws://127.0.0.1:1" in snap.unreachable
        and "cannot connect" in snap.unreachable["ws://127.0.0.1:1"]
    )
    # the fake relay ignores `limit`/`until`, so it returns everything on every page: dedupe by id keeps 7
    assert sorted(e.created_at for e in snap.of_kind(SIGNAL_KIND)) == [
        1_700_000_000 + i for i in range(7)
    ]


def test_a_relay_that_serves_a_forged_event_does_not_get_it_into_the_snapshot(
    relay: ThreadedRelay,
) -> None:
    key = DeviceKey.generate()
    good = build_event(key, SIGNAL_KIND, "{}", [], 1_700_000_000)
    other = build_event(key, SIGNAL_KIND, '{"x": 1}', [], 1_700_000_001)
    forged = other.model_copy(update={"content": "tampered"})  # id and signature no longer match
    relay.inject(good)
    relay.inject(forged)

    snap = asyncio.run(relay_data.fetch_snapshot([relay.url], kinds=[SIGNAL_KIND]))
    assert [e.id for e in snap.of_kind(SIGNAL_KIND)] == [good.id]
    assert asyncio.run(relay_data.relay_ok(relay.url)) is True
    assert asyncio.run(relay_data.relay_ok("ws://127.0.0.1:1")) is False
