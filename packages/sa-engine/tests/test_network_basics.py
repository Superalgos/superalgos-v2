"""Fixture oracle, replay clock and feed, journal, ledger."""

from __future__ import annotations

import itertools
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sa_core.chains import OracleError
from sa_core.keys import SAKey
from sa_core.models import AnchorProof

from sa_engine.network import fixture, replay
from sa_engine.network.executor import Execution
from sa_engine.network.journal import JournalEntry, JournalError, ProviderStore
from sa_engine.network.ledger import Ledger
from tests.conftest import kline_zip, synthetic_minutes
from tests.net_helpers import Identity, candles, flat_feed
from tests.test_data import FakeBinance

# ---------------------------------------------------------------- fixture oracle


def test_fixture_file_is_shared_with_the_oracle(tmp_path: Path) -> None:
    path = tmp_path / "balances.json"
    a, b = SAKey.generate().address, SAKey.generate().address
    fixture.set_balance(path, a, 1_000_000)
    fixture.set_balance(path, b, "20000.5")
    assert json.loads(path.read_text()) == {a: "1000000", b: "20000.5"}
    oracle = fixture.fixture_oracle(path)
    assert oracle.power(a).tokens == 1_000_000
    assert oracle.power(b).total == 20_000_500_000_000_000_000_000
    fixture.set_balance(path, a, 5)  # edit while "running": no cache
    assert oracle.power(a).tokens == 5
    assert oracle.power("0x" + "11" * 20).total == 0  # unknown address holds nothing


