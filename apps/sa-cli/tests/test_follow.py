"""sa follow: signal files (off-grid), relay mode against an in-process relay, error messages."""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cli_support import Cli, ThreadedRelay
from sa_cli import cmd_follow
from sa_core.models import Signal
from sa_core.power import DEFAULT_TIERS, ruleset_event, sign_ruleset
from sa_core.signals import build_signal_event
from sa_core.trackrecord import Candle
from sa_engine.network.ledger import Ledger
from sa_engine.network.replay import ReplaySpec


def candles(spec: ReplaySpec) -> list[Candle]:
    t0 = int(time.time() * 1000) - 60_000
    return [
        Candle(
            open_time_ms=t0 + i * 1000,
            open=Decimal(50_000),
            high=Decimal(50_100),
            low=Decimal(49_900),
            close=Decimal(50_000),
        )
        for i in range(600)
    ]


@pytest.fixture
def spec_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "replay.json"
    ReplaySpec(
        instrument="BINANCE:BTCUSDT",
        start="2026-09-01",
        end="2026-09-02",
        speed=1,
        wall0_ms=int(time.time() * 1000),
    ).save(path)
    monkeypatch.setattr(cmd_follow, "load_replay_candles", lambda spec, **kw: candles(spec))
    return path


def provider_signal(demo: Cli, seq: int = 1, prev: str | None = None, **kw: Any) -> tuple[str, str]:
    """A signal event signed by this home's keys (the home doubles as provider), as JSON."""
    h = demo.home()
    device, delegation = h.load_device(), h.load_delegation(h.load_device())
    base: dict[str, Any] = {
        "strategy_id": "s", "seq": seq, "prev": prev, "instrument": "BINANCE:BTCUSDT", "side": "buy", "action": "open",
        "size_fraction": 0.5, "order_type": "market", "timeframe": 3600, "created_ms": int(time.time() * 1000),
    }  # fmt: skip
    merged = {**base, **kw}
    merged.setdefault("bar_open_ms", merged["created_ms"] - merged["timeframe"] * 1000)
    ev = build_signal_event(device, Signal.model_validate(merged), delegation)
    return ev.model_dump_json(), device.pubkey_hex


def test_a_signal_file_is_verified_and_executed(demo: Cli, spec_file: Path, tmp_path: Path) -> None:
    text, provider = provider_signal(demo)
    file = tmp_path / "signal.json"
    file.write_text(text)
    out = demo.ok(
        "follow",
        "--signal-file",
        file,
        "--provider",
        provider,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l.sqlite",
    )
    assert "seq 1: accepted, 0.1 @" in out
    row = Ledger(tmp_path / "l.sqlite").rows()[0]
    assert row.source == "file" and row.filled and row.qty == "0.1"


def test_tampered_or_foreign_signal_files_are_rejected_with_exit_1(
    demo: Cli, spec_file: Path, tmp_path: Path
) -> None:
    text, provider = provider_signal(demo)
    bad = tmp_path / "bad.json"
    bad.write_text(text.replace("0.5", "0.9"))
    code, out, _ = demo.run(
        "follow",
        "--signal-file",
        bad,
        "--provider",
        provider,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l.sqlite",
    )
    assert code == 1 and "rejected: verification failed" in out
    foreign = tmp_path / "foreign.json"
    foreign.write_text(text)
    err = demo.fail(
        "follow",
        "--signal-file",
        foreign,
        "--provider",
        "ab" * 32,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l2.sqlite",
    )
    assert "pinned provider" in err
    junk = tmp_path / "junk.json"
    junk.write_text("hello")
    assert "not a Nostr event" in demo.fail(
        "follow",
        "--signal-file",
        junk,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l3.sqlite",
    )
    assert "cannot read signal file" in demo.fail(
        "follow",
        "--signal-file",
        tmp_path / "none.json",
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l4.sqlite",
    )


def test_a_rule_set_file_enforces_the_timeframe(demo: Cli, spec_file: Path, tmp_path: Path) -> None:
    text, provider = provider_signal(demo)
    h = demo.home()
    rules = sign_ruleset(h.load_sa(), provider, 900, tiers=DEFAULT_TIERS)
    rfile = tmp_path / "rules.json"
    rfile.write_text(rules.model_dump_json())
    file = tmp_path / "signal.json"
    file.write_text(text)
    code, out, _ = demo.run(
        "follow",
        "--signal-file",
        file,
        "--ruleset",
        rfile,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l.sqlite",
    )
    assert code == 1 and "timeframe" in out
    rfile.write_text("nope")
    assert "cannot read the rule set" in demo.fail(
        "follow",
        "--signal-file",
        file,
        "--ruleset",
        rfile,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l2.sqlite",
    )


def test_follow_needs_somebody_to_follow_and_a_relay(demo: Cli, spec_file: Path) -> None:
    assert "name at least one provider" in demo.fail("follow", "--replay-spec", spec_file)
    assert "--relay URL is required" in demo.fail(
        "follow", "--provider", "ab" * 32, "--replay-spec", spec_file
    )
    assert "64-character hex" in demo.fail(
        "follow", "--provider", "xyz", "--relay", "ws://x", "--replay-spec", spec_file
    )
    assert "needs --replay-spec" in demo.fail(
        "follow", "--provider", "ab" * 32, "--relay", "ws://x", "--executor", "sim"
    )


def test_follow_over_the_relay_executes_and_keeps_a_ledger(
    demo: Cli, spec_file: Path, relay: ThreadedRelay, tmp_path: Path
) -> None:
    h = demo.home()
    device, delegation = h.load_device(), h.load_delegation(h.load_device())
    sa = h.load_sa()
    relay.inject(
        ruleset_event(
            device, sign_ruleset(sa, device.pubkey_hex, 3600, tiers=DEFAULT_TIERS), delegation
        )
    )
    ready = tmp_path / "ready"

    def publish_later() -> None:
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        text, _ = provider_signal(demo)
        relay.inject(
            build_signal_event(
                device, Signal.model_validate_json(json.loads(text)["content"]), delegation
            )
        )

    t = threading.Thread(target=publish_later)
    t.start()
    out = demo.ok("follow", "--relay", relay.url, "--provider", device.pubkey_hex, "--replay-spec", spec_file, "--ledger", tmp_path / "l.sqlite",
                  "--ready-file", ready, "--duration", "3", "--reorder-wait", "0")  # fmt: skip
    t.join()
    assert (
        "ready: following 1 provider(s)" in out and '"accepted": 1' in out and '"filled": 1' in out
    )
    assert Ledger(tmp_path / "l.sqlite").rows()[0].status == "accepted"


def test_follow_reports_a_provider_without_a_rule_set(
    demo: Cli, spec_file: Path, relay: ThreadedRelay, tmp_path: Path
) -> None:
    err = demo.fail(
        "follow",
        "--relay",
        relay.url,
        "--provider",
        "ab" * 32,
        "--replay-spec",
        spec_file,
        "--ledger",
        tmp_path / "l.sqlite",
        "--duration",
        "2",
    )
    assert "has no rule set" in err


def test_the_home_can_be_missing_its_delegation(cli: Cli) -> None:
    cli.ok("keys", "init")
    assert "no delegation" in cli.fail("follow", "--provider", "ab" * 32, "--relay", "ws://x")
