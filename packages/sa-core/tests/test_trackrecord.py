"""Track record: SEP-3.8 to SEP-3.11 (hand-computed fixture, determinism), SEP-3.16, SEP-3.28, SEP-3.12."""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core_helpers import FIXTURE_CANDLES, MINUTE, T0_MS, Provider, anchor_all, candle
from sa_core.bundle import bundle_to_json
from sa_core.trackrecord import Candle, TrackRecordError, compute

INST = "BINANCE:BTCUSDT"
CANDLES = {INST: FIXTURE_CANDLES}


def q12(x: Fraction) -> str:
    d = (Decimal(x.numerator) / Decimal(x.denominator)).quantize(
        Decimal("1e-12"), rounding=ROUND_HALF_EVEN
    )
    return format(d, "f")


def fixture_provider() -> Provider:
    """Five signals, fills at the candle opens named below (anchored at creation time)."""
    p = Provider()
    p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)  # c0 open 100
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)  # c2 open 110
    p.publish(side="buy", action="open", size=0.4, at_ms=T0_MS + 2 * MINUTE)  # c2 open 110
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 4 * MINUTE)  # c4 open 95
    p.publish(
        side="sell", action="open", size=0.4, at_ms=T0_MS + 4 * MINUTE
    )  # c4 open 95, stays open
    return p


def run(p: Provider, anchored: dict[str, int | None] | None = None) -> dict[str, Any]:
    b = p.bundle()
    used = anchored or anchor_all(list(b.events), delay_s=0)
    report: dict[str, Any] = json.loads(compute(b, CANDLES, anchored=used).to_json())
    return report


# Hand calculation (fractions of capital; units = notional / price):
#   trade 1: +0.5 @100 -> close @110:           (0.5/100) * 10      = +1/20
#   trade 2: +0.4 @110 -> close @95:            (0.4/110) * (-15)   = -3/55
#   open short -0.4 @95, marked at last close 120: -(0.4/95) * 25    = -2/19
#   realised = 1/20 - 3/55 = -1/220 ; unrealised = -2/19 ; total = -459/4180
#   equity at candle closes: 1, 1.05 (peak), 1.05-1/55, 1.05-3/55, 1-1/220-2/475 (c4), 1-1/220-2/19 (c5,c6)
#   max drawdown = (21/20 - 3721/4180) / (21/20)
R, U = Fraction(-1, 220), Fraction(-2, 19)
PEAK, LOW = Fraction(21, 20), 1 + R + U
DD = (PEAK - LOW) / PEAK


def test_sep_3_8_pnl_matches_the_hand_computed_value() -> None:
    r = run(fixture_provider())
    assert r["pnl_realized"] == q12(R) == "-0.004545454545"
    assert r["pnl_unrealized"] == q12(U) == "-0.105263157895"
    assert r["pnl"] == q12(R + U) == "-0.109808612440"


def test_sep_3_9_win_rate_matches() -> None:
    r = run(fixture_provider())
    assert (r["trades"], r["wins"], r["losses"], r["misses"], r["open_positions"]) == (
        2,
        1,
        1,
        0,
        1,
    )
    assert r["win_rate"] == "0.500000000000"


def test_sep_3_10_max_drawdown_matches() -> None:
    r = run(fixture_provider())
    assert Fraction(668, 4389) == DD  # (21/20 - 3721/4180) / (21/20), by hand
    assert r["max_drawdown"] == q12(DD) == "0.152198678514"


def test_sep_3_11_two_runs_are_byte_identical_and_hash_seed_independent(tmp_path: Path) -> None:
    p = fixture_provider()
    b = p.bundle()
    anchored = anchor_all(list(b.events), delay_s=0)
    first = compute(b, CANDLES, anchored=anchored).to_json()
    assert compute(b, CANDLES, anchored=anchored).to_json() == first
    (tmp_path / "bundle.json").write_text(bundle_to_json(b))
    (tmp_path / "anchored.json").write_text(json.dumps(anchored))
    (tmp_path / "candles.json").write_text(
        json.dumps(
            [
                [c.open_time_ms, str(c.open), str(c.high), str(c.low), str(c.close), "0"]
                for c in FIXTURE_CANDLES
            ]
        )
    )
    script = (
        "import json,sys;from sa_core.bundle import bundle_from_json;from sa_core.trackrecord import Candle,compute;"
        "d=sys.argv[1];b=bundle_from_json(open(d+'/bundle.json').read());"
        "c=[Candle.from_kline(r) for r in json.load(open(d+'/candles.json'))];"
        "a=json.load(open(d+'/anchored.json'));sys.stdout.write(compute(b,{'BINANCE:BTCUSDT':c},anchored=a).to_json())"
    )
    outs = []
    for seed in ("1", "12345"):
        env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"}
        res = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        outs.append(res.stdout)
    assert outs[0] == outs[1] == first


