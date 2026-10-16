"""Follower bot: authenticate, allocate, subscribe, verify every signal, execute it on paper, keep a ledger.

Checks, in order, for each signal (relayed or read from a file, SEP-3.29):

1. event id + BIP-340 signature, one valid ``sa-delegation`` tag at receive time that the SA address has not
   revoked (kind 9334, from its ``effective_at``), content schema
   (private signals are NIP-44 decrypted first);
2. the delegating SA address equals the provider's rule set (when the rule set is known);
3. not stale and not from the future;
4. the signal's ``timeframe`` equals the one bound in the rule set (SEP-3.40);
5. chain continuity: ``seq`` is the next one and ``prev`` is the previous signal's reference. The first
   signal seen sets the baseline. A gap rejects the signal (and moves the head, so later ones verify);
6. anchors are checked when their kind-1040 attestations arrive; a late or invalid anchor is recorded,
   it cannot undo a paper order already placed.

A rejected signal is never executed. Everything, accepted or not, is in the ledger with its receive time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError
from sa_core.anchors import Anchor, verify_proofs
from sa_core.delegation import DelegationError, delegation_tag, validate_revocation_event
from sa_core.governance import GovernanceError, proof_from_attestation
from sa_core.keys import DeviceKey
from sa_core.models import (
    ALLOCATION_KIND,
    ANCHOR_KIND,
    COMMITMENT_KIND,
    REVOCATION_KIND,
    RULESET_KIND,
    SIGNAL_KIND,
    AnchorProof,
    Commitment,
    Delegation,
    NostrEvent,
    PowerAllocation,
    ProviderRuleSet,
    Revocation,
    Signal,
)
from sa_core.nostr import NostrError, RelayClient, event_from_json
from sa_core.power import PowerError, allocation_event, check_timeframe, validate_ruleset_event
from sa_core.signals import (
    SignalError,
    commitment_digest,
    open_private_signal,
    verify_commitment_event,
    verify_signal_event,
)
from websockets.exceptions import WebSocketException

from sa_engine.intent import OrderIntent
from sa_engine.network.executor import PaperExecutor
from sa_engine.network.ledger import Ledger, LedgerRow

log = logging.getLogger("sa.follower")


@dataclass(frozen=True)
class _Verified:
    """A signal that passed every check except chain order; ``ref`` is what the next signal's ``prev`` must be."""

    ev: NostrEvent
    signal: Signal
    ref: str
    private: bool
    source: str
    received_ms: int


class FollowerError(Exception):
    """The follower cannot start or continue. The message says what to change."""