def test_fixture_errors_say_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(OracleError, match="not found"):
        fixture.read_balances(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("[1]")
    with pytest.raises(OracleError, match="JSON object"):
        fixture.read_balances(bad)
    bad.write_text("{nope")
    with pytest.raises(OracleError, match="not JSON"):
        fixture.read_balances(bad)
    bad.write_text('{"0xabc": "1"}')
    with pytest.raises(OracleError, match="not an address"):
        fixture.read_balances(bad)
    with pytest.raises(OracleError, match="negative"):
        fixture.set_balance(tmp_path / "ok.json", "0x" + "22" * 20, -1)
    bad.write_text("{nope")
    with pytest.raises(OracleError, match="not JSON"):
        fixture.set_balance(bad, "0x" + "22" * 20, 1)


# ---------------------------------------------------------------- replay clock, spec, feed


@given(st.integers(0, 10**9), st.floats(1, 30_000))
def test_clock_round_trips_within_one_wall_millisecond(offset_ms: int, speed: float) -> None:
    clock = replay.ReplayClock(hist0_ms=1_700_000_000_000, wall0_ms=1_800_000_000_000, speed=speed)
    hist = clock.hist0_ms + offset_ms
    assert abs(clock.to_hist(clock.to_wall(hist)) - hist) <= speed / 2 + 1


def test_wall_candles_stay_strictly_ascending_at_the_maximum_speed() -> None:
    clock = replay.ReplayClock(hist0_ms=0, wall0_ms=10**12, speed=replay.MAX_SPEED)
    mapped = replay.wall_candles(candles([1.0] * 500, 0), clock)
    assert all(b.open_time_ms > a.open_time_ms for a, b in itertools.pairwise(mapped))
    with pytest.raises(ValueError, match="less than or equal"):
        replay.ReplayClock(hist0_ms=0, wall0_ms=0, speed=replay.MAX_SPEED + 1)


def test_replay_spec_round_trip_and_errors(tmp_path: Path) -> None:
    spec = replay.ReplaySpec(
        instrument="BTCUSDT", start="2026-09-01", end="2026-09-08", speed=7200, wall0_ms=1_800_000_000_000
    )
    assert spec.symbol == "BTCUSDT" and spec.wall_duration_s() == 84.0
    assert spec.clock.hist0_ms == 1_788_220_800_000
    spec.save(tmp_path / "replay.json")
    assert replay.ReplaySpec.load(tmp_path / "replay.json") == spec
    with pytest.raises(replay.ReplayError, match="not found"):
        replay.ReplaySpec.load(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(replay.ReplayError, match="not a replay spec"):
        replay.ReplaySpec.load(tmp_path / "bad.json")
    with pytest.raises(ValueError, match="after start"):
        replay.ReplaySpec(instrument="BTCUSDT", start="2026-09-08", end="2026-09-01", speed=1, wall0_ms=1)


def test_price_feed_addresses_candles_by_wall_time() -> None:
    feed = replay.PriceFeed(candles([10, 20, 30], 1000, step_ms=100, spread=1))
    assert feed.price_at(0) == 10 and feed.price_at(1000) == 10  # before the replay: first candle
    assert feed.price_at(1150) == 20 and feed.price_at(10**9) == 30
    assert feed.range_between(1000, 1250) == (9.0, 31.0)
    assert feed.range_between(1250, 1000) == (29.0, 31.0)  # reversed span collapses to the later instant
    with pytest.raises(replay.ReplayError, match="at least one"):
        replay.PriceFeed([])


def test_load_candles_reads_the_verified_archive_for_the_window(tmp_path: Path) -> None:
    frame = synthetic_minutes(start="2025-01-01", days=3)
    fake = FakeBinance(kline_zip(frame))
    got = replay.load_candles("BTCUSDT", date(2025, 1, 2), date(2025, 1, 3), data_dir=tmp_path, fetch=fake)
    assert len(got) == 1440
    assert got[0].open_time_ms == 1_735_776_000_000  # 2025-01-02T00:00Z
    assert got[0].open == Decimal(repr(float(frame.iloc[1440]["open"])))
    with pytest.raises(replay.ReplayError, match="no BTCUSDT"):
        replay.load_candles("BTCUSDT", date(2025, 1, 20), date(2025, 1, 21), data_dir=tmp_path, fetch=fake)


# ---------------------------------------------------------------- journal


def _entry(ident: Identity, seq: int, prev: str | None, kind_text: str = "x") -> JournalEntry:
    from sa_core.models import Signal
    from sa_core.signals import build_signal_event

    sig = Signal(
        strategy_id="s", seq=seq, prev=prev, instrument="BINANCE:BTCUSDT", side="buy", action="open",
        size_fraction=0.5, order_type="market", timeframe=60, created_ms=1_700_000_000_000 + seq,
        bar_open_ms=1_700_000_000_000 + seq - 60_000,
    )  # fmt: skip
    ev = build_signal_event(ident.device, sig, ident.delegation)
    return JournalEntry(event=ev, strategy_id="s", seq=seq, ref=ev.id, run="r1", intent_index=seq - 1)


def test_journal_persists_chain_acks_and_proofs(tmp_path: Path) -> None:
    ident = Identity()
    store = ProviderStore(tmp_path / "state")
    e1 = _entry(ident, 1, None)
    e2 = _entry(ident, 2, e1.ref)
    store.append(e1, e2)
    store.mark_acked(e1.event.id)
    store.mark_acked(e1.event.id)  # idempotent
    store.add_proof(AnchorProof(kind="fake", digest=e1.event.id, data="{}"))
    again = ProviderStore(tmp_path / "state")
    head = again.head("s")
    assert head is not None and (head.seq, head.ref) == (2, e2.event.id)
    assert [e.id for e in again.unacked()] == [e2.event.id]
    assert again.progress("r1") == 2 and again.progress("other") == 0
    assert again.unanchored_refs() == [e2.event.id]
    assert again.counts()["signals"] == 2 and json.loads(again.dump())["proofs"] == 1
    assert again.acked_count() == 1 and len(again.proofs()) == 1 and len(again.entries()) == 2


def test_journal_refuses_a_jump_and_names_the_damaged_line(tmp_path: Path) -> None:
    ident = Identity()
    store = ProviderStore(tmp_path)
    e1 = _entry(ident, 1, None)
    store.append(e1)
    with pytest.raises(JournalError, match="jumps from seq 1 to 3"):
        store.append(_entry(ident, 3, e1.ref))
    (tmp_path / "events.jsonl").write_text("{cut off")
    with pytest.raises(JournalError, match=r"events\.jsonl:1"):
        ProviderStore(tmp_path)
    (tmp_path / "events.jsonl").write_text("")
    (tmp_path / "proofs.jsonl").write_text("nope\n")
    with pytest.raises(JournalError, match=r"proofs\.jsonl:1"):
        ProviderStore(tmp_path)


def test_ruleset_file_round_trip_and_errors(tmp_path: Path) -> None:
    store = ProviderStore(tmp_path)
    with pytest.raises(JournalError, match="sa provider register"):
        store.load_ruleset()
    rules = Identity().rules()
    store.save_ruleset(rules)
    assert store.load_ruleset() == rules
    store.ruleset_path.write_text("{}")
    with pytest.raises(JournalError, match="not a rule set"):
        store.load_ruleset()


# ---------------------------------------------------------------- ledger


def test_ledger_records_executions_anchors_and_chain_heads() -> None:
    led = Ledger(":memory:")
    led.record(
        event_id="e1",
        provider="p",
        source="relay",
        received_ms=10,
        status="accepted",
        strategy_id="s",
        seq=1,
        ref="e1",
        created_ms=5,
        signal_json="{}",
    )
    led.record(
        event_id="e2",
        provider="p",
        source="relay",
        received_ms=20,
        status="rejected",
        reason="gap: seq 2..2 missing before seq 3",
        strategy_id="s",
        seq=3,
        ref="e2",
    )
    led.record(
        event_id="e3",
        provider="p",
        source="relay",
        received_ms=30,
        status="rejected",
        reason="stale",
        strategy_id="s",
        seq=4,
    )
    assert led.head("p", "s") == (3, "e2")  # a gap-rejected signal still moves the head; a stale one does not
    assert led.head("p", "other") is None
    led.set_execution(
        Execution("e1", True, True, qty="0.5", price=100.0, fee=0.1, ts_ms=11, equity_before=1000.0, ref_price=99.0)
    )
    led.set_anchor("e1", "ok", 99)
    row = led.get("e1")
    assert row is not None and row.filled and row.qty == "0.5" and row.anchor_status == "ok" and row.anchored_s == 99
    assert [r.event_id for r in led.rows()] == ["e1", "e2", "e3"]
    assert [r.event_id for r in led.rows("rejected")] == ["e2", "e3"]
    assert led.by_ref("e2")[0].event_id == "e2" and led.get("nope") is None
    led.record(
        event_id="e1",
        provider="p",
        source="relay",
        received_ms=99,
        status="accepted",
        reason="again",
        strategy_id="s",
        seq=1,
        ref="e1",
    )
    again = led.get("e1")
    assert again is not None and again.received_ms == 10 and again.reason == "again"  # receive time is kept
    led.close()


def test_ledger_file_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "ledger.sqlite"
    led = Ledger(path)
    led.record(
        event_id="e1", provider="p", source="file", received_ms=1, status="accepted", strategy_id="s", seq=1, ref="e1"
    )
    led.close()
    assert Ledger(path).head("p", "s") == (1, "e1")


def test_flat_feed_helper_is_flat() -> None:
    assert flat_feed(123.0).price_at(5) == 123.0


def test_collect_intents_runs_the_strategy_over_the_catalog_and_returns_every_order(fixture_catalog: object) -> None:
    from sa_engine.network.replay import collect_intents
    from sa_engine.schema import validate_strategy
    from tests.strategies import ev

    doc = validate_strategy(
        {
            "schema_version": 2, "name": "fast", "timeframe": "1h",
            "stages": {
                "trigger": {"on": ev(("always", ["true"]))},
                "open": {"when": ev(("up", ["cross_above(ema(close, 2), ema(close, 5))"])),
                         "orders": [{"id": "e", "type": "market", "size_fraction": 0.5, "stop_loss_pct": 2}]},
                "close": {"when": ev(("down", ["cross_below(ema(close, 2), ema(close, 5))"]))},
            },
        }
    )  # fmt: skip
    intents = collect_intents(doc, "BINANCE:BTCUSDT", "2025-01-01", "2025-01-04", catalog=fixture_catalog)
    assert intents and {i.action for i in intents} <= {"open", "close"}
    assert [i.ts for i in intents] == sorted(i.ts for i in intents)
    assert all(i.timeframe == 3600 for i in intents)