def test_report_shape_is_stable() -> None:
    r = run(fixture_provider())
    assert list(r) == sorted(r)  # canonical: keys sorted
    assert r["signals"] == {
        "counted": 5,
        "delegation_expired": 0,
        "excluded": {},
        "late_anchor": 0,
        "penalized": 0,
        "penalized_trades": 0,
        "total": 5,
        "unanchored": 0,
    }
    assert r["max_gross_exposure"] == "0.500000000000"
    assert r["pnl_per_exposure"] == q12((R + U) / Fraction(1, 2))
    assert r["completeness"]["complete"] is True
    assert r["anchor_coverage"] == {"anchored": 5, "total": 5, "ratio": "1.000000000000"}
    assert r["assumptions"]["fees"] == "none"


# ---------------------------------------------------------------- SEP-3.16


def test_sep_3_16_a_signal_anchored_after_the_move_it_claims_is_a_miss_not_hidden() -> None:
    p = fixture_provider()
    b = p.bundle()
    anchored = anchor_all(list(b.events), delay_s=0)
    base = json.loads(compute(b, CANDLES, anchored=anchored).to_json())
    late = dict(anchored)
    late[p.events[1].id] = (
        p.events[1].created_at + 61
    )  # beyond the 60 s tolerance: the closer of trade 1
    r = json.loads(compute(b, CANDLES, anchored=late).to_json())
    assert r["signals"]["excluded"] == {}  # nothing is dropped
    assert (r["signals"]["late_anchor"], r["signals"]["penalized"], r["signals"]["counted"]) == (
        1,
        1,
        5,
    )
    assert r["anchor_coverage"]["anchored"] == 4  # coverage counts on-time anchors only
    # trade 1 (+1/20) is a miss: no gain, counted as a loss; trade 2 and the open short are unchanged
    assert r["pnl"] == q12(Fraction(-3, 55) + U) and Decimal(r["pnl"]) < Decimal(base["pnl"])
    assert r["pnl_forfeited"] == q12(Fraction(1, 20))
    assert (r["trades"], r["wins"], r["losses"], r["win_rate"]) == (2, 0, 2, "0.000000000000")
    ok = dict(anchored)
    ok[p.events[1].id] = p.events[1].created_at + 60  # exactly at the tolerance: still on time
    on_time = json.loads(compute(b, CANDLES, anchored=ok).to_json())
    assert on_time["signals"]["penalized"] == 0 and on_time["pnl"] == base["pnl"]


@given(delay=st.integers(61, 10**7))
def test_sep_3_16_property_backdated_signals_never_earn_anything(delay: int) -> None:
    p = Provider()
    ev = p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)
    r = json.loads(compute(p.bundle(), CANDLES, anchored={ev.id: ev.created_at + delay}).to_json())
    assert r["signals"]["late_anchor"] == 1 and r["signals"]["excluded"] == {}
    assert (
        r["pnl"] == "0.000000000000"
    )  # the open long gains on the rising fixture, all of it forfeited
    assert Decimal(r["pnl_forfeited"]) > 0 and r["trades"] == 0


