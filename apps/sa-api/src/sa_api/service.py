"""The read model behind the dashboard API: real data in, contract shapes out.

Nothing here writes. Sources: the relay (rule sets, allocations, signals, commitments, anchor proofs, proposals), provider
bundles (reveals of private signals), ``sa_core.trackrecord`` for every number of a record, ``sa-engine`` backtest
reports, the balance oracle for token power and chain status, and optional follower ledgers for real receipt times.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from sa_api import models as m
from sa_api.anchors import tsa_verifiers
from sa_api.candles import CandleLoader, CandleSource
from sa_api.config import ApiConfig
from sa_api.relay_data import Snapshot, fetch_snapshot, relay_ok
from sa_core.anchors import Anchor, BlockstreamHeaders, OtsAnchor, anchored_time, verifier_map
from sa_core.bundle import (
    BundleCheck,
    BundleError,
    ChainItem,
    bundle_from_json,
    bundle_to_json,
    check_bundle,
    export_bundle,
)
from sa_core.chains import BalanceOracle, ChainRegistry, JsonRpcBackend, OracleError, Power
from sa_core.delegation import revocations_from_events
from sa_core.governance import (
    Collected,
    GovernanceError,
    TallyResult,
    anchored_map,
    collect,
    oracle_power,
    proof_from_attestation,
    tally,
    validate_proposal_event,
)
from sa_core.models import (
    ALLOCATION_KIND,
    ANCHOR_KIND,
    GOV_DELEGATION_KIND,
    PROPOSAL_KIND,
    REVOCATION_KIND,
    TOKEN_UNIT,
    VOTE_KIND,
    AnchorProof,
    Bundle,
    NostrEvent,
    PowerAllocation,
    ProviderRuleSet,
    Reveal,
    Revocation,
    Signal,
)
from sa_core.power import (
    PowerError,
    TierResolution,
    provider_power,
    release_ms,
    resolve_tier,
    validate_allocation_event,
    validate_ruleset_event,
)
from sa_core.trackrecord import Candle, TrackRecord, TrackRecordError, compute
from sa_engine.network.fixture import fixture_oracle
from sa_engine.network.replay import ReplayError
from sa_engine.report import BacktestReport

log = logging.getLogger("sa_api")

MAX_PROPOSALS_CHECKED = 200  # newest proposals examined per refresh
MAX_PROPOSALS_TALLIED = 50  # of those, how many qualifying ones are tallied (4 queries each)
PROPOSAL_TTL_FACTOR = 5  # a proposal's tally (or rejection) is reused for this many snapshot TTLs


class NotFoundError(Exception):
    """A provider, backtest or identity that is not there. The message says what to ask for instead."""


# ---------------------------------------------------------------- pure helpers

# the first ten are the dashboard's documented export; the rest add what a spreadsheet needs to replay a signal
CSV_COLUMNS = (
    "seq", "id", "created_ms", "created_iso", "anchored_ms", "instrument", "side", "action", "size_fraction",
    "private", "order_type", "limit_price", "stop_price", "timeframe_s",
)  # fmt: skip


def fills_of(report: BacktestReport) -> list[m.BacktestFill]:
    """The report's fills for the chart. ``kind``: walking the signed position (buy +qty, sell -qty), a fill that moves
    away from flat, or across it, is an entry; one that moves toward flat is an exit."""
    out: list[m.BacktestFill] = []
    pos = 0.0
    for f in report.fills:
        after = pos + (f.qty if f.side == "buy" else -f.qty)
        kind = "exit" if abs(after) < abs(pos) - 1e-12 and pos * after >= 0 else "entry"
        out.append(
            m.BacktestFill.model_validate(
                {"t": f.t, "side": f.side, "price": f.price, "qty": f.qty, "kind": kind}
            )
        )
        pos = 0.0 if abs(after) < 1e-12 else after
    return out


def tokens(units: int) -> float:
    """18-decimal integer units -> whole SA tokens."""
    return units / TOKEN_UNIT


def downsample[T](points: Sequence[T], limit: int) -> list[T]:
    """At most ``limit`` points, evenly spaced, always keeping the first and the last."""
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit - 1)]
    picked.append(points[-1])
    return picked


def tiers_of(rules: ProviderRuleSet) -> list[m.Tier]:
    """Best tier first, the order ``release[].tier`` indexes (``sa_core.power.resolve_tier``)."""
    if rules.private is not None:
        return [m.Tier(min_power=rules.private.min_power, delay_fraction=0.0)]
    assert rules.tiers is not None  # noqa: S101 - ProviderRuleSet guarantees one of the two
    ordered = sorted(rules.tiers, key=lambda t: t.min_power, reverse=True)
    return [m.Tier(min_power=t.min_power, delay_fraction=t.delay_fraction) for t in ordered]


def gap_runs(gaps: Sequence[str]) -> list[m.Gap]:
    """``"strategy:seq"`` entries -> runs of consecutive missing seqs as ``{after_seq, missing}``."""
    by_strategy: dict[str, list[int]] = {}
    for g in gaps:
        sid, _, raw = g.rpartition(":")
        by_strategy.setdefault(sid, []).append(int(raw))
    out: list[m.Gap] = []
    for sid in sorted(by_strategy):
        run: list[int] = []
        for seq in sorted(by_strategy[sid]):
            if run and seq != run[-1] + 1:
                out.append(m.Gap(after_seq=run[0] - 1, missing=len(run)))
                run = []
            run.append(seq)
        if run:
            out.append(m.Gap(after_seq=run[0] - 1, missing=len(run)))
    return out


def completeness(check: BundleCheck) -> float:
    """1 for a complete chain; otherwise the share of slots present, kept below 1 while anything is wrong."""
    if check.complete:
        return 1.0
    slots = len(check.items) + len(check.gaps)
    if slots == 0:
        return 0.0
    wrong = len(check.gaps) + len(check.problems)
    return min(0.99, max(0.0, 1 - wrong / slots))


def iso_ms(ms: int) -> str:
    return f"{datetime.fromtimestamp(ms // 1000, UTC):%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"


def iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_allocations(
    events: Sequence[NostrEvent], revocations: Sequence[Revocation] = ()
) -> dict[str, PowerAllocation]:
    """address -> the valid allocation with the highest nonce (equal nonces: smallest event id).

    Valid means a device key delegated by the allocating address published it (and was not revoked by then)."""
    best: dict[str, tuple[int, str, PowerAllocation]] = {}
    for ev in events:
        try:
            alloc = validate_allocation_event(ev, revocations)
        except PowerError:
            continue
        cur = best.get(alloc.address)
        if cur is None or (-alloc.nonce, ev.id) < (-cur[0], cur[1]):
            best[alloc.address] = (alloc.nonce, ev.id, alloc)
    return {a: v[2] for a, v in best.items()}


def _latest_rulesets(
    events: Sequence[NostrEvent], at: int, revocations: Sequence[Revocation] = ()
) -> dict[str, ProviderRuleSet]:
    """Newest valid rule set per provider, its delegation judged at ``at`` (now), as the relay and followers do."""
    best: dict[str, tuple[int, str, ProviderRuleSet]] = {}
    for ev in events:
        try:
            rules = validate_ruleset_event(ev, at, revocations)
        except PowerError as exc:
            log.warning("ignoring rule set %s: %s", ev.id[:12], exc)
            continue
        cur = best.get(rules.provider)
        if cur is None or (-ev.created_at, ev.id) < (-cur[0], cur[1]):
            best[rules.provider] = (ev.created_at, ev.id, rules)
    return {p: v[2] for p, v in best.items()}


def signal_status(anchored_ms: int | None) -> Literal["out", "waiting_for_time_stamp"]:
    """The provider's one status for a signal the API lists. Listed means signed (the chain check drops anything
    that does not verify) and on the relay; "out" adds a verified time-stamp, otherwise the time-stamp is what is missing."""
    return "out" if anchored_ms is not None else "waiting_for_time_stamp"


def follower_outcome(
    status: str, placed: bool, reason: str, exec_reason: str
) -> tuple[Literal["executed", "rejected", "pending"], str]:
    """The follower's ledger row as the follower sees it: executed (an order was placed), rejected (failed a check, or
    accepted but not placeable, with the reason the follower recorded), or still pending."""
    if status == "rejected":
        return "rejected", reason
    if status == "accepted":
        return ("executed", exec_reason) if placed else ("rejected", exec_reason or reason)
    return "pending", reason


def _signal_of(signal_json: str | None) -> Signal | None:
    if not signal_json:
        return None
    try:
        return Signal.model_validate_json(signal_json)
    except ValidationError:
        return None


@dataclass(frozen=True)
class _Item:
    item: ChainItem
    anchored: int | None


@dataclass(frozen=True)
class _Record:
    bundle: Bundle
    check: BundleCheck
    anchored: dict[str, int | None]
    track: TrackRecord | None
    error: str | None


# ---------------------------------------------------------------- service


class Service:
    def __init__(
        self,
        cfg: ApiConfig,
        *,
        oracle: BalanceOracle | None = None,
        fetcher: Callable[[Sequence[str]], Any] = fetch_snapshot,
        candles: CandleLoader | None = None,
        verifiers: Mapping[str, Anchor] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.oracle = oracle or (
            fixture_oracle(cfg.balances)
            if cfg.balances is not None
            else BalanceOracle(
                ChainRegistry.default(), JsonRpcBackend(timeout_s=6.0, retries=1), ttl_s=300
            )
        )
        self._fetch = fetcher
        self._candles = candles or CandleSource(cfg)
        self._verifiers: dict[str, Anchor] = (
            dict(verifiers) if verifiers is not None else tsa_verifiers(cfg.tsa_config)
        )
        self._snap: Snapshot | None = None
        self._snap_lock = asyncio.Lock()
        self._records: dict[str, tuple[str, _Record]] = {}
        self._first_seen: dict[str, int] = {}
        self._proposals: tuple[float, list[m.Proposal]] | None = None
        self._proposal_cache: dict[str, tuple[float, m.Proposal | None]] = {}  # None: rejected
        self._bundle_cache: dict[tuple[str, int], Bundle | None] = {}

    # ------------------------------------------------------------ snapshot

    async def snapshot(self) -> Snapshot:
        async with self._snap_lock:
            snap = self._snap
            if snap is None or self.clock() - snap.taken_at >= self.cfg.cache_ttl_s:
                snap = await self._fetch(self.cfg.relays)
                if not snap.answered and self._snap is not None:
                    snap = self._snap  # every relay down: keep showing the last good data
                self._snap = snap
            return snap

    # ------------------------------------------------------------ relay facts

    def _revocations(self, snap: Snapshot) -> list[Revocation]:
        return revocations_from_events(snap.of_kind(REVOCATION_KIND))

    def _rulesets(self, snap: Snapshot) -> dict[str, ProviderRuleSet]:
        return _latest_rulesets(snap.of_kind(37318), int(self.clock()), self._revocations(snap))

    def _allocations(self, snap: Snapshot) -> dict[str, PowerAllocation]:
        return _latest_allocations(snap.of_kind(ALLOCATION_KIND), self._revocations(snap))

    # ------------------------------------------------------------ power

    async def _power(self, address: str) -> Power | None:
        try:
            return await asyncio.to_thread(self.oracle.power, address)
        except OracleError as exc:
            log.warning("token power of %s unreadable: %s", address, exc)
            return None

    # ------------------------------------------------------------ bundles and records

    def _bundles_for(self, provider: str) -> list[Bundle]:
        folder = self.cfg.bundles_dir
        if folder is None or not Path(folder).is_dir():
            return []
        out: list[Bundle] = []
        for path in sorted(Path(folder).glob("*.json")):
            try:
                key = (str(path), path.stat().st_mtime_ns)
            except OSError:
                continue
            if key not in self._bundle_cache:
                try:
                    self._bundle_cache[key] = bundle_from_json(path.read_text(encoding="utf-8"))
                except (OSError, BundleError) as exc:
                    log.warning("ignoring bundle %s: %s", path, exc)
                    self._bundle_cache[key] = None
            bundle = self._bundle_cache[key]
            if bundle is not None and bundle.provider == provider:
                out.append(bundle)
        return out

    def _proofs(self, snap: Snapshot) -> list[AnchorProof]:
        proofs: dict[tuple[str, str, str], AnchorProof] = {}
        for ev in snap.of_kind(ANCHOR_KIND):
            try:
                p = proof_from_attestation(ev)
            except GovernanceError:
                continue
            proofs[(p.digest, p.kind, p.data)] = p
        return list(proofs.values())

    async def _record(self, snap: Snapshot, provider: str) -> _Record:
        extra = self._bundles_for(provider)
        chain_events = [
            e for e in snap.events.values() if e.pubkey == provider and e.kind in (9317, 9318)
        ]
        proofs = {(p.digest, p.kind, p.data): p for p in self._proofs(snap)}
        reveals: dict[str, Reveal] = {}
        for b in extra:
            chain_events.extend(b.events)
            proofs.update({(p.digest, p.kind, p.data): p for p in b.proofs})
            reveals.update({r.commitment_id: r for r in b.reveals})
        bundle = export_bundle(
            provider, chain_events, list(proofs.values()), list(reveals.values()), exported_at=0
        )
        revs = [r for r in self._revocations(snap) if r.nostr_pubkey == provider]
        digest = hashlib.sha256(
            json.dumps(
                [
                    sorted(e.id for e in bundle.events),
                    sorted(map(str, proofs)),
                    sorted(reveals),
                    sorted((r.evm_address, r.effective_at) for r in revs),
                ]
            ).encode()
        ).hexdigest()
        cached = self._records.get(provider)
        if cached is not None and cached[0] == digest:
            return cached[1]
        rec = await asyncio.to_thread(self._compute, provider, bundle, revs)
        self._records[provider] = (digest, rec)
        return rec

    def _compute(self, provider: str, bundle: Bundle, revs: Sequence[Revocation] = ()) -> _Record:
        check = check_bundle(bundle, revocations=revs)
        anchored = {
            i.event.id: anchored_time(bytes.fromhex(i.event.id), bundle.proofs, self._verifiers)
            for i in check.items
        }
        sigs = [i.signal for i in check.items if i.signal is not None]
        candles: dict[str, list[Candle]] = {}
        error: str | None = None
        if sigs:
            try:
                candles = self._candles.load(
                    provider,
                    sorted({s.instrument for s in sigs}),
                    min(s.created_ms for s in sigs),
                    max(s.created_ms for s in sigs),
                )
            except ReplayError as exc:
                error = str(exc)
                log.warning("no candles for provider %s: %s", provider[:12], exc)
        track: TrackRecord | None = None
        if error is None:
            try:
                track = compute(
                    bundle,
                    candles,
                    verifiers=self._verifiers,
                    tolerance_s=self.cfg.tolerance_s,
                    revocations=revs,
                )
            except TrackRecordError as exc:
                error = str(exc)
                log.warning("cannot score provider %s: %s", provider[:12], exc)
        return _Record(bundle, check, anchored, track, error)

    # ------------------------------------------------------------ providers

    def _name(self, pubkey: str, rec: _Record) -> str:
        if pubkey in self.cfg.labels:
            return self.cfg.labels[pubkey]
        if rec.check.items:
            return rec.check.items[0].strategy_id
        return f"provider {pubkey[:8]}"

    async def _followers_of(self, snap: Snapshot, pubkey: str) -> tuple[int, float]:
        n, backing = 0, 0
        for address, alloc in self._allocations(snap).items():
            bps = alloc.providers.get(pubkey, 0)
            if bps <= 0:
                continue
            n += 1
            power = await self._power(address)
            if power is not None:
                backing += power.total * bps // 10_000
        return n, tokens(backing)

    def _on_time(self, rec: _Record) -> tuple[int, int]:
        """(items anchored on time, items): only an anchor within the tolerance counts as coverage."""
        if rec.track is not None:
            cov = rec.track.report["anchor_coverage"]
            return int(cov["anchored"]), int(cov["total"])
        return 0, len(rec.check.items)

    def _summary(self, rec: _Record) -> m.RecordSummary:
        report = rec.track.report if rec.track is not None else None
        on_time, total = self._on_time(rec)
        win = report["win_rate"] if report else None
        sigs = report["signals"] if report else {}
        return m.RecordSummary(
            pnl_pct=float(report["pnl"]) * 100 if report else 0.0,
            win_rate=float(win) if win is not None else 0.0,
            max_dd_pct=float(report["max_drawdown"]) * 100 if report else 0.0,
            trades=int(report["trades"]) if report else 0,
            completeness=completeness(rec.check),
            anchor_coverage=on_time / total if total else 0.0,
            signatures_ok=rec.check.signature_failures == 0,
            excluded=sum(int(n) for n in sigs.get("excluded", {}).values()),
            late_anchor=int(sigs.get("late_anchor", 0)),
            unanchored=int(sigs.get("unanchored", 0)),
        )

    async def _provider(self, snap: Snapshot, rules: ProviderRuleSet) -> m.Provider:
        rec = await self._record(snap, rules.provider)
        followers, backing = await self._followers_of(snap, rules.provider)
        return m.Provider(
            pubkey=rules.provider,
            name=self._name(rules.provider, rec),
            sa_address=rules.sa_address,
            mode="private" if rules.private is not None else "public",
            timeframe_s=rules.timeframe,
            tiers=tiers_of(rules),
            followers=followers,
            power_backing=backing,
            record_summary=self._summary(rec),
        )

    async def providers(self) -> list[m.Provider]:
        snap = await self.snapshot()
        rules = self._rulesets(snap)
        out = [await self._provider(snap, r) for r in rules.values()]
        return sorted(out, key=lambda p: (-p.power_backing, p.pubkey))

    async def provider(self, pubkey: str) -> m.Provider | None:
        snap = await self.snapshot()
        rules = self._rulesets(snap).get(pubkey)
        return None if rules is None else await self._provider(snap, rules)

    async def record(self, pubkey: str) -> m.TrackRecord | None:
        snap = await self.snapshot()
        if pubkey not in self._rulesets(snap):
            return None
        rec = await self._record(snap, pubkey)
        series = rec.track.series if rec.track is not None else None
        cap = self.cfg.capital
        equity = [
            m.EquityPoint(t=t, equity=round(float(e) * cap, 2))
            for t, e in (series.equity if series else ())
        ]
        trades = [
            m.Trade(
                open_t=t.open_ms, close_t=t.close_ms, side="buy" if t.side == "buy" else "sell",
                entry=float(t.entry), exit=float(t.exit), pnl_pct=round(float(t.pnl_pct), 4),
            )
            for t in (series.trades if series else ())
        ]  # fmt: skip
        by_id = {i.ref: i for i in rec.check.items}
        unrevealed = [
            m.UnrevealedCommitment(
                id=cid,
                seq=by_id[cid].seq if cid in by_id else None,
                committed_ms=(by_id[cid].event.created_at if cid in by_id else 0) * 1000,
            )
            for cid in rec.check.unrevealed
        ]
        anchored, total = self._on_time(rec)
        return m.TrackRecord(
            equity=downsample(equity, self.cfg.max_points),
            trades=trades[-200:],
            gaps=gap_runs(rec.check.gaps),
            unrevealed_commitments=unrevealed,
            verification=m.Verification(
                signatures_ok=rec.check.signature_failures == 0,
                chain_complete=rec.check.complete,
                anchored_pct=round(anchored / total * 100, 2) if total else 0.0,
            ),
        )

    # ------------------------------------------------------------ signals

    def _tier_of(self, home: Path, pubkey: str, rules: ProviderRuleSet) -> TierResolution | None:
        """The tier the follower at ``home`` has with this provider, or None when its identity, allocation or power
        cannot be read or it is not entitled."""
        try:
            ident = json.loads((home / "identity.json").read_text(encoding="utf-8"))
            address = str(ident["address"])
            alloc_path = home / "allocation.json"
            alloc = (
                PowerAllocation.model_validate_json(alloc_path.read_text(encoding="utf-8"))
                if alloc_path.exists()
                else None
            )
            power = self.oracle.power(address).total
            res = resolve_tier(rules, provider_power(alloc, pubkey, power))
        except (OSError, KeyError, ValueError, OracleError, PowerError):
            return None
        return res if res.entitled else None

    def _observed(self, pubkey: str, rules: ProviderRuleSet) -> dict[str, dict[int, int]]:
        """DEV VIEWS ONLY. chain reference -> {tier index: earliest receipt time (ms) seen by a follower of that tier}."""
        out: dict[str, dict[int, int]] = {}
        for src in self.cfg.followers:
            home = Path(src.home)
            res = self._tier_of(home, pubkey, rules)
            if res is None:
                continue
            tier = res.tier_index or 0
            ledger = Path(src.ledger) if src.ledger else home / "follower" / "ledger.sqlite"
            if not ledger.exists():
                continue
            try:
                with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=2)) as db:
                    rows = db.execute(
                        "SELECT ref, MIN(received_ms) FROM signals WHERE provider = ? AND ref IS NOT NULL GROUP BY ref",
                        (pubkey,),
                    ).fetchall()
            except sqlite3.Error as exc:
                log.warning("cannot read follower ledger %s: %s", ledger, exc)
                continue
            for ref, received in rows:
                slot = out.setdefault(str(ref), {})
                slot[tier] = min(int(received), slot.get(tier, int(received)))
        return out

    def _releases(
        self, rules: ProviderRuleSet, created_ms: int, observed: Mapping[int, int] | None
    ) -> list[m.Release]:
        """Per tier: when the relay releases the signal. Without observations the receipt time is taken as
        ``created_ms`` (the relay requires it within seconds); with a follower's ledger the receipt time is
        estimated from the earliest observed release minus that tier's delay, so tiers stay consistent."""
        if rules.private is not None:
            delays = [(0, 0.0)]
        else:
            assert rules.tiers is not None  # noqa: S101
            ordered = sorted(rules.tiers, key=lambda t: t.min_power, reverse=True)
            delays = [(i, t.delay_fraction) for i, t in enumerate(ordered)]
        delay_ms = {
            index: round(
                release_ms(
                    0,
                    TierResolution(True, index, fraction, rules.private is not None, False),
                    rules.timeframe,
                )
                * self.cfg.time_scale
            )
            for index, fraction in delays
        }
        receipt = created_ms
        if observed:
            receipt = min(at - delay_ms[i] for i, at in observed.items() if i in delay_ms)
        out = [
            m.Release(
                tier=i,
                at_ms=observed[i] if observed and i in observed else receipt + delay_ms[i],
                observed=bool(observed and i in observed),
            )
            for i, _ in delays
        ]
        return sorted(out, key=lambda r: (r.at_ms, r.tier))

    async def _signal_items(
        self, pubkey: str, limit: int
    ) -> tuple[ProviderRuleSet, list[_Item]] | None:
        snap = await self.snapshot()
        rules = self._rulesets(snap).get(pubkey)
        if rules is None:
            return None
        rec = await self._record(snap, pubkey)
        items: list[_Item] = []
        for item in sorted(
            rec.check.items, key=lambda i: (i.event.created_at, i.seq), reverse=True
        ):
            if item.signal is None:
                continue  # a commitment nobody revealed: see the record's unrevealed list
            items.append(_Item(item, rec.anchored.get(item.event.id)))
            if len(items) >= limit:
                break
        return rules, items

    @staticmethod
    def _signal(it: _Item, sig: Signal) -> m.Signal:
        anchored_ms = None if it.anchored is None else it.anchored * 1000
        return m.Signal(
            id=it.item.ref, seq=it.item.seq, created_ms=sig.created_ms, anchored_ms=anchored_ms,
            status=signal_status(anchored_ms),
            instrument=sig.instrument, side=sig.side, action=sig.action, size_fraction=sig.size_fraction,
            private=it.item.kind == "commitment",
        )  # fmt: skip

    async def signals(self, pubkey: str, limit: int) -> list[m.Signal] | None:
        """The provider's own view: each signal with one status (``signal_status``). Nothing about followers."""
        got = await self._signal_items(pubkey, limit)
        if got is None:
            return None
        out: list[m.Signal] = []
        for it in got[1]:
            assert it.item.signal is not None  # noqa: S101
            out.append(self._signal(it, it.item.signal))
        return out

    async def dev_signals(self, pubkey: str, limit: int) -> list[m.DevSignal] | None:
        """DEV VIEWS ONLY (``dev_views``): every signal with the release time of each tier, observed from the
        configured followers' ledgers where there are any. Not a product view: it lists other followers' receipts."""
        got = await self._signal_items(pubkey, limit)
        if got is None:
            return None
        rules, items = got
        observed = (
            await asyncio.to_thread(self._observed, pubkey, rules) if self.cfg.followers else {}
        )
        out: list[m.DevSignal] = []
        for it in items:
            sig = it.item.signal
            assert sig is not None  # noqa: S101
            base = self._signal(it, sig)
            out.append(
                m.DevSignal(
                    **base.model_dump(),
                    release=self._releases(rules, sig.created_ms, observed.get(it.item.ref)),
                )
            )
        return out

    # ------------------------------------------------------------ the follower's own view

    def _my_signals(
        self, home: Path, rulesets: Mapping[str, ProviderRuleSet], limit: int
    ) -> list[m.FollowerSignal]:
        ledger = home / "follower" / "ledger.sqlite"
        if not ledger.exists():
            return []
        try:
            with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT event_id, provider, seq, ref, received_ms, created_ms, status, reason, signal_json,"
                    " placed, exec_reason FROM signals ORDER BY received_ms DESC, event_id LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("cannot read follower ledger %s: %s", ledger, exc)
            return []
        tiers: dict[str, TierResolution | None] = {}
        out: list[m.FollowerSignal] = []
        for r in rows:
            provider = str(r["provider"])
            rules = rulesets.get(provider)
            if provider not in tiers:
                tiers[provider] = None if rules is None else self._tier_of(home, provider, rules)
            res = tiers[provider]
            delay_ms = (
                None
                if res is None or rules is None
                else round(release_ms(0, res, rules.timeframe) * self.cfg.time_scale)
            )
            sig = _signal_of(r["signal_json"])
            outcome, reason = follower_outcome(
                str(r["status"]), bool(r["placed"]), str(r["reason"]), str(r["exec_reason"])
            )
            out.append(
                m.FollowerSignal(
                    id=str(r["ref"] or r["event_id"]), provider=provider, seq=r["seq"],
                    received_ms=int(r["received_ms"]), created_ms=r["created_ms"],
                    tier=None if res is None else (res.tier_index or 0), delay_ms=delay_ms,
                    outcome=outcome, reason=reason,
                    instrument=None if sig is None else sig.instrument,
                    side=None if sig is None else sig.side,
                    action=None if sig is None else sig.action,
                )
            )  # fmt: skip
        return out

    async def my_signals(
        self, limit: int, session_address: str | None = None
    ) -> list[m.FollowerSignal]:
        """What this follower received, read only from its own ledger (``me_home``): when, the delay its tier
        schedules, and whether it executed or rejected the signal and why. A wallet session for another address
        has no ledger here and sees nothing."""
        home = self.cfg.me_home
        if home is None:
            raise NotFoundError(
                "no follower is configured; start sa-api with --me-home DIR (the `sa` home of the follower) to show My signals"
            )
        home = Path(home)
        try:
            address = str(
                json.loads((home / "identity.json").read_text(encoding="utf-8"))["address"]
            )
        except (OSError, KeyError, ValueError) as exc:
            raise NotFoundError(
                f"{home}/identity.json has no SA address ({exc}); run `sa identity` in that home"
            ) from exc
        if session_address is not None and session_address.lower() != address.lower():
            return []
        snap = await self.snapshot()
        rulesets = self._rulesets(snap)
        return await asyncio.to_thread(self._my_signals, home, rulesets, limit)

    # ------------------------------------------------------------ backtests

    def _reports(self) -> dict[str, BacktestReport]:
        folder = self.cfg.backtests_dir
        if folder is None or not Path(folder).is_dir():
            return {}
        out: dict[str, BacktestReport] = {}
        for path in sorted(Path(folder).glob("*.json")):
            try:
                out[path.stem] = BacktestReport.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError) as exc:
                log.warning("ignoring backtest file %s: %s", path, exc)
        return out

    def _backtest(self, bid: str, r: BacktestReport) -> m.Backtest:
        curve = [m.EquityPoint(t=t, equity=round(e, 2)) for t, e in r.equity_curve]
        return m.Backtest(
            id=bid, strategy_name=r.strategy, instrument=r.instrument, timeframe=r.timeframe, fills=fills_of(r),
            start=f"{r.start:%Y-%m-%d}", end=f"{r.end:%Y-%m-%d}", pnl_pct=r.pnl_pct, trades=r.trade_count,
            win_rate=r.win_rate, max_dd_pct=r.max_drawdown_pct, equity=downsample(curve, self.cfg.max_points),
        )  # fmt: skip

    def backtests(self) -> list[m.Backtest]:
        return [self._backtest(i, r) for i, r in self._reports().items()]

    def backtest(self, bid: str) -> m.Backtest | None:
        r = self._reports().get(bid)
        return None if r is None else self._backtest(bid, r)

    # ------------------------------------------------------------ export

    async def export(self, pubkey: str, fmt: str) -> tuple[str, str, str] | None:
        """(body, media type, file name) of a provider's signed history: ``bundle`` is what ``sa verify`` accepts,
        ``csv`` one row per signal this API can show (a private signal only once revealed, as in ``signals``)."""
        snap = await self.snapshot()
        rules = self._rulesets(snap).get(pubkey)
        if rules is None:
            return None
        rec = await self._record(snap, pubkey)
        stem = f"superalgos-{pubkey[:12]}"
        if fmt == "bundle":
            bundle = rec.bundle.model_copy(update={"exported_at": int(self.clock())})
            return bundle_to_json(bundle), "application/json", f"{stem}-bundle.json"
        buf = io.StringIO()
        out = csv.writer(buf, lineterminator="\n")
        out.writerow(CSV_COLUMNS)
        for item in sorted(rec.check.items, key=lambda i: (i.event.created_at, i.seq)):
            sig = item.signal
            if sig is None:
                continue  # an unrevealed commitment: nothing the API could show
            anchored = rec.anchored.get(item.event.id)
            out.writerow(
                [
                    item.seq, item.ref, sig.created_ms, iso_ms(sig.created_ms),
                    "" if anchored is None else anchored * 1000,
                    sig.instrument, sig.side, sig.action, sig.size_fraction, str(item.kind == "commitment").lower(),
                    sig.order_type, "" if sig.limit_price is None else sig.limit_price,
                    "" if sig.stop_price is None else sig.stop_price, sig.timeframe,
                ]
            )  # fmt: skip
        return buf.getvalue(), "text/csv; charset=utf-8", f"{stem}-signals.csv"

    # ------------------------------------------------------------ me

    async def me(self, session_address: str | None = None) -> m.Me:
        """My power: for a wallet session, that address (balances from the oracle, the newest valid allocation the
        relay holds for it); without one, the demo identity from ``me_home``."""
        if session_address is not None:
            return await self._me_of(session_address)
        home = self.cfg.me_home
        if home is None:
            raise NotFoundError(
                "no identity is configured; sign in with a wallet, or start sa-api with --me-home DIR (an `sa` home) to show My power"
            )
        try:
            address = str(
                json.loads((Path(home) / "identity.json").read_text(encoding="utf-8"))["address"]
            )
        except (OSError, KeyError, ValueError) as exc:
            raise NotFoundError(
                f"{home}/identity.json has no SA address ({exc}); run `sa identity` in that home"
            ) from exc
        alloc_path = Path(home) / "allocation.json"
        alloc = (
            PowerAllocation.model_validate_json(alloc_path.read_text(encoding="utf-8"))
            if alloc_path.exists()
            else None
        )
        return await self._me(address, alloc, signed_in=False)

    async def _me_of(self, address: str) -> m.Me:
        snap = await self.snapshot()
        alloc = self._allocations(snap).get(address.lower())
        return await self._me(address, alloc, signed_in=True)

    async def _me(self, address: str, alloc: PowerAllocation | None, *, signed_in: bool) -> m.Me:
        power = await self._power(address)
        registry = self.oracle.registry
        balances = (
            [
                m.ChainBalance(chain=registry.get(cid).name, balance=tokens(v))
                for cid, v in power.per_chain.items()
            ]
            if power is not None
            else []
        )
        return m.Me(
            sa_address=address,
            signed_in=signed_in,
            token_power=tokens(power.total) if power is not None else 0.0,
            balances=balances,
            allocations=m.Allocations(
                governance_bps=alloc.governance if alloc else 0,
                providers=dict(alloc.providers) if alloc else {},
            ),
        )

    # ------------------------------------------------------------ governance

    def _tally(
        self, prop: NostrEvent, events: list[NostrEvent], proofs: list[AnchorProof], got: Any
    ) -> TallyResult:
        if self.cfg.governance_anchor == "created-at":
            anchored: Mapping[str, int | None] = {e.id: e.created_at for e in events}
        else:
            anchored = anchored_map(events, proofs, verifier_map(OtsAnchor(BlockstreamHeaders())))
        return tally(
            prop, events, anchored=anchored, power=oracle_power(self.oracle), first_seen=got.first_seen,
            delay_d=self.cfg.delay_d_s, coverage=got.coverage, min_proposer_power=self.cfg.min_proposer_power,
        )  # fmt: skip

    async def proposals(self) -> list[m.Proposal]:
        cached = self._proposals
        if cached is not None and self.clock() - cached[0] < self.cfg.cache_ttl_s:
            return cached[1]
        result = await self._tally_proposals()
        self._proposals = (self.clock(), result)
        return result

    async def _tally_proposals(self) -> list[m.Proposal]:
        """Tally every qualifying proposal, newest first. Work is bounded (a spammer must not turn this into an
        amplifier): the newest ``MAX_PROPOSALS_CHECKED`` proposals are checked, each rejection or tally is cached
        per proposal id for ``PROPOSAL_TTL_FACTOR`` snapshot TTLs, a proposal without a delegated, powerful
        proposer costs no vote query, and the four shared kinds are fetched once, not once per proposal."""
        relays = list(self.cfg.relays)
        first = await collect(relays, [{"kinds": [PROPOSAL_KIND]}])
        now = self.clock()
        ttl = self.cfg.cache_ttl_s * PROPOSAL_TTL_FACTOR
        newest = sorted(
            (e for e in first.events.values() if e.kind == PROPOSAL_KIND),
            key=lambda e: (-e.created_at, e.id),
        )[:MAX_PROPOSALS_CHECKED]
        power = oracle_power(self.oracle)
        shared: Collected | None = None
        out: list[tuple[int, m.Proposal]] = []
        for prop_event in newest:
            pid = prop_event.id
            hit = self._proposal_cache.get(pid)
            if hit is not None and now - hit[0] < ttl:
                if hit[1] is not None:
                    out.append((prop_event.created_at, hit[1]))
                continue
            if sum(1 for _, p in out) >= MAX_PROPOSALS_TALLIED:
                continue
            try:
                prop = validate_proposal_event(prop_event, power, self.cfg.min_proposer_power)
            except GovernanceError as exc:
                log.warning("ignoring proposal %s: %s", pid[:12], exc)
                self._proposal_cache[pid] = (now, None)
                continue
            if shared is None:
                shared = await collect(
                    relays,
                    [
                        {"kinds": [ALLOCATION_KIND]},
                        {"kinds": [GOV_DELEGATION_KIND]},
                        {"kinds": [REVOCATION_KIND]},
                        {"kinds": [ANCHOR_KIND]},
                    ],
                    prior_first_seen=self._first_seen,
                )
            votes = await collect(
                relays,
                [{"kinds": [VOTE_KIND], "#e": [pid]}],
                prior_first_seen=self._first_seen,
            )
            got = Collected(
                events={**shared.events, **votes.events},
                first_seen={**shared.first_seen, **votes.first_seen},
                coverage=shared.coverage if not shared.coverage.complete else votes.coverage,
            )
            self._first_seen.update(got.first_seen)
            proofs = []
            for ev in got.events.values():
                if ev.kind == ANCHOR_KIND:
                    try:
                        proofs.append(proof_from_attestation(ev))
                    except GovernanceError:
                        continue
            evs = [e for e in got.events.values() if e.kind != ANCHOR_KIND]
            try:
                result = await asyncio.to_thread(self._tally, prop_event, evs, proofs, got)
            except GovernanceError as exc:  # e.g. the proposer's device key was revoked
                log.warning("ignoring proposal %s: %s", pid[:12], exc)
                self._proposal_cache[pid] = (now, None)
                continue
            shown = m.Proposal(
                id=pid, title=prop.title, options=list(prop.options), deadline=iso(prop.deadline),
                tally={o: tokens(int(t)) for o, t in zip(result.options, result.totals, strict=True)},
                complete=result.complete, relays_answered=list(result.relays_answered),
            )  # fmt: skip
            self._proposal_cache[pid] = (now, shown)
            out.append((prop_event.created_at, shown))
        if (
            len(self._proposal_cache) > 4 * MAX_PROPOSALS_CHECKED
        ):  # forget proposals that left the newest window
            keep = {e.id for e in newest}
            self._proposal_cache = {k: v for k, v in self._proposal_cache.items() if k in keep}
        return [p for _, p in sorted(out, key=lambda x: (-x[0], x[1].id))]

    # ------------------------------------------------------------ network

    def _chain_status(self) -> list[m.ChainStatus]:
        backend = self.oracle.backend
        out: list[m.ChainStatus] = []
        probe = (
            JsonRpcBackend(timeout_s=4.0, retries=0)
            if isinstance(backend, JsonRpcBackend)
            else None
        )
        for chain in self.oracle.registry.active:
            if probe is None:
                out.append(m.ChainStatus(name=chain.name, ok=True, block=None))
                continue
            try:
                block = int(probe.rpc(chain, "eth_blockNumber", []), 16)
            except (OracleError, ValueError):
                out.append(m.ChainStatus(name=chain.name, ok=False, block=None))
            else:
                out.append(m.ChainStatus(name=chain.name, ok=True, block=block))
        return out

    async def network(self) -> m.Network:
        oks = await asyncio.gather(*(relay_ok(u) for u in self.cfg.relays))
        chains = await asyncio.to_thread(self._chain_status)
        return m.Network(
            time_scale=self.cfg.time_scale,
            dev_views=self.cfg.dev_views,
            relays=[
                m.RelayStatus(url=u, ok=ok) for u, ok in zip(self.cfg.relays, oks, strict=True)
            ],
            chains=chains,
        )