class FollowerBot:
    def __init__(
        self,
        *,
        device: DeviceKey,
        delegation: Delegation,
        relay_url: str,
        providers: Sequence[str],
        executor: PaperExecutor,
        ledger: Ledger,
        verifiers: Mapping[str, Anchor] | None = None,
        rules: Mapping[str, ProviderRuleSet] | None = None,
        allocation: PowerAllocation | None = None,
        clock: Callable[[], float] = time.time,
        tolerance_s: int = 60,
        max_age_s: float | None = None,
        skew_s: float = 10.0,
        allow_gaps: bool = False,
        poll_s: float = 0.5,
        reorder_wait_s: float = 1.0,
        relay_factory: Callable[[], RelayClient] | None = None,
    ) -> None:
        if delegation.nostr_pubkey != device.pubkey_hex:
            raise FollowerError(
                f"delegation names device key {delegation.nostr_pubkey[:12]}.., not {device.pubkey_hex[:12]}..; "
                "create a delegation for this key"
            )
        self.device, self.delegation, self.relay_url = device, delegation, relay_url
        self.providers = list(dict.fromkeys(providers))
        self.executor, self.ledger = executor, ledger
        self.verifiers: Mapping[str, Anchor] = verifiers or {}
        self.rules: dict[str, ProviderRuleSet] = dict(rules or {})
        self.allocation, self.clock = allocation, clock
        self.revocations: dict[tuple[str, str, int], Revocation] = {}  # kind 9334 for the keys we depend on
        self.tolerance_s, self.max_age_s, self.skew_s = tolerance_s, max_age_s, skew_s
        self.allow_gaps, self.poll_s, self.reorder_wait_s = allow_gaps, poll_s, reorder_wait_s
        self._relay_factory = relay_factory or (
            lambda: RelayClient(relay_url, auth_key=device, auth_tags=[delegation_tag(delegation)])
        )
        self.ready = asyncio.Event()
        self.authenticated = False
        self._proofs: dict[str, list[AnchorProof]] = {}
        self._commitments: dict[str, tuple[Commitment, str, str]] = {}  # digest -> (commitment, event id, provider)
        self._waiting: dict[str, NostrEvent] = {}  # digest -> private signal waiting for its commitment
        # chain (provider, strategy) -> seq -> verified signal that arrived ahead of its predecessors
        self._held: dict[tuple[str, str], dict[int, _Verified]] = {}
        self._timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ session
    async def run(self, stop: asyncio.Event, duration_s: float | None = None) -> None:
        """Run until ``stop`` is set or ``duration_s`` passes; reconnects if the relay drops."""
        poller = asyncio.create_task(self._poll_loop(stop))
        deadline = None if duration_s is None else time.monotonic() + duration_s
        since = int(self.clock()) - 2
        try:
            while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
                try:
                    await self._session(stop, deadline, since)
                except (NostrError, OSError, WebSocketException) as exc:
                    log.warning("relay session ended (%s); reconnecting in 1 s", exc)
                    since = int(self.clock()) - 5
                    await asyncio.sleep(1.0)
        finally:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller

    async def _session(self, stop: asyncio.Event, deadline: float | None, since: int) -> None:
        relay = self._relay_factory()
        await relay.connect()
        try:
            try:
                await relay.authenticate(timeout=5.0)
                self.authenticated = True
            except NostrError as exc:
                log.warning("running without AUTH (zero-power service): %s", exc)
            if self.allocation is not None:
                await self._publish_allocation(relay)
            await self._load_revocations(relay)
            await self._load_rules(relay)
            filters = [
                {"kinds": [SIGNAL_KIND, COMMITMENT_KIND, ANCHOR_KIND], "authors": self.providers, "since": since},
                {"kinds": [REVOCATION_KIND], "since": since},  # anyone may publish a revocation
            ]
            consumer = asyncio.create_task(self._consume(relay, filters))
            await asyncio.sleep(0.3)  # the REQ goes out as the consumer starts iterating
            self.ready.set()
            waiter = asyncio.create_task(stop.wait())
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, _ = await asyncio.wait({consumer, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for task in (consumer, waiter):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if consumer in done:
                consumer.result()  # the relay closed the stream: raises, and run() reconnects
        finally:
            await relay.close()

    async def _consume(self, relay: RelayClient, filters: list[dict[str, object]]) -> None:
        async for ev in relay.subscribe(filters):
            try:
                await self.ingest(ev)
            except Exception:  # one bad event must never stop the stream
                log.exception("ingest of %s failed", ev.id[:12])
        raise NostrError(f"relay {self.relay_url} closed the subscription")

    async def _poll_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self.poll_s)
            for ex in await self.executor.poll(int(self.clock() * 1000)):
                self.ledger.set_execution(ex)

    async def _publish_allocation(self, relay: RelayClient) -> None:
        alloc = self.allocation
        assert alloc is not None
        mine = await relay.query([{"kinds": [ALLOCATION_KIND], "authors": [self.device.pubkey_hex]}])
        for ev in mine:
            if ev.content == alloc.model_dump_json():
                log.info("allocation nonce %d is already on the relay", alloc.nonce)
                return
        res = await relay.publish(allocation_event(self.device, alloc, self.delegation))
        if not res.accepted:
            raise FollowerError(f"relay {self.relay_url} refused the allocation (nonce {alloc.nonce}): {res.message}")

    async def _load_revocations(self, relay: RelayClient) -> None:
        for ev in await relay.query([{"kinds": [REVOCATION_KIND]}]):
            self._on_revocation(ev)

    def _on_revocation(self, ev: NostrEvent) -> None:
        """Keep a valid revocation of a provider device key (or ours): from ``effective_at`` it speaks for nobody."""
        try:
            rev = validate_revocation_event(ev)
        except DelegationError as exc:
            log.warning("ignoring revocation %s: %s", ev.id[:12], exc)
            return
        if rev.nostr_pubkey in self.providers or rev.nostr_pubkey == self.device.pubkey_hex:
            self.revocations[(rev.evm_address, rev.nostr_pubkey, rev.effective_at)] = rev

    def _revs(self) -> list[Revocation]:
        return list(self.revocations.values())

    async def _load_rules(self, relay: RelayClient) -> None:
        missing = [p for p in self.providers if p not in self.rules]
        if not missing:
            return
        newest: dict[str, tuple[int, NostrEvent]] = {}
        for ev in await relay.query([{"kinds": [RULESET_KIND], "authors": missing}]):
            if ev.pubkey in missing and (ev.pubkey not in newest or ev.created_at > newest[ev.pubkey][0]):
                newest[ev.pubkey] = (ev.created_at, ev)
        for provider in missing:
            if provider not in newest:
                raise FollowerError(
                    f"provider {provider[:12]}.. has no rule set on {self.relay_url}; "
                    "check the pubkey, or ask the provider to run `sa provider register`"
                )
            try:
                self.rules[provider] = validate_ruleset_event(newest[provider][1], int(self.clock()), self._revs())
            except PowerError as exc:
                raise FollowerError(f"rule set of provider {provider[:12]}.. is invalid: {exc}") from exc

    # ------------------------------------------------------------------ ingest
    async def ingest(self, ev: NostrEvent, source: str = "relay") -> LedgerRow | None:
        now_ms = int(self.clock() * 1000)
        if ev.kind == ANCHOR_KIND:
            self._on_attestation(ev)
            return None
        if ev.kind == REVOCATION_KIND:
            self._on_revocation(ev)
            return None
        if ev.kind == COMMITMENT_KIND:
            return await self._on_commitment(ev, now_ms, source)
        if ev.kind == SIGNAL_KIND:
            return await self._on_signal(ev, now_ms, source)
        return None

    async def ingest_text(
        self, text: str, provider: str | None = None, rules: ProviderRuleSet | None = None
    ) -> LedgerRow:
        """Off-grid signal (SEP-3.29): the same checks as a relayed one. ``provider`` pins the expected key."""
        try:
            ev = event_from_json(text)
        except NostrError as exc:
            raise FollowerError(f"input is not a Nostr event JSON: {exc}") from exc
        if provider is not None and ev.pubkey != provider:
            raise FollowerError(f"signal is from {ev.pubkey[:12]}.., not the pinned provider {provider[:12]}..")
        if rules is not None:
            self.rules[rules.provider] = rules
        if ev.kind != SIGNAL_KIND:
            raise FollowerError(f"event has kind {ev.kind}; a signal file holds a kind {SIGNAL_KIND} event")
        row = await self._on_signal(ev, int(self.clock() * 1000), "file")
        assert row is not None
        return row

    def _reject(
        self,
        ev: NostrEvent,
        source: str,
        now_ms: int,
        reason: str,
        *,
        signal: Signal | None = None,
        ref: str | None = None,
    ) -> LedgerRow:
        log.warning("rejected %s from %s: %s", ev.id[:12], ev.pubkey[:12], reason)
        self.ledger.record(
            event_id=ev.id,
            provider=ev.pubkey,
            source=source,
            received_ms=now_ms,
            status="rejected",
            reason=reason,
            strategy_id=signal.strategy_id if signal else None,
            seq=signal.seq if signal else None,
            ref=ref,
            prev=signal.prev if signal else None,
            created_ms=signal.created_ms if signal else None,
            signal_json=signal.model_dump_json() if signal else None,
        )
        row = self.ledger.get(ev.id)
        assert row is not None
        return row

    async def _on_commitment(self, ev: NostrEvent, now_ms: int, source: str) -> LedgerRow | None:
        if source == "relay" and ev.pubkey not in self.providers:
            return None
        try:
            commitment, _addr = verify_commitment_event(ev, now_ms // 1000, self._revs())
        except SignalError as exc:
            log.warning("ignoring commitment %s: %s", ev.id[:12], exc)
            return None
        self._commitments[commitment.digest] = (commitment, ev.id, ev.pubkey)
        waiting = self._waiting.pop(commitment.digest, None)
        if waiting is not None:
            return await self._on_signal(waiting, now_ms, "relay")
        return None

    async def _on_signal(self, ev: NostrEvent, now_ms: int, source: str) -> LedgerRow | None:
        provider = ev.pubkey
        if source == "relay" and provider not in self.providers:
            return None
        known = self.ledger.get(ev.id)
        if known is not None and known.status != "pending":
            return known  # redelivery
        rules = self.rules.get(provider)
        private = rules.private is not None if rules is not None else bool(ev.tag_values("p"))
        expected = rules.sa_address if rules is not None else None
        at = now_ms // 1000
        revs = self._revs()
        try:
            signed = (
                open_private_signal(ev, self.device, at, expected, revs)
                if private
                else verify_signal_event(ev, at, expected, revs)
            )
        except SignalError as exc:
            return self._reject(ev, source, now_ms, f"verification failed: {exc}")
        signal = signed.signal
        if rules is None and source != "file":
            return self._reject(ev, source, now_ms, "no rule set known for this provider", signal=signal)

        age_s = (now_ms - signal.created_ms) / 1000
        limit = (
            self.max_age_s if self.max_age_s is not None else 3 * (rules.timeframe if rules else signal.timeframe) + 30
        )
        if age_s > limit:
            return self._reject(
                ev, source, now_ms, f"stale: signal is {age_s:.0f}s old, limit {limit:.0f}s", signal=signal
            )
        if age_s < -self.skew_s:
            return self._reject(
                ev, source, now_ms, f"from the future: created {-age_s:.1f}s ahead of this clock", signal=signal
            )
        if rules is not None:
            try:
                check_timeframe(rules, signal.timeframe)
            except PowerError as exc:
                return self._reject(ev, source, now_ms, f"timeframe: {exc}", signal=signal)

        ref = ev.id
        if private and source != "file":
            digest = commitment_digest(signal.model_dump_json())
            found = self._commitments.get(digest)
            if found is None:
                self._waiting[digest] = ev
                self.ledger.record(
                    event_id=ev.id,
                    provider=provider,
                    source=source,
                    received_ms=known.received_ms if known else now_ms,
                    status="pending",
                    reason="waiting for the commitment of this private signal",
                    strategy_id=signal.strategy_id,
                    seq=signal.seq,
                    prev=signal.prev,
                    created_ms=signal.created_ms,
                )
                row = self.ledger.get(ev.id)
                assert row is not None
                return row
            commitment, commit_id, committer = found
            if committer != provider or (commitment.strategy_id, commitment.seq, commitment.prev) != (
                signal.strategy_id,
                signal.seq,
                signal.prev,
            ):
                return self._reject(ev, source, now_ms, "commitment does not match this signal's slot", signal=signal)
            ref = commit_id

        verified = _Verified(ev, signal, ref, private, source, known.received_ms if known else now_ms)
        return await self._sequence(verified)

    # ------------------------------------------------------------------ ordering
    # Relays release each signal on its own timer, so two signals created within a millisecond can arrive in
    # either order. A signal that jumps ahead of the chain head waits `reorder_wait_s` for its predecessors.
    async def _sequence(self, v: _Verified) -> LedgerRow:
        key = (v.ev.pubkey, v.signal.strategy_id)
        async with self._lock:
            if self._must_wait(key, v):
                return self._hold(key, v)
            row = await self._execute(v)
            await self._drain(key)
            return row

    def _must_wait(self, key: tuple[str, str], v: _Verified) -> bool:
        if self.reorder_wait_s <= 0 or self.allow_gaps or v.source == "file":
            return False
        head = self.ledger.head(*key)
        return v.signal.seq > 1 if head is None else v.signal.seq > head[0] + 1

    def _hold(self, key: tuple[str, str], v: _Verified) -> LedgerRow:
        self._held.setdefault(key, {})[v.signal.seq] = v
        self.ledger.record(
            event_id=v.ev.id, provider=v.ev.pubkey, source=v.source, received_ms=v.received_ms, status="pending",
            reason=f"out of order: waiting up to {self.reorder_wait_s:g}s for the signals before seq {v.signal.seq}",
            strategy_id=v.signal.strategy_id, seq=v.signal.seq, prev=v.signal.prev, created_ms=v.signal.created_ms,
        )  # fmt: skip
        if key not in self._timers:
            self._timers[key] = asyncio.get_running_loop().call_later(self.reorder_wait_s, self._expire_soon, key)
        row = self.ledger.get(v.ev.id)
        assert row is not None
        return row

    def _expire_soon(self, key: tuple[str, str]) -> None:
        task = asyncio.get_running_loop().create_task(self._expire(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, key: tuple[str, str]) -> None:
        """Run held signals that now follow the head, in order."""
        held = self._held.get(key)
        while held:
            head = self.ledger.head(*key)
            nxt = held.pop(1 if head is None else head[0] + 1, None)
            if nxt is None:
                break
            await self._execute(nxt)
        if held is not None and not held:
            self._held.pop(key, None)
            timer = self._timers.pop(key, None)
            if timer is not None:
                timer.cancel()

    async def _expire(self, key: tuple[str, str]) -> None:
        """The wait is over: whatever is still held is processed in seq order (a gap is then judged as a gap)."""
        async with self._lock:
            self._timers.pop(key, None)
            held = self._held.pop(key, {})
            for seq in sorted(held):
                await self._execute(held[seq])

    async def _execute(self, v: _Verified) -> LedgerRow:
        ev, signal, ref, source = v.ev, v.signal, v.ref, v.source
        provider = ev.pubkey
        verdict = (
            self._chain(provider, signal, ref)
            if not (v.private and source == "file")
            else ("ok", "chain: not checked (private file, no commitment)")
        )
        received_ms = v.received_ms
        if verdict[0] == "duplicate":
            return self.ledger.get(ev.id) or self.ledger.by_ref(ref)[0]
        if verdict[0] == "reject":
            return self._reject(
                ev, source, received_ms, verdict[1], signal=signal, ref=ref if verdict[1].startswith("gap:") else None
            )
        try:
            intent = OrderIntent(
                instrument=signal.instrument,
                side=signal.side,
                action=signal.action,
                size_fraction=signal.size_fraction,
                order_type=signal.order_type,
                limit_price=signal.limit_price,
                stop_price=signal.stop_price,
                timeframe=signal.timeframe,
                ts=signal.created_ms,
            )
        except ValidationError as exc:
            return self._reject(
                ev, source, received_ms, f"signal cannot be executed: {exc.errors()[0]['msg']}", signal=signal
            )
        self.ledger.record(
            event_id=ev.id, provider=provider, source=source, received_ms=received_ms, status="accepted",
            reason=verdict[1], strategy_id=signal.strategy_id, seq=signal.seq, ref=ref, prev=signal.prev,
            created_ms=signal.created_ms, signal_json=signal.model_dump_json(),
        )  # fmt: skip
        self.ledger.set_execution(await self.executor.execute(ev.id, intent, int(self.clock() * 1000)))
        self._anchor_check(ref)
        row = self.ledger.get(ev.id)
        assert row is not None
        return row

    def _chain(self, provider: str, signal: Signal, ref: str) -> tuple[str, str]:
        head = self.ledger.head(provider, signal.strategy_id)
        if head is None:
            return "ok", f"chain baseline at seq {signal.seq}"
        hseq, href = head
        if signal.seq == hseq and ref == href:
            return "duplicate", ""
        if signal.seq <= hseq:
            return "reject", f"chain: seq {signal.seq} does not follow {hseq} (replay or fork)"
        if signal.seq == hseq + 1:
            if signal.prev != href:
                return (
                    "reject",
                    f"chain: seq {signal.seq} prev {str(signal.prev)[:12]}.. is not {href[:12]}.. (fork)",
                )
            return "ok", ""
        msg = f"gap: seq {hseq + 1}..{signal.seq - 1} missing before seq {signal.seq}"
        if self.allow_gaps:
            return "ok", msg + " (allowed)"
        return "reject", msg

    # ------------------------------------------------------------------ anchors
    def _on_attestation(self, ev: NostrEvent) -> None:
        try:
            proof = proof_from_attestation(ev)
        except GovernanceError as exc:
            log.warning("ignoring attestation %s: %s", ev.id[:12], exc)
            return
        self._proofs.setdefault(proof.digest, []).append(proof)
        self._anchor_check(proof.digest)

    def _anchor_check(self, ref: str) -> None:
        rows = self.ledger.by_ref(ref)
        proofs = self._proofs.get(ref)
        if not rows or not proofs:
            return
        bounds, why = verify_proofs(bytes.fromhex(ref), proofs, self.verifiers)
        if not bounds:
            self.ledger.set_anchor(ref, f"invalid: {why[0] if why else 'no verifier'}", None)
            return
        anchored = min(b.not_after for b in bounds)
        created_ms = rows[0].created_ms or 0
        late = anchored > created_ms // 1000 + self.tolerance_s
        self.ledger.set_anchor(ref, "late" if late else "ok", anchored)

    # ------------------------------------------------------------------ summary
    def summary(self) -> dict[str, object]:
        rows = self.ledger.rows()
        return {
            "received": len(rows),
            "accepted": sum(r.status == "accepted" for r in rows),
            "rejected": sum(r.status == "rejected" for r in rows),
            "pending": sum(r.status == "pending" for r in rows),
            "placed": sum(r.placed for r in rows),
            "filled": sum(r.filled for r in rows),
            "anchored_ok": sum(r.anchor_status == "ok" for r in rows),
            "account": self.executor.snapshot(int(self.clock() * 1000)),
        }