def test_unanchored_expired_and_unfillable_items() -> None:
    p = fixture_provider()
    b = p.bundle()
    none = json.loads(compute(b, CANDLES, anchored={}).to_json())
    assert none["signals"]["excluded"] == {}
    assert none["signals"]["unanchored"] == 5 and none["anchor_coverage"]["anchored"] == 0
    # every signal is a miss: trade 1's gain is forfeited, the two losses stay, no round trip wins
    assert none["pnl"] == q12(Fraction(-3, 55) + U) and none["win_rate"] == "0.000000000000"
    expiring = Provider(expires_at=T0_MS // 1000 + 1)
    ev = expiring.publish(at_ms=T0_MS)
    r = json.loads(
        compute(expiring.bundle(), CANDLES, anchored={ev.id: T0_MS // 1000 + 5}).to_json()
    )
    assert r["signals"]["delegation_expired"] == 1 and r["signals"]["penalized"] == 1
    assert r["signals"]["excluded"] == {} and r["pnl"] == "0.000000000000"
    other = Provider()
    ev2 = other.publish(instrument="BINANCE:ETHUSDT", at_ms=T0_MS)
    r2 = json.loads(compute(other.bundle(), CANDLES, anchored=anchor_all([ev2], 0)).to_json())
    assert r2["signals"]["excluded"] == {"no_candles": 1}
    after = Provider()
    ev3 = after.publish(at_ms=T0_MS + 99 * MINUTE)
    r3 = json.loads(compute(after.bundle(), CANDLES, anchored=anchor_all([ev3], 0)).to_json())
    assert r3["signals"]["excluded"] == {"unfilled": 1}


def test_anchor_time_inside_the_tolerance_does_not_move_the_fill() -> None:
    """The fill never depends on the anchor, so withholding or delaying one cannot change any price."""
    p = Provider()
    ev = p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    b = p.bundle()
    on_time = anchor_all(list(b.events), 0)
    slow = {**on_time, ev.id: ev.created_at + 3}
    assert (
        compute(b, CANDLES, anchored=slow).to_json()
        == compute(b, CANDLES, anchored=on_time).to_json()
    )
    p2 = Provider()
    e2 = p2.publish(side="buy", action="open", size=0.5, at_ms=T0_MS + 2 * MINUTE)
    p2.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 4 * MINUTE)
    b2 = p2.bundle()
    a2 = {**anchor_all(list(b2.events), 0), e2.id: e2.created_at + 3}
    r = json.loads(compute(b2, CANDLES, anchored=a2).to_json())
    assert r["pnl"] == q12(Fraction(1, 2) * Fraction(95 - 110, 110))  # c2 open 110, c4 open 95


# ---------------------------------------------------------------- SEP-3.28 / SEP-3.12


def test_sep_3_28_a_revealed_signal_is_on_time_only_if_its_commitment_predates_the_outcome() -> (
    None
):
    p = Provider()
    c1 = p.commit(side="buy", action="open", size=0.5, at_ms=T0_MS)
    c2 = p.commit(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    b = p.bundle()
    ok = json.loads(compute(b, CANDLES, anchored=anchor_all([c1, c2], 0)).to_json())
    assert ok["signals"]["counted"] == 2 and ok["pnl"] == q12(Fraction(1, 20)) and ok["misses"] == 0
    # the commitment for the opening signal is anchored two minutes after the signal claims
    late = json.loads(
        compute(b, CANDLES, anchored={c1.id: c1.created_at + 120, c2.id: c2.created_at}).to_json()
    )
    assert late["signals"]["excluded"] == {} and late["signals"]["late_anchor"] == 1
    # the opener is a miss: the round trip it starts is a loss in win_rate and its gain is forfeited
    assert (late["trades"], late["wins"], late["losses"]) == (1, 0, 1)
    assert late["pnl"] == "0.000000000000" and late["pnl_forfeited"] == q12(Fraction(1, 20))
    # a plaintext that does not match its commitment is not accepted
    forged = b.model_copy(
        update={
            "reveals": (
                b.reveals[0].model_copy(update={"plaintext": b.reveals[1].plaintext}),
                b.reveals[1],
            )
        }
    )
    fr = json.loads(compute(forged, CANDLES, anchored=anchor_all([c1, c2], 0)).to_json())
    assert fr["completeness"]["complete"] is False and fr["misses"] == 1


@given(n=st.integers(1, 6), revealed=st.lists(st.booleans(), min_size=6, max_size=6))
def test_sep_3_12_an_unrevealed_commitment_is_always_counted_as_a_miss(
    n: int, revealed: list[bool]
) -> None:
    p = Provider()
    evs = []
    for i in range(n):
        evs.append(
            p.commit(
                reveal=revealed[i],
                side="buy" if i % 2 == 0 else "sell",
                action="open" if i % 2 == 0 else "close",
                size=0.5 if i % 2 == 0 else 1.0,
                at_ms=T0_MS + i * MINUTE,
            )
        )
    r = json.loads(compute(p.bundle(), CANDLES, anchored=anchor_all(evs, 0)).to_json())
    missed = [e.id for e, rv in zip(evs, revealed, strict=False) if not rv]
    assert r["misses"] == len(missed)
    assert r["unrevealed_commitments"] == sorted(missed)
    denom = r["trades"] + r["misses"]
    if denom:
        assert Decimal(r["win_rate"]) == (Decimal(r["wins"]) / Decimal(denom)).quantize(
            Decimal("1e-12"), rounding=ROUND_HALF_EVEN
        )
    else:
        assert r["win_rate"] is None


def test_sep_3_12_long_and_short_committed_only_one_revealed() -> None:
    p = Provider()
    long_ = p.commit(side="buy", action="open", size=0.5, at_ms=T0_MS, reveal=True)
    close = p.commit(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE, reveal=True)
    short = p.commit(side="sell", action="open", size=0.5, at_ms=T0_MS + 3 * MINUTE, reveal=False)
    r = json.loads(
        compute(p.bundle(), CANDLES, anchored=anchor_all([long_, close, short], 0)).to_json()
    )
    assert r["misses"] == 1 and r["unrevealed_commitments"] == [short.id]
    assert r["wins"] == 1 and r["win_rate"] == "0.500000000000"  # the miss is in the denominator
    assert r["signals"]["excluded"] == {"unrevealed": 1}


# ---------------------------------------------------------------- accounting edge cases


def test_netting_flip_adjust_and_invalid_close() -> None:
    p = Provider()
    p.publish(side="buy", action="open", size=0.2, at_ms=T0_MS)  # long 0.2 @100
    p.publish(
        side="sell", action="open", size=0.5, at_ms=T0_MS + 2 * MINUTE
    )  # sells through: flat then short @110
    p.publish(
        side="sell", action="close", size=1.0, at_ms=T0_MS + 3 * MINUTE
    )  # wrong side (already short): ignored
    p.publish(side="buy", action="adjust", size=0.2, at_ms=T0_MS + 3 * MINUTE)  # buy back part
    p.publish(
        side="buy", action="close", size=0.5, at_ms=T0_MS + 4 * MINUTE
    )  # close half the rest @95
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 4 * MINUTE)  # wrong side again
    r = run(p)
    assert r["signals"]["excluded"] == {"invalid_close": 2}
    assert r["trades"] >= 1 and r["open_positions"] == 1
    # first trade: +0.2 @100 closed by selling 0.2 notional-worth @110 -> (0.2/100)*10 = +0.02
    assert r["wins"] >= 1


def test_stop_orders_trigger_on_the_candle_that_reaches_the_stop_or_stay_unfilled() -> None:
    p = Provider()
    # buy stop 105: c0 high 101 does not reach it, c1 high 111 does; c1 opens at 100 < 105 -> fill 105
    p.publish(
        side="buy",
        action="open",
        size=0.5,
        at_ms=T0_MS,
        order_type="stop",
        stop_price=105.0,
        timeframe=300,
    )
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 5 * MINUTE)
    # never reached within the timeframe -> unfilled
    p.publish(
        side="buy",
        action="open",
        size=0.5,
        at_ms=T0_MS + 5 * MINUTE,
        order_type="stop",
        stop_price=500.0,
        timeframe=60,
    )
    r = run(p)
    assert r["pnl_realized"] == q12(Fraction(1, 210) * (96 - 105))
    assert r["signals"]["excluded"]["unfilled"] == 1


def test_sell_stop_gapping_through_fills_at_the_open() -> None:
    p = Provider()
    p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)  # market, c0 open 100
    # sell stop 97 placed at c5 (open 96, low 95): already through the stop -> fills at the open 96
    p.publish(
        side="sell",
        action="close",
        size=1.0,
        at_ms=T0_MS + 5 * MINUTE,
        order_type="stop",
        stop_price=97.0,
        timeframe=60,
    )
    r = run(p)
    assert r["pnl_realized"] == q12(Fraction(1, 200) * (96 - 100))


