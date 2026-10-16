"""sa provider register | run | export against an in-process relay."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cli_helpers import FakeSettings, intents, strategy_file
from cli_support import Cli, ThreadedRelay
from sa_cli import cmd_provider
from sa_core.bundle import read_bundle
from sa_core.models import ANCHOR_KIND, COMMITMENT_KIND, RULESET_KIND, SIGNAL_KIND
from sa_core.signals import verify_signal_event
from sa_engine.network.replay import ReplayError, ReplaySpec


@pytest.fixture
def strategy(tmp_path: Path) -> Path:
    return strategy_file(tmp_path)


def register(demo: Cli, strategy: Path, relay: ThreadedRelay, *extra: str) -> Path:
    state = demo.root / "state"
    demo.ok("provider", "register", strategy, "--relay", relay.url, "--state", state, *extra)
    return state


def test_register_signs_and_publishes_the_default_tiers(
    demo: Cli, strategy: Path, relay: ThreadedRelay
) -> None:
    state = register(demo, strategy, relay)
    ruleset = relay.events(RULESET_KIND)
    assert len(ruleset) == 1
    rules = json.loads((state / "ruleset.json").read_text())
    assert rules["timeframe"] == 3600 and [t["min_power"] for t in rules["tiers"]] == [
        500000,
        100000,
        10000,
        0,
    ]


def test_register_custom_and_private_rule_sets(
    demo: Cli, strategy: Path, relay: ThreadedRelay
) -> None:
    state = register(demo, strategy, relay, "--tier", "1000@0", "--tier", "0@1.5")
    assert [
        t["delay_fraction"] for t in json.loads((state / "ruleset.json").read_text())["tiers"]
    ] == [0.0, 1.5]
    out = demo.ok(
        "provider",
        "register",
        strategy,
        "--relay",
        relay.url,
        "--state",
        demo.root / "p2",
        "--private",
        "5000",
    )
    assert "private (threshold 5000 SA)" in out


def test_register_errors_are_short_and_say_what_to_do(
    demo: Cli, strategy: Path, relay: ThreadedRelay, tmp_path: Path
) -> None:
    assert "MIN_POWER@DELAY_FRACTION" in demo.fail(
        "provider", "register", strategy, "--tier", "lots", "--relay", relay.url
    )
    assert "invalid rule set" in demo.fail(
        "provider", "register", strategy, "--tier", "10@2", "--tier", "20@3", "--relay", relay.url
    )
    assert "cannot read strategy" in demo.fail("provider", "register", tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 2, "name": "x", "timeframe": "7s", "stages": {}}))
    err = demo.fail("provider", "register", bad)
    assert "not a valid strategy" in err and "timeframe" in err
    with ThreadedRelay(reject=lambda ev: "blocked: no") as refusing:
        assert "refused the event" in demo.fail(
            "provider", "register", strategy, "--relay", refusing.url, "--state", demo.root / "s9"
        )


def run_replay(
    demo: Cli,
    strategy: Path,
    relay: ThreadedRelay,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    n: int = 3,
) -> Path:
    state = register(demo, strategy, relay)
    hist0 = ReplaySpec(
        instrument="BINANCE:BTCUSDT", start="2026-09-01", end="2026-09-02", speed=1, wall0_ms=1
    ).clock.hist0_ms
    monkeypatch.setattr(cmd_provider, "collect_intents", lambda *a, **k: intents(hist0, n))
    demo.ok("provider", "run", strategy, "--relay", relay.url, "--state", state, "--replay", "2026-09-01", "2026-09-02",
            "--speed", "3600", "--lead", "0.2", *extra)  # fmt: skip
    return state


def test_run_replay_publishes_a_verifiable_chain_and_exports_the_bundle(
    demo: Cli, strategy: Path, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = FakeSettings()
    monkeypatch.setattr(cmd_provider, "load_tsa_settings", lambda _: settings)
    bundle = demo.root / "bundle.json"
    state = run_replay(demo, strategy, relay, monkeypatch, "--export", str(bundle))
    signals = sorted(relay.events(SIGNAL_KIND), key=lambda e: e.created_at)
    assert len(signals) == 3 and [
        verify_signal_event(e, int(time.time())).signal.seq for e in signals
    ] == [1, 2, 3]
    assert len(relay.events(ANCHOR_KIND)) == 3 and settings.fake.calls >= 1
    exported = read_bundle(bundle)
    assert len(exported.events) == 3 and len(exported.proofs) == 3
    spec = ReplaySpec.load(state / "replay.json")
    assert spec.speed == 3600 and spec.instrument == "BINANCE:BTCUSDT"
    again = demo.root / "again.json"
    assert "3 events, 3 proofs" in demo.ok("provider", "export", "--state", state, "-o", again)
    assert read_bundle(again).events == exported.events


def test_run_prints_a_json_summary(
    demo: Cli,
    strategy: Path,
    relay: ThreadedRelay,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = register(demo, strategy, relay)
    capsys.readouterr()
    hist0 = ReplaySpec(
        instrument="BINANCE:BTCUSDT", start="2026-09-01", end="2026-09-02", speed=1, wall0_ms=1
    ).clock.hist0_ms
    monkeypatch.setattr(cmd_provider, "collect_intents", lambda *a, **k: intents(hist0, 2))
    code, out, _ = demo.run("provider", "run", strategy, "--relay", relay.url, "--state", state, "--replay", "2026-09-01", "2026-09-02",
                            "--speed", "3600", "--lead", "0.1", "--no-anchor")  # fmt: skip
    assert code == 0
    summary = json.loads(out.strip().splitlines()[-1])
    assert (
        summary["signals"] == 2
        and summary["sent"] == 2
        and summary["rejected"] == []
        and summary["anchored"] == 0
    )


def test_run_exits_nonzero_when_the_relay_refuses_signals(
    demo: Cli, strategy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ThreadedRelay(
        reject=lambda ev: "blocked: closed" if ev.kind == SIGNAL_KIND else None
    ) as r:
        state = register(demo, strategy, r)
        hist0 = ReplaySpec(
            instrument="BINANCE:BTCUSDT", start="2026-09-01", end="2026-09-02", speed=1, wall0_ms=1
        ).clock.hist0_ms
        monkeypatch.setattr(cmd_provider, "collect_intents", lambda *a, **k: intents(hist0, 1))
        code, out, _ = demo.run("provider", "run", strategy, "--relay", r.url, "--state", state, "--replay", "2026-09-01", "2026-09-02",
                                "--speed", "3600", "--lead", "0.1", "--no-anchor")  # fmt: skip
    assert code == 1 and "blocked: closed" in out


def test_private_run_commits_and_reveals_on_export(
    demo: Cli, strategy: Path, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = demo.root / "state"
    demo.ok(
        "provider", "register", strategy, "--relay", relay.url, "--state", state, "--private", "100"
    )
    hist0 = ReplaySpec(
        instrument="BINANCE:BTCUSDT", start="2026-09-01", end="2026-09-02", speed=1, wall0_ms=1
    ).clock.hist0_ms
    monkeypatch.setattr(cmd_provider, "collect_intents", lambda *a, **k: intents(hist0, 2))
    bundle = demo.root / "private.json"
    demo.ok("provider", "run", strategy, "--relay", relay.url, "--state", state, "--replay", "2026-09-01", "2026-09-02", "--speed", "3600",
            "--lead", "0.1", "--no-anchor", "--balances", demo.root / "balances.json", "--export", bundle, "--reveal")  # fmt: skip
    assert (
        len(relay.events(COMMITMENT_KIND)) == 2 and relay.events(SIGNAL_KIND) == []
    )  # nobody is entitled: commitments only
    assert len(read_bundle(bundle).reveals) == 2
    bare = demo.root / "bare.json"
    demo.ok("provider", "export", "--state", state, "-o", bare)
    assert read_bundle(bare).reveals == ()


def test_run_refuses_inconsistent_setups(
    demo: Cli, strategy: Path, relay: ThreadedRelay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "choose one" in demo.fail("provider", "run", strategy)
    assert "choose one" in demo.fail(
        "provider", "run", strategy, "--paper", "--replay", "2026-09-01", "2026-09-02"
    )
    assert "sa provider register" in demo.fail(
        "provider",
        "run",
        strategy,
        "--paper",
        "--state",
        tmp_path / "unregistered",
        "--relay",
        relay.url,
    )
    state = register(demo, strategy, relay)
    (tmp_path / "fast").mkdir()
    fast = strategy_file(
        tmp_path / "fast", {**json.loads(strategy.read_text()), "timeframe": "15m"}
    )
    assert "binds 3600s" in demo.fail(
        "provider", "run", fast, "--state", state, "--paper", "--relay", relay.url
    )
    other = demo.root / "state2"
    other.mkdir()
    (other / "ruleset.json").write_text(
        (state / "ruleset.json")
        .read_text()
        .replace(json.loads((state / "ruleset.json").read_text())["provider"], "ab" * 32)
    )
    assert "but this home's device key" in demo.fail(
        "provider", "run", strategy, "--state", other, "--paper", "--relay", relay.url
    )
    monkeypatch.setattr(
        cmd_provider,
        "collect_intents",
        lambda *a, **k: (_ for _ in ()).throw(ReplayError("no candles")),
    )
    assert "cannot prepare the replay: no candles" in demo.fail(
        "provider",
        "run",
        strategy,
        "--state",
        state,
        "--replay",
        "2026-09-01",
        "2026-09-02",
        "--relay",
        relay.url,
        "--no-anchor",
    )


def test_export_needs_a_registered_state_directory(demo: Cli, tmp_path: Path) -> None:
    assert "sa provider register" in demo.fail(
        "provider", "export", "--state", tmp_path / "none", "-o", tmp_path / "b.json"
    )


def test_paper_mode_turns_every_live_order_into_a_signal(
    demo: Cli, strategy: Path, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sa_engine import paper

    state = register(demo, strategy, relay)
    seen: dict[str, Any] = {}

    def fake_run_paper(
        doc: Any,
        instrument: str,
        capital: float,
        *,
        duration_s: float | None,
        intent_hook: Any,
        log_level: str,
    ) -> None:
        seen.update(instrument=instrument, capital=capital, duration=duration_s)
        for it in intents(1_700_000_000_000, 2):
            intent_hook(it)  # called from the main thread here; Nautilus calls it from its own loop

    monkeypatch.setattr(paper, "run_paper", fake_run_paper)
    out = demo.ok(
        "provider",
        "run",
        strategy,
        "--relay",
        relay.url,
        "--state",
        state,
        "--paper",
        "--duration",
        "5",
        "--no-anchor",
    )
    assert seen == {"instrument": "BINANCE:BTCUSDT", "capital": 10000.0, "duration": 5.0}
    assert '"signals": 2' in out and len(relay.events(SIGNAL_KIND)) == 2


def test_paper_mode_reports_a_bot_that_cannot_start(
    demo: Cli, strategy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = demo.root / "state"
    with ThreadedRelay() as r:
        demo.ok("provider", "register", strategy, "--relay", r.url, "--state", state)
    err = demo.fail(
        "provider",
        "run",
        strategy,
        "--relay",
        "ws://127.0.0.1:1",
        "--state",
        state,
        "--paper",
        "--no-anchor",
    )
    assert "did not start" in err and "cannot connect" in err


def test_start_at_sets_the_replay_clock_for_several_providers(
    demo: Cli, strategy: Path, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cmd_provider, "load_tsa_settings", lambda _: FakeSettings())
    wall0 = time.time() + 0.3
    state = run_replay(demo, strategy, relay, monkeypatch, "--start-at", str(wall0))
    assert ReplaySpec.load(state / "replay.json").wall0_ms == int(wall0 * 1000)  # not now + --lead
