"""sa record and sa verify."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cli_helpers import FakeSettings
from cli_support import Cli
from sa_cli import cmd_record
from sa_core.bundle import export_bundle, write_bundle
from sa_core.delegation import sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import AnchorProof, Bundle, Signal
from sa_core.signals import build_commitment_event, build_private_signal_event, build_signal_event
from sa_core.trackrecord import Candle
from sa_engine.network.replay import ReplaySpec


def make_chain(n: int = 4) -> tuple[Bundle, DeviceKey, SAKey]:
    sa, device = SAKey.generate(), DeviceKey.generate()
    delegation = sign_delegation(sa, device.pubkey_hex, 4_000_000_000)
    now_ms = int(time.time() * 1000)
    events, prev = [], None
    for i in range(n):
        buy = i % 2 == 0
        sig = Signal(
            strategy_id="s", seq=i + 1, prev=prev, instrument="BINANCE:BTCUSDT", side="buy" if buy else "sell",
            action="open" if buy else "close", size_fraction=0.5 if buy else 1.0, order_type="market", timeframe=3600,
            created_ms=now_ms + i * 1000,
            bar_open_ms=now_ms + i * 1000 - 3_600_000,
        )  # fmt: skip
        ev = build_signal_event(device, sig, delegation)
        events.append(ev)
        prev = ev.id
    proofs = [AnchorProof(kind="fake", digest=e.id, data="{}") for e in events]
    return export_bundle(device.pubkey_hex, events, proofs, exported_at=1), device, sa


@pytest.fixture
def bundle_file(tmp_path: Path) -> Path:
    bundle, _, _ = make_chain()
    path = tmp_path / "bundle.json"
    write_bundle(path, bundle)
    return path


def fake_candles(*_: object, **__: object) -> list[Candle]:
    t0 = int(time.time() * 1000) - 120_000
    return [
        Candle(open_time_ms=t0 + i * 500, open=Decimal(100 + i), high=Decimal(101 + i), low=Decimal(99 + i), close=Decimal(100 + i))
        for i in range(200)
    ]  # fmt: skip


def test_verify_accepts_a_complete_bundle_and_flags_every_kind_of_tampering(
    cli: Cli, bundle_file: Path, tmp_path: Path
) -> None:
    out = cli.ok("verify", bundle_file)
    assert "4 chain items" in out and "bundle is complete" in out
    raw = json.loads(bundle_file.read_text())
    mutations: dict[str, Callable[[dict[str, Any]], object]] = {
        "dropped": lambda d: d["events"].pop(1),
        "reordered": lambda d: d["events"].insert(0, d["events"].pop(2)),
        "edited": lambda d: d["events"][2].__setitem__(
            "content", d["events"][2]["content"].replace("0.5", "0.9", 1)
        ),
    }
    for label, mutate in mutations.items():
        doc = json.loads(json.dumps(raw))
        mutate(doc)
        bad = tmp_path / f"{label}.json"
        bad.write_text(json.dumps(doc))
        code, text, _ = cli.run("verify", bad)
        assert code == 1 and "NOT complete" in text, label
    gappy = json.loads(json.dumps(raw))
    gappy["events"].pop(1)
    (tmp_path / "gap.json").write_text(json.dumps(gappy))
    assert "gap: s:2" in cli.run("verify", tmp_path / "gap.json")[1]


def test_verify_counts_only_proofs_that_verify(
    cli: Cli, bundle_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cmd_record, "load_tsa_settings", lambda _: FakeSettings())
    assert "4 anchored" in cli.ok("verify", bundle_file)
    doc = json.loads(bundle_file.read_text())
    doc["proofs"][0]["data"] = "forged"
    forged = bundle_file.with_name("forged.json")
    forged.write_text(json.dumps(doc))
    assert "3 anchored" in cli.run("verify", forged)[1]


def test_verify_checks_signal_files(cli: Cli, tmp_path: Path) -> None:
    bundle, _, sa = make_chain(1)
    event = bundle.events[0]
    file = tmp_path / "signal.json"
    file.write_text(event.model_dump_json())
    out = cli.ok("verify", file)
    assert "signal is valid: s seq 1 open buy" in out and sa.address in out
    code, text, _ = cli.run("verify", file, "--sa-address", SAKey.generate().address)
    assert code == 1 and "signal is INVALID" in text
    tampered = tmp_path / "t.json"
    tampered.write_text(
        event.model_copy(update={"content": event.content.replace("0.5", "0.9")}).model_dump_json()
    )
    assert cli.run("verify", tampered)[0] == 1


def test_verify_explains_private_signals_and_unknown_files(cli: Cli, tmp_path: Path) -> None:
    sa, device, other = SAKey.generate(), DeviceKey.generate(), DeviceKey.generate()
    delegation = sign_delegation(sa, device.pubkey_hex, 4_000_000_000)
    sig = Signal(strategy_id="s", seq=1, prev=None, instrument="BINANCE:BTCUSDT", side="buy", action="open",
                 size_fraction=0.5, order_type="market", timeframe=3600, created_ms=int(time.time() * 1000),
                 bar_open_ms=int(time.time() * 1000) - 3_600_000)  # fmt: skip
    private = tmp_path / "private.json"
    private.write_text(
        build_private_signal_event(device, sig, delegation, other.pubkey_hex).model_dump_json()
    )
    assert "private signal" in cli.fail("verify", private)
    unknown = tmp_path / "unknown.json"
    unknown.write_text('{"hello": 1}')
    assert "neither a bundle" in cli.fail("verify", unknown)
    text = tmp_path / "text.json"
    text.write_text("not json")
    assert "not JSON" in cli.fail("verify", text)
    assert build_commitment_event(device, sig, delegation).kind == 9318


def test_record_with_a_replay_spec_is_deterministic(
    cli: Cli, bundle_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / "replay.json"
    ReplaySpec(
        instrument="BINANCE:BTCUSDT",
        start="2026-09-01",
        end="2026-09-02",
        speed=60,
        wall0_ms=int(time.time() * 1000),
    ).save(spec)
    monkeypatch.setattr(cmd_record, "load_replay_candles", fake_candles)
    monkeypatch.setattr(cmd_record, "load_tsa_settings", lambda _: FakeSettings())
    first = cli.ok("record", bundle_file, "--replay-spec", spec)
    second = cli.ok("record", bundle_file, "--replay-spec", spec)
    assert first == second and first.endswith("\n")
    report = json.loads(first)
    assert report["signals"]["total"] == 4 and report["completeness"]["complete"] is True
    out = tmp_path / "record.json"
    assert "track record written" in cli.ok("record", bundle_file, "--replay-spec", spec, "-o", out)
    assert out.read_text() == first
    wrong = tmp_path / "eth.json"
    ReplaySpec(
        instrument="BINANCE:ETHUSDT", start="2026-09-01", end="2026-09-02", speed=60, wall0_ms=1
    ).save(wrong)
    assert "was of BINANCE:ETHUSDT" in cli.fail("record", bundle_file, "--replay-spec", wrong)


def test_record_without_a_replay_spec_uses_binance_rest_klines(
    cli: Cli, bundle_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, int]] = []

    def rest(symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
        calls.append((symbol, start_ms, end_ms))
        return fake_candles()

    monkeypatch.setattr(cmd_record, "fetch_rest_candles", rest)
    assert json.loads(cli.ok("record", bundle_file))["provider"]
    assert calls and calls[0][0] == "BTCUSDT"


def test_record_errors_are_short(cli: Cli, tmp_path: Path) -> None:
    assert "cannot read bundle" in cli.fail("record", tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert "not a Superalgos bundle" in cli.fail("record", bad)
    empty = tmp_path / "empty.json"
    write_bundle(empty, Bundle(provider="ab" * 32, events=(), exported_at=0))
    assert "holds no signal that can be scored" in cli.fail("record", empty)