def test_limit_orders_fill_at_the_limit_or_not_at_all() -> None:
    p = Provider()
    p.publish(
        side="buy",
        action="open",
        size=0.5,
        at_ms=T0_MS,
        order_type="limit",
        limit_price=99.5,
        timeframe=300,
    )
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 5 * MINUTE)
    p.publish(
        side="buy",
        action="open",
        size=0.5,
        at_ms=T0_MS + 5 * MINUTE,
        order_type="limit",
        limit_price=50.0,
        timeframe=60,
    )
    p.publish(
        side="sell",
        action="open",
        size=0.5,
        at_ms=T0_MS + 5 * MINUTE,
        order_type="limit",
        limit_price=300.0,
        timeframe=60,
    )
    p.publish(
        side="sell",
        action="open",
        size=0.1,
        at_ms=T0_MS,
        order_type="limit",
        limit_price=100.5,
        timeframe=60,
    )
    r = run(p)
    # buy limit 99.5: first candle touching it is c0 (low 99); fill at min(open 100, limit) = 99.5
    # sell close at c5 open 96 -> (0.5/99.5) * (96 - 99.5)
    assert r["pnl_realized"] == q12(Fraction(1, 2) / Fraction(199, 2) * (96 - Fraction(199, 2)))
    assert (
        r["signals"]["excluded"]["unfilled"] == 2
    )  # 50.0 buy and 300.0 sell never trade within the window


