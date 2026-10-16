"""Provider bot: turns a strategy's order intents into signed, anchored, published signals.

Per intent it builds one kind-9317 signal (public) or, for a private provider, a kind-9318 commitment plus
one NIP-44 ciphertext signal per entitled follower. Every event is journaled (fsync) before it is sent, so
the seq/prev chain survives a restart; every chain event is also handed to a TSA batch and its proof is
published as a kind-1040 attestation. The bot never needs the SA key: it holds a device key and a delegation.

Fail closed (SEP-3.5): ``build_events`` raises when a signal cannot be built or journaled; the compiled
strategy's intent hook then refuses the order.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sa_core.anchors import AnchorError
from sa_core.bundle import export_bundle
from sa_core.delegation import delegation_tag
from sa_core.governance import build_anchor_attestation
from sa_core.keys import DeviceKey
from sa_core.models import AnchorProof, Bundle, Delegation, NostrEvent, ProviderRuleSet, Reveal, Signal
from sa_core.nostr import NostrError, PublishResult, RelayClient
from sa_core.power import ruleset_event
from sa_core.signals import build_commitment_event, build_private_signal_event, build_signal_event
from websockets.exceptions import WebSocketException

from sa_engine.intent import OrderIntent
from sa_engine.network.entitlement import Entitled
from sa_engine.network.journal import JournalEntry, ProviderStore
from sa_engine.network.replay import ReplaySpec

log = logging.getLogger("sa.provider")


class ProviderError(Exception):
    """The provider cannot sign or publish. The message says what to change."""


class EntitlementSource(Protocol):
    async def refresh(self) -> list[Entitled]: ...


class Stamper(Protocol):
    """What the bot needs from ``sa_core.tsa.TsaStamper`` (or any batch anchor)."""

    def seconds_until_ready(self) -> float: ...
    def stamp_batch(self, digests: Sequence[bytes]) -> list[AnchorProof]: ...


@dataclass
class ProviderStats:
    signals: int = 0
    sent: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (event id, relay message)
    anchor_errors: list[str] = field(default_factory=list)
    anchored: int = 0


def replay_run_id(strategy_json: str, spec: ReplaySpec) -> str:
    """Identity of a replay independent of when it started on the wall clock."""
    material = f"{strategy_json}|{spec.instrument}|{spec.start}|{spec.end}|{spec.speed}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class ProviderBot:
    def __init__(
        self,
        *,
        device: DeviceKey,
        delegation: Delegation,
        rules: ProviderRuleSet,
        strategy_id: str,
        store: ProviderStore,
        relay_url: str,
        entitlements: EntitlementSource | None = None,
        stamper: Stamper | None = None,
        clock: Callable[[], float] = time.time,
        relay_factory: Callable[[], RelayClient] | None = None,
        publish_timeout: float = 10.0,
        entitlement_refresh_s: float = 5.0,
        retry_backoff_s: float = 0.5,
    ) -> None:
        if rules.provider != device.pubkey_hex:
            raise ProviderError(
                f"rule set is for provider {rules.provider[:12]}.. but this device key is {device.pubkey_hex[:12]}..; "
                "register with the device key you run with"
            )
        if delegation.nostr_pubkey != device.pubkey_hex:
            raise ProviderError(
                f"delegation names device key {delegation.nostr_pubkey[:12]}.., not {device.pubkey_hex[:12]}..; "
                "create a delegation for this key"
            )
        if rules.private is not None and entitlements is None:
            raise ProviderError("a private provider needs an entitlement source (relay allocations + balances)")
        self.device, self.delegation, self.rules = device, delegation, rules
        self.strategy_id, self.store, self.relay_url = strategy_id, store, relay_url
        self.entitlements, self.stamper, self.clock = entitlements, stamper, clock
        self.stats = ProviderStats()
        self._relay_factory = relay_factory or (
            lambda: RelayClient(relay_url, auth_key=device, auth_tags=[delegation_tag(delegation)])
        )
        self._publish_timeout = publish_timeout
        self._refresh_s = entitlement_refresh_s
        self._backoff_s = retry_backoff_s
        self._relay: RelayClient | None = None
        self._outbox: asyncio.Queue[NostrEvent | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._recipients: list[Entitled] = []
        self._lock = threading.Lock()
        self._to_anchor: list[bytes] = []
        self._anchoring = False

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> ProviderBot:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._outbox = asyncio.Queue()
        await self._connect()
        if self.rules.private is not None:
            await self.refresh_recipients()
        for ev in self.store.unacked():  # signed before a crash or a rejection, never accepted
            self._outbox.put_nowait(ev)
        with self._lock:
            self._to_anchor.extend(bytes.fromhex(r) for r in self.store.unanchored_refs())
        self._tasks = [asyncio.create_task(self._sender())]
        if self.stamper is not None:
            self._tasks.append(asyncio.create_task(self._anchor_loop()))
        if self.rules.private is not None:
            self._tasks.append(asyncio.create_task(self._entitlement_loop()))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        if self._relay is not None:
            with contextlib.suppress(Exception):
                await self._relay.close()
            self._relay = None

    async def _connect(self) -> RelayClient:
        relay = self._relay_factory()
        await relay.connect()
        self._relay = relay
        return relay

    # ------------------------------------------------------------------ registration
    async def publish_ruleset(self) -> PublishResult:
        """Kind 37318 for this provider (the rule set was signed by the SA key beforehand)."""
        if self._relay is None:
            raise ProviderError("start() the bot before publishing")
        ev = ruleset_event(self.device, self.rules, self.delegation)
        res = await self._relay.publish(ev, self._publish_timeout)
        if not res.accepted:
            raise ProviderError(f"relay {self.relay_url} refused the rule set: {res.message}")
        return res

    # ------------------------------------------------------------------ signing
    def build_events(
        self,
        intent: OrderIntent,
        *,
        now_ms: int | None = None,
        recipients: Sequence[Entitled] | None = None,
        run: str | None = None,
        intent_index: int | None = None,
    ) -> list[NostrEvent]:
        """Sign and journal the events for one intent. Raises ``ProviderError``; the caller must not
        let the order proceed when it does. Does not touch the network."""
        if intent.timeframe != self.rules.timeframe:
            raise ProviderError(
                f"the strategy emits timeframe {intent.timeframe}s but the registered rule set binds "
                f"{self.rules.timeframe}s (SEP-3.40); register again with the strategy's timeframe"
            )
        created_ms = int(self.clock() * 1000) if now_ms is None else now_ms
        with self._lock:
            head = self.store.head(self.strategy_id)
            seq, prev = (1, None) if head is None else (head.seq + 1, head.ref)
            try:
                signal = Signal(
                    strategy_id=self.strategy_id,
                    seq=seq,
                    prev=prev,
                    instrument=intent.instrument,
                    side=intent.side,
                    action=intent.action,
                    size_fraction=intent.size_fraction,
                    order_type=intent.order_type,
                    limit_price=intent.limit_price,
                    stop_price=intent.stop_price,
                    timeframe=self.rules.timeframe,
                    created_ms=created_ms,
                )
            except ValueError as exc:
                raise ProviderError(f"intent {intent!r} is not a valid signal: {exc}") from exc
            created_at = created_ms // 1000
            entries: list[JournalEntry] = []
            if self.rules.private is None:
                ev = build_signal_event(self.device, signal, self.delegation, created_at)
                entries.append(
                    JournalEntry(
                        event=ev, strategy_id=self.strategy_id, seq=seq, ref=ev.id, run=run, intent_index=intent_index
                    )
                )
            else:
                commit = build_commitment_event(self.device, signal, self.delegation, created_at)
                entries.append(
                    JournalEntry(
                        event=commit,
                        strategy_id=self.strategy_id,
                        seq=seq,
                        ref=commit.id,
                        plaintext=signal.model_dump_json(),
                        run=run,
                        intent_index=intent_index,
                    )
                )
                for r in self._recipients if recipients is None else recipients:
                    cipher = build_private_signal_event(self.device, signal, self.delegation, r.pubkey, created_at)
                    entries.append(JournalEntry(event=cipher, run=run, intent_index=intent_index))
            try:
                self.store.append(*entries)
            except OSError as exc:
                raise ProviderError(
                    f"cannot journal the signal in {self.store.dir}: {exc}; free disk space or fix permissions"
                ) from exc
            self._to_anchor.append(bytes.fromhex(entries[0].event.id))
        self.stats.signals += 1
        return [e.event for e in entries]

    def enqueue(self, events: Sequence[NostrEvent]) -> None:
        """Thread-safe: hand signed events to the sender."""
        if self._loop is None or self._outbox is None:
            raise ProviderError("start() the bot before publishing")
        outbox = self._outbox
        for ev in events:
            if threading.get_ident() == self._loop_thread:
                outbox.put_nowait(ev)  # on the loop: counted by drain() immediately
            else:
                self._loop.call_soon_threadsafe(outbox.put_nowait, ev)

    def live_hook(self) -> Callable[[OrderIntent], None]:
        """The strategy's ``intent_hook`` for live paper mode: sign now (raises = no order), send async."""

        def hook(intent: OrderIntent) -> None:
            self.enqueue(self.build_events(intent))

        return hook

    # ------------------------------------------------------------------ replay
    async def run_replay(self, intents: Sequence[OrderIntent], spec: ReplaySpec, run_id: str) -> ReplaySpec:
        """Publish ``intents`` at their replayed wall-clock times. Returns the clock actually used.

        After a restart the intents already signed are skipped and the clock is re-anchored so the next one
        is due now (a restarted replay keeps its chain intact; its timing is no longer one single clock).
        """
        done = self.store.progress(run_id)
        effective = spec
        if 0 < done < len(intents):
            hist_next = intents[done].ts
            shifted = int(self.clock() * 1000) - round((hist_next - spec.clock.hist0_ms) / spec.speed)
            effective = spec.model_copy(update={"wall0_ms": shifted})
            log.info("resuming replay %s at intent %d of %d", run_id, done, len(intents))
        clock = effective.clock
        for i in range(done, len(intents)):
            intent = intents[i]
            await self._sleep_until(clock.to_wall(intent.ts))
            recipients = await self.refresh_recipients() if self.rules.private is not None else None
            self.enqueue(self.build_events(intent, recipients=recipients, run=run_id, intent_index=i))
        return effective

    async def _sleep_until(self, wall_ms: int) -> None:
        delay = wall_ms / 1000 - self.clock()
        if delay > 0:
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ entitlements
    async def refresh_recipients(self) -> list[Entitled]:
        if self.entitlements is None:
            return []
        self._recipients = await self.entitlements.refresh()
        return self._recipients

    async def _entitlement_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_s)
            try:
                await self.refresh_recipients()
            except (NostrError, OSError, TimeoutError, WebSocketException) as exc:
                log.warning("cannot refresh entitlements, keeping the last list: %s", exc)

    # ------------------------------------------------------------------ sending
    async def _sender(self) -> None:
        assert self._outbox is not None
        while True:
            ev = await self._outbox.get()
            try:
                if ev is not None:
                    await self._send(ev)
            except Exception as exc:  # one bad event must not stop the sender; it stays unacked
                log.exception("sending %s failed: %s", ev.id[:12] if ev else "-", exc)
            finally:
                self._outbox.task_done()

    async def _send(self, ev: NostrEvent) -> None:
        for attempt in range(4):
            try:
                relay = self._relay or await self._connect()
                res = await relay.publish(ev, self._publish_timeout)
            except (NostrError, OSError, WebSocketException) as exc:
                log.warning("publish of %s failed (%s), reconnecting", ev.id[:12], exc)
                self._relay = None
                await asyncio.sleep(self._backoff_s * 2**attempt)
                with contextlib.suppress(NostrError, OSError, WebSocketException):
                    await self._connect()
                continue
            if res.accepted or res.message.startswith("duplicate"):
                self.store.mark_acked(ev.id)
                self.stats.sent += 1
                return
            if res.message.startswith("rate-limited"):
                await asyncio.sleep(0.5)
                continue
            log.error("relay refused event %s (kind %d): %s", ev.id[:12], ev.kind, res.message)
            self.stats.rejected.append((ev.id, res.message))
            return
        self.stats.rejected.append((ev.id, f"relay {self.relay_url} unreachable after 4 attempts"))

    # ------------------------------------------------------------------ anchoring
    async def _anchor_loop(self) -> None:
        assert self.stamper is not None
        while True:
            await asyncio.sleep(0.25)
            await self._anchor_once()

    async def _anchor_once(self) -> bool:
        stamper = self.stamper
        assert stamper is not None
        with self._lock:
            if not self._to_anchor or self._anchoring or stamper.seconds_until_ready() > 0:
                return False
            batch, self._to_anchor = self._to_anchor, []
            self._anchoring = True
        try:
            proofs = await asyncio.to_thread(stamper.stamp_batch, batch)
        except AnchorError as exc:
            msg = str(exc)
            log.warning("TSA batch of %d failed, will retry: %s", len(batch), msg)
            self.stats.anchor_errors.append(msg)
            with self._lock:
                self._to_anchor = batch + self._to_anchor
            return False
        finally:
            self._anchoring = False
        for proof in proofs:
            self.store.add_proof(proof)
            att = build_anchor_attestation(self.device, proof)
            self.store.append(JournalEntry(event=att))
            self.enqueue([att])
        self.stats.anchored += len(proofs)
        return True

    async def drain(self, anchor_timeout_s: float = 60.0) -> None:
        """Wait until everything signed is sent and (when a stamper is configured) anchored and sent."""
        if self._outbox is None:
            return
        await self._outbox.join()
        if self.stamper is not None:
            async with asyncio.timeout(anchor_timeout_s):
                while True:
                    with self._lock:
                        busy = bool(self._to_anchor) or self._anchoring
                    if not busy:
                        break
                    await asyncio.sleep(0.25)
            await self._outbox.join()

    # ------------------------------------------------------------------ export (SEP-3.14)
    def export(self, *, reveal: bool = False) -> Bundle:
        """The provider's full signed history. ``reveal`` adds the plaintext of committed signals."""
        return export_store(self.store, self.device.pubkey_hex, reveal=reveal, exported_at=int(self.clock()))


def export_store(store: ProviderStore, provider: str, *, reveal: bool = False, exported_at: int = 0) -> Bundle:
    """Bundle a state directory (no relay, no keys needed): signed events, reveals, anchor proofs."""
    entries = store.entries()
    reveals = [Reveal(commitment_id=e.event.id, plaintext=e.plaintext) for e in entries if reveal and e.plaintext]
    return export_bundle(provider, [e.event for e in entries], store.proofs(), reveals, exported_at=exported_at)
