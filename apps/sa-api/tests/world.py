"""A small world for the API tests: throwaway identities, a public and a private provider, followers, anchors, a vote.

Everything is signed with throwaway keys and served by an in-process relay; candles are synthetic. Nothing here
touches a network or a real wallet.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sa_core.delegation import sign_delegation
from sa_core.governance import build_anchor_attestation, build_proposal_event, build_vote_event
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import (
    Delegation,
    NostrEvent,
    PowerAllocation,
    PrivateRule,
    Proposal,
    Reveal,
    Signal,
    Tier,
    Vote,
)
from sa_core.power import allocation_event, ruleset_event, sign_allocation, sign_ruleset
from sa_core.signals import build_commitment_event, build_signal_event
from sa_core.trackrecord import Candle
from sa_core.tsa import Endpoint, TrustStore, TsaStamper
from sa_engine.network import fixture
from sa_engine.report import BacktestReport, ReportFill

MINUTE = 60_000
FAR = 4_000_000_000
INSTRUMENT = "BINANCE:BTCUSDT"


@dataclass
class Actor:
    sa: SAKey = field(default_factory=SAKey.generate)
    device: DeviceKey = field(default_factory=DeviceKey.generate)

    def __post_init__(self) -> None:
        self.delegation: Delegation = sign_delegation(self.sa, self.device.pubkey_hex, FAR)

    @property
    def pubkey(self) -> str:
        return self.device.pubkey_hex

    @property
    def address(self) -> str:
        return self.sa.address


def candles_around(t0_ms: int, minutes: int = 60) -> list[Candle]:
    """One candle per minute from t0, price 100 rising 1 per minute (so every long wins)."""
    out = []
    for i in range(minutes):
        p = Decimal(100 + i)
        out.append(
            Candle(open_time_ms=t0_ms + i * MINUTE, open=p, high=p + 1, low=p - 1, close=p + 1)
        )
    return out


class StubCandles:
    """Duck-typed ``CandleSource``: the same synthetic series for every provider."""

    def __init__(self, series: list[Candle]) -> None:
        self.series = series
        self.calls = 0

    def load(
        self, provider: str, instruments: list[str], first_ms: int, last_ms: int
    ) -> dict[str, list[Candle]]:
        self.calls += 1
        return dict.fromkeys(instruments, self.series)


def signal(
    strategy: str, seq: int, prev: str | None, at_ms: int, *, side: str, action: str, size: float
) -> Signal:
    return Signal.model_validate(
        {
            "strategy_id": strategy, "seq": seq, "prev": prev, "instrument": INSTRUMENT, "side": side,
            "action": action, "size_fraction": size, "order_type": "market", "timeframe": 3600, "created_ms": at_ms,
        }
    )  # fmt: skip


@dataclass
class World:
    """Events for the fake relay plus the files the API reads."""

    root: Path
    now_ms: int
    pub: Actor = field(default_factory=Actor)
    priv: Actor = field(default_factory=Actor)
    whale: Actor = field(default_factory=Actor)
    mid: Actor = field(default_factory=Actor)
    events: list[NostrEvent] = field(default_factory=list)
    reveals: list[Reveal] = field(default_factory=list)
    private_ids: list[str] = field(default_factory=list)
    public_ids: list[str] = field(default_factory=list)
    proposal_id: str = ""

    @property
    def balances(self) -> Path:
        return self.root / "balances.json"

    def build(self, stamper: TsaStamper, *, reveal_private: bool = True, gap: bool = False) -> None:
        t0 = (
            self.now_ms - 6 * MINUTE
        )  # signals a few minutes ago; anchored "now" (within the 60 s default? see below)
        fixture.set_balance(self.balances, self.whale.address, 1_000_000)
        fixture.set_balance(self.balances, self.mid.address, 20_000)
        self.events.append(
            ruleset_event(
                self.pub.device,
                sign_ruleset(
                    self.pub.sa, self.pub.pubkey, 3600,
                    tiers=[Tier(min_power=100_000, delay_fraction=0.0), Tier(min_power=10_000, delay_fraction=0.5), Tier(min_power=0, delay_fraction=2.0)],
                ),
                self.pub.delegation,
            )
        )  # fmt: skip
        self.events.append(
            ruleset_event(
                self.priv.device,
                sign_ruleset(
                    self.priv.sa, self.priv.pubkey, 3600, private=PrivateRule(min_power=10_000)
                ),
                self.priv.delegation,
            )
        )
        # public chain: buy, sell, buy, sell (closed trades) with a seq gap optional
        prev: str | None = None
        pub_events = []
        plan = [
            ("buy", "open", 0.5),
            ("sell", "close", 1.0),
            ("buy", "open", 0.5),
            ("sell", "close", 1.0),
        ]
        for i, (side, action, size) in enumerate(plan, start=1):
            sig = signal("atlas-ema", i, prev, t0 + i * MINUTE, side=side, action=action, size=size)
            ev = build_signal_event(
                self.pub.device, sig, self.pub.delegation, created_at=sig.created_ms // 1000
            )
            prev = ev.id
            if gap and i == 3:
                continue  # drop one from the relay: the chain shows a gap
            pub_events.append(ev)
            self.public_ids.append(ev.id)
        # private chain: two commitments (one revealed unless reveal_private is False)
        prev = None
        priv_events = []
        for i, (side, action, size) in enumerate(
            [("buy", "open", 0.4), ("sell", "close", 1.0)], start=1
        ):
            sig = signal("fox-ema", i, prev, t0 + i * MINUTE, side=side, action=action, size=size)
            ev = build_commitment_event(
                self.priv.device, sig, self.priv.delegation, created_at=sig.created_ms // 1000
            )
            prev = ev.id
            priv_events.append(ev)
            self.private_ids.append(ev.id)
            if reveal_private:
                self.reveals.append(Reveal(commitment_id=ev.id, plaintext=sig.model_dump_json()))
        chain = [*pub_events, *priv_events]
        self.events.extend(chain)
        for proof, ev in zip(
            stamper.stamp_batch([bytes.fromhex(e.id) for e in chain]), chain, strict=True
        ):
            owner = self.pub if ev in pub_events else self.priv
            self.events.append(build_anchor_attestation(owner.device, proof))
        # allocations: whale 60% / 30% / 10% gov, mid 70% to the public provider and 30% governance
        for actor, shares, gov in (
            (self.whale, {self.pub.pubkey: 6000, self.priv.pubkey: 3000}, 1000),
            (self.mid, {self.pub.pubkey: 7000}, 3000),
        ):
            alloc: PowerAllocation = sign_allocation(actor.sa, 1, gov, shares)
            self.events.append(allocation_event(actor.device, alloc, actor.delegation))

    def add_vote_round(self) -> None:
        now = int(time.time())
        prop = Proposal(
            title="Ship the demo?",
            options=("Yes", "No"),
            deadline=now + 3600,
            snapshot_time=now,
            relays=("ws://x",),
        )
        pev = build_proposal_event(self.whale.device, prop, self.whale.delegation, now)
        self.proposal_id = pev.id
        self.events.append(pev)
        self.events.append(
            build_vote_event(
                self.whale.device,
                Vote(proposal=pev.id, option=0, nonce=1),
                self.whale.delegation,
                now,
            )
        )
        self.events.append(
            build_vote_event(
                self.mid.device, Vote(proposal=pev.id, option=1, nonce=1), self.mid.delegation, now
            )
        )

    def write_follower_home(
        self, name: str, actor: Actor, allocation: PowerAllocation | None, received: dict[str, int]
    ) -> Path:
        home = self.root / name
        (home / "follower").mkdir(parents=True)
        (home / "identity.json").write_text(
            json.dumps(
                {"v": 1, "mode": "demo", "address": actor.address, "device_pubkey": actor.pubkey}
            )
        )
        if allocation is not None:
            (home / "allocation.json").write_text(allocation.model_dump_json())
        db = sqlite3.connect(home / "follower" / "ledger.sqlite")
        db.execute(
            "CREATE TABLE signals (event_id TEXT, provider TEXT, ref TEXT, received_ms INTEGER)"
        )
        for ref, ms in received.items():
            db.execute("INSERT INTO signals VALUES (?, ?, ?, ?)", (ref, self.pub.pubkey, ref, ms))
        db.commit()
        db.close()
        return home


def write_ledger(home: Path, rows: list[dict[str, object]]) -> None:
    """A follower's real ledger (`sa_engine`'s schema) under ``home``: one row per dict, ``event_id`` and ``provider``
    required; ``executed`` (bool) marks an accepted row whose order was placed, ``exec_reason`` the note on it, ``queued`` (bool) a not-yet-placed order waiting for the exchange."""
    from sa_engine.network.executor import Execution
    from sa_engine.network.ledger import Ledger

    ledger = Ledger(home / "follower" / "ledger.sqlite")
    for r in rows:
        ledger.record(
            event_id=str(r["event_id"]), provider=str(r["provider"]), source="relay",
            received_ms=int(str(r["received_ms"])), status=r.get("status", "accepted"),  # type: ignore[arg-type]
            reason=str(r.get("reason", "")), seq=r.get("seq"),  # type: ignore[arg-type]
            ref=str(r["ref"]) if "ref" in r else None, created_ms=r.get("created_ms"),  # type: ignore[arg-type]
            signal_json=r.get("signal_json"),  # type: ignore[arg-type]
        )  # fmt: skip
        if "executed" in r:
            ledger.set_execution(
                Execution(
                    str(r["event_id"]),
                    bool(r["executed"]),
                    reason=str(r.get("exec_reason", "")),
                    queued=bool(r.get("queued", False)),
                )
            )
    ledger.close()


def farm_stamper(config: Path) -> TsaStamper:
    cfg = json.loads(config.read_text())
    return TsaStamper(
        tuple(Endpoint(**e) for e in cfg["endpoints"]),
        TrustStore.load(Path(cfg["trust_dir"])),
        k=cfg["k"],
        min_gap_s=cfg["min_gap_s"],
    )


def make_report(points: int = 1000, fills: list[ReportFill] | None = None) -> BacktestReport:
    """A BacktestReport as `sa backtest --json -o` writes it."""
    from datetime import UTC, datetime

    if fills is None:  # two long round trips, the second scaled out in two sells
        fills = [
            ReportFill(t=1_788_000_300_000, side="buy", price=100.5, qty=2.0, fee=0.2),
            ReportFill(t=1_788_003_900_000, side="sell", price=104.25, qty=2.0, fee=0.2),
            ReportFill(t=1_788_007_500_000, side="buy", price=103.0, qty=1.5, fee=0.15),
            ReportFill(t=1_788_011_100_000, side="sell", price=105.0, qty=1.0, fee=0.1),
            ReportFill(t=1_788_014_700_000, side="sell", price=106.0, qty=0.5, fee=0.05),
        ]
    return BacktestReport(
        strategy="ema", instrument="BINANCE:BTCUSDT", timeframe="1h",
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 8, tzinfo=UTC),
        starting_capital=10_000, final_equity=10_500, pnl=500, pnl_pct=5.0, trade_count=4, wins=3, win_rate=0.75,
        max_drawdown=300, max_drawdown_pct=2.9, fees=10, open_qty_at_end=0, bars_processed=10_080, intents=8,
        rejections=[], trades=[], fills=fills, equity_curve=[(1_788_000_000_000 + i * 60_000, 10_000 + i) for i in range(points)],
    )  # fmt: skip