def test_candles_must_be_sorted_and_instruments_are_independent() -> None:
    p = Provider()
    ev = p.publish(at_ms=T0_MS)
    with pytest.raises(TrackRecordError, match="strictly ascending"):
        compute(
            p.bundle(),
            {INST: [FIXTURE_CANDLES[1], FIXTURE_CANDLES[0]]},
            anchored=anchor_all([ev], 0),
        )
    with pytest.raises(TrackRecordError, match="tolerance_s"):
        compute(p.bundle(), CANDLES, tolerance_s=-1)


def test_candle_from_binance_kline_row() -> None:
    c = Candle.from_kline(
        [1700000040000, "100.5", "101", "99", "100", "12.5", 0, "0", 1, "0", "0", "0"]
    )
    assert (
        c.open == Decimal("100.5")
        and c.volume == Decimal("12.5")
        and c.open_time_ms == 1700000040000
    )
    assert candle(1, "1", "2", "0.5", "1").open_time_ms == T0_MS + MINUTE


def test_empty_bundle_report() -> None:
    p = Provider()
    r = json.loads(compute(p.bundle(), CANDLES).to_json())
    assert (
        r["pnl"] == "0.000000000000"
        and r["win_rate"] is None
        and r["anchor_coverage"]["ratio"] == "n/a"
    )
    assert r["max_drawdown"] == "0.000000000000" and r["trades"] == 0


def test_proofs_in_the_bundle_drive_anchoring_end_to_end() -> None:
    from sa_core.anchors import BscCommitAnchor, SimChain

    clock = [float(T0_MS // 1000)]
    chain = SimChain(clock=lambda: clock[0])
    bsc = BscCommitAnchor(chain, chain, min_depth=3)
    p = Provider()
    e1 = p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)
    e2 = p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    proofs = bsc.stamp_batch([bytes.fromhex(e1.id), bytes.fromhex(e2.id)])
    chain.mine(T0_MS // 1000 + 2)
    chain.mine_many(4, start=T0_MS // 1000 + 3)
    b = p.bundle(tuple(proofs))
    r = json.loads(compute(b, CANDLES, verifiers={"bsc-commit": bsc}).to_json())
    assert r["anchor_coverage"]["anchored"] == 2
    # e2 was "stamped" with the same block (a bound of ~T0+5 s) long before... it claims T0+120 s: fine
    assert r["signals"]["counted"] == 2
    # without a verifier the same bundle has zero anchor coverage
    assert json.loads(compute(b, CANDLES).to_json())["anchor_coverage"]["anchored"] == 0


@given(delay=st.integers(0, 400), tolerance=st.integers(0, 200))
def test_sep_3_28_property_reveal_accepted_iff_commitment_predates_the_outcome(
    delay: int, tolerance: int
) -> None:
    """A committed signal is on time iff its commitment's anchored time <= signal time + tolerance."""
    p = Provider()
    c = p.commit(side="buy", action="open", size=0.5, at_ms=T0_MS)
    r = json.loads(
        compute(
            p.bundle(), CANDLES, anchored={c.id: c.created_at + delay}, tolerance_s=tolerance
        ).to_json()
    )
    accepted = delay <= tolerance
    assert r["signals"]["counted"] == 1 and r["signals"]["excluded"] == {}
    assert r["signals"]["late_anchor"] == (0 if accepted else 1)
    assert (Decimal(r["pnl"]) > 0) is accepted  # a late commitment's open position earns nothing
    assert r["misses"] == 0


def test_series_lists_the_round_trips_and_an_equity_curve_without_changing_the_report() -> None:
    p = fixture_provider()
    b = p.bundle()
    anchored = anchor_all(list(b.events), delay_s=0)
    rec = compute(b, CANDLES, anchored=anchored)
    trades = rec.series.trades
    assert [(t.side, t.entry, t.exit) for t in trades] == [
        ("buy", Decimal(100), Decimal(110)),
        ("buy", Decimal(110), Decimal(95)),
    ]
    assert [t.open_ms for t in trades] == [T0_MS, T0_MS + 2 * MINUTE]
    assert [t.close_ms for t in trades] == [T0_MS + 2 * MINUTE, T0_MS + 4 * MINUTE]
    assert trades[0].pnl_pct == Decimal(10)
    assert round(trades[1].pnl_pct, 4) == round(Decimal(-15) / Decimal(110) * 100, 4)  # -13.6364
    times = [t for t, _ in rec.series.equity]
    assert times == sorted(times)
    assert rec.series.equity[0][1] == 1
    expected = Decimal(1) + Decimal((R + U).numerator) / Decimal((R + U).denominator)
    assert abs(rec.series.equity[-1][1] - expected) < Decimal("1e-9")
    # the series is not part of the deterministic report bytes
    assert "series" not in rec.to_json()
    assert max(1 - e for _, e in rec.series.equity) >= 0


def test_series_short_trade_with_an_add_and_a_partial_close() -> None:
    p = Provider()
    p.publish(side="sell", action="open", size=0.2, at_ms=T0_MS)  # short 0.002 units @100
    p.publish(side="sell", action="open", size=0.2, at_ms=T0_MS + MINUTE)  # add 0.002 @100
    p.publish(side="buy", action="close", size=0.5, at_ms=T0_MS + 2 * MINUTE)  # buy back half @110
    p.publish(side="buy", action="close", size=1.0, at_ms=T0_MS + 3 * MINUTE)  # the rest @105
    b = p.bundle()
    rec = compute(b, CANDLES, anchored=anchor_all(list(b.events), delay_s=0))
    assert len(rec.series.trades) == 1
    t = rec.series.trades[0]
    assert (t.side, t.entry, t.exit) == ("sell", Decimal(100), Decimal("107.5"))
    assert (t.open_ms, t.close_ms) == (T0_MS, T0_MS + 3 * MINUTE)
    assert t.pnl_pct == Decimal("-7.5")  # a short loses when the price rises


def test_series_flip_ends_one_trade_and_starts_the_next() -> None:
    p = Provider()
    p.publish(side="buy", action="open", size=0.2, at_ms=T0_MS)  # long 0.002 @100
    p.publish(
        side="sell", action="open", size=0.5, at_ms=T0_MS + 2 * MINUTE
    )  # through flat to short @110
    p.publish(side="buy", action="close", size=1.0, at_ms=T0_MS + 3 * MINUTE)  # short closed @105
    b = p.bundle()
    rec = compute(b, CANDLES, anchored=anchor_all(list(b.events), delay_s=0))
    first, second = rec.series.trades
    assert (first.side, first.entry, first.exit) == ("buy", Decimal(100), Decimal(110))
    assert (second.side, second.entry, second.exit) == ("sell", Decimal(110), Decimal(105))
    assert second.open_ms == first.close_ms


# ---------------------------------------------------------------- H3: late or missing anchors never help


def _random_market(prices: list[int]) -> dict[str, list[Candle]]:
    return {
        INST: [
            candle(i, str(o), str(max(o, c) + 1), str(min(o, c) - 1), str(c))
            for i, (o, c) in enumerate(itertools.pairwise(prices))
        ]
    }


_ACTION = st.tuples(
    st.sampled_from(["buy", "sell"]),
    st.sampled_from(["open", "close", "adjust"]),
    st.sampled_from([0.2, 0.5, 1.0]),
)


@given(
    prices=st.lists(st.integers(60, 140), min_size=10, max_size=10),
    plan=st.lists(_ACTION, min_size=1, max_size=8),
    states=st.lists(st.sampled_from(["ok", "late", "none"]), min_size=8, max_size=8),
    more=st.lists(st.sampled_from(["late", "none"]), min_size=8, max_size=8),
    pick=st.lists(st.booleans(), min_size=8, max_size=8),
)
def test_h3_withholding_or_delaying_any_subset_of_anchors_never_raises_pnl_or_win_rate(
    prices: list[int],
    plan: list[tuple[str, str, float]],
    states: list[str],
    more: list[str],
    pick: list[bool],
) -> None:
    market = _random_market(prices)
    p = Provider()
    evs = [
        p.publish(side=side, action=action, size=size, at_ms=T0_MS + i * MINUTE)
        for i, (side, action, size) in enumerate(plan)
    ]
    b = p.bundle()

    def score(anchor: dict[str, int | None]) -> tuple[Decimal, Decimal | None, int]:
        r = json.loads(compute(b, market, anchored=anchor).to_json())
        wr = None if r["win_rate"] is None else Decimal(r["win_rate"])
        return Decimal(r["pnl"]), wr, r["trades"]

    honest: dict[str, int | None] = {e.id: e.created_at for e in evs}
    base = score(honest)

    def degrade(
        old: dict[str, int | None], how: list[str], only: list[bool]
    ) -> dict[str, int | None]:
        out = dict(old)
        for e, h, chosen in zip(evs, how, only, strict=False):
            if chosen and h != "ok":
                out[e.id] = None if h == "none" else e.created_at + 61
        return out

    first = degrade(honest, states, [True] * len(evs))
    second = degrade(first, more, pick)  # a superset of the damage
    s1, s2 = score(first), score(second)
    for worse, better in ((s1, base), (s2, s1)):
        assert worse[0] <= better[0], "withholding/delaying anchors raised pnl"
        assert worse[2] == better[2]  # the same round trips are counted: nothing is dropped
        if worse[1] is not None and better[1] is not None:
            assert worse[1] <= better[1], "withholding/delaying anchors raised win_rate"


def test_h3_a_hidden_loser_cannot_be_cherry_picked_out_of_the_record() -> None:
    """The review's attack: withhold the anchor of every losing trade so only winners score."""
    p = Provider()
    p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)  # c0 100 -> c2 110: wins
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    lose_open = p.publish(
        side="buy", action="open", size=0.4, at_ms=T0_MS + 2 * MINUTE
    )  # 110 -> 95: loses
    lose_close = p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 4 * MINUTE)
    b = p.bundle()
    honest = anchor_all(list(b.events), 0)
    hidden = {**honest, lose_open.id: None, lose_close.id: None}
    h = json.loads(compute(b, CANDLES, anchored=honest).to_json())
    c = json.loads(compute(b, CANDLES, anchored=hidden).to_json())
    assert (
        Decimal(c["pnl"]) <= Decimal(h["pnl"]) and c["pnl"] == h["pnl"]
    )  # the loss still counts in full
    assert (c["trades"], c["wins"], c["losses"]) == (2, 1, 1)
    assert c["win_rate"] == "0.500000000000"
    assert c["signals"]["unanchored"] == 2 and c["signals"]["excluded"] == {}


def test_h3_the_report_counts_late_unanchored_and_on_time_coverage() -> None:
    p = Provider()
    a = p.publish(side="buy", action="open", size=0.2, at_ms=T0_MS)
    b_ = p.publish(side="buy", action="adjust", size=0.2, at_ms=T0_MS + MINUTE)
    c = p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    anchored: dict[str, int | None] = {a.id: a.created_at, b_.id: b_.created_at + 500, c.id: None}
    r = json.loads(compute(p.bundle(), CANDLES, anchored=anchored).to_json())
    assert (r["signals"]["late_anchor"], r["signals"]["unanchored"], r["signals"]["penalized"]) == (
        1,
        1,
        2,
    )
    assert r["anchor_coverage"] == {"anchored": 1, "total": 3, "ratio": q12(Fraction(1, 3))}
    assert r["signals"]["penalized_trades"] == 1 and (r["trades"], r["wins"]) == (1, 0)


# ---------------------------------------------------------------- M4: exposure


def test_m4_opens_stack_only_up_to_full_capital_and_the_excess_is_rejected() -> None:
    p = Provider()
    p.publish(side="buy", action="open", size=1.0, at_ms=T0_MS)
    p.publish(side="buy", action="open", size=1.0, at_ms=T0_MS + MINUTE)  # would be 200% of capital
    p.publish(side="buy", action="adjust", size=0.5, at_ms=T0_MS + MINUTE)
    p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    r = json.loads(
        compute(p.bundle(), CANDLES, anchored=anchor_all(list(p.bundle().events), 0)).to_json()
    )
    assert r["signals"]["excluded"] == {"exposure_cap": 2}
    assert r["max_gross_exposure"] == "1.000000000000"
    assert r["pnl"] == q12(
        Fraction(1) * Fraction(110 - 100, 100)
    )  # one unit of capital, +10%, not +30%
    assert r["pnl_per_exposure"] == r["pnl"]


@given(sizes=st.lists(st.sampled_from([0.1, 0.3, 0.5, 1.0]), min_size=1, max_size=10))
def test_m4_gross_exposure_never_exceeds_one_whatever_is_published(sizes: list[float]) -> None:
    p = Provider()
    evs = [
        p.publish(side="buy", action="open", size=s, at_ms=T0_MS + i * 1000)
        for i, s in enumerate(sizes)
    ]
    r = json.loads(compute(p.bundle(), CANDLES, anchored=anchor_all(evs, 0)).to_json())
    assert Decimal(r["max_gross_exposure"]) <= 1
    if Decimal(r["max_gross_exposure"]) > 0:
        want = Decimal(r["pnl"]) / Decimal(r["max_gross_exposure"])
        assert abs(Decimal(r["pnl_per_exposure"]) - want) < Decimal("1e-11")


def test_m4_a_reducing_or_flipping_signal_is_not_blocked_by_the_cap() -> None:
    p = Provider()
    p.publish(side="buy", action="open", size=1.0, at_ms=T0_MS)
    p.publish(
        side="sell", action="open", size=0.5, at_ms=T0_MS + MINUTE
    )  # reduces the long: allowed
    r = json.loads(
        compute(p.bundle(), CANDLES, anchored=anchor_all(list(p.bundle().events), 0)).to_json()
    )
    assert r["signals"]["excluded"] == {} and r["open_positions"] == 1


# ---------------------------------------------------------------- M3: revoked device keys


def test_m3_a_revoked_device_key_speaks_for_nobody_from_effective_at() -> None:
    from sa_core.delegation import sign_revocation

    p = Provider()
    before = p.publish(side="buy", action="open", size=0.5, at_ms=T0_MS)
    after = p.publish(side="sell", action="close", size=1.0, at_ms=T0_MS + 2 * MINUTE)
    rev = sign_revocation(p.sa, p.pubkey, after.created_at)  # effective with the second signal
    b = p.bundle()
    r = json.loads(
        compute(b, CANDLES, anchored=anchor_all(list(b.events), 0), revocations=[rev]).to_json()
    )
    assert r["signals"]["total"] == 1  # only the signal before the revocation is an item
    assert any("revoked" in x for x in r["completeness"]["problems"])
    assert r["completeness"]["complete"] is False
    assert before.id != after.id
