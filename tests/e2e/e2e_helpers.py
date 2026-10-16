"""Read-side helpers for the e2e tests: query the real relay like any other client."""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from sa_core.anchors import verifier_map
from sa_core.bundle import export_bundle
from sa_core.delegation import delegation_tag
from sa_core.governance import proof_from_attestation
from sa_core.keys import DeviceKey
from sa_core.models import (
    ANCHOR_KIND,
    COMMITMENT_KIND,
    SIGNAL_KIND,
    Bundle,
    Delegation,
    NostrEvent,
    Reveal,
)
from sa_core.nostr import RelayClient
from sa_core.trackrecord import Candle
from sa_core.tsa import TrustStore, TsaAnchor
from sa_engine.network.replay import ReplaySpec, load_replay_candles

STEP = Decimal("0.00001")


def query(
    url: str,
    filters: Sequence[dict[str, Any]],
    device: DeviceKey | None = None,
    delegation: Delegation | None = None,
) -> list[NostrEvent]:
    async def go() -> list[NostrEvent]:
        tags = [delegation_tag(delegation)] if delegation else []
        async with RelayClient(url, auth_key=device, auth_tags=tags) as relay:
            if device is not None:
                await relay.authenticate()
            return await relay.query(list(filters), timeout=20)

    return asyncio.run(go())


def publish(url: str, event: NostrEvent) -> tuple[bool, str]:
    async def go() -> tuple[bool, str]:
        async with RelayClient(url) as relay:
            res = await relay.publish(event)
            return res.accepted, res.message

    return asyncio.run(go())


def replay_candles(spec: ReplaySpec, data_dir: Path) -> dict[str, list[Candle]]:
    """The replay's candles on the wall-clock axis, with a tail so late-anchored fills have prices."""
    return {spec.instrument: load_replay_candles(spec, data_dir=data_dir)}


def verifiers_for(trust_dir: Path) -> dict[str, Any]:
    return verifier_map(TsaAnchor(TrustStore.load(trust_dir)))


def bundle_from_relay(
    url: str, provider: str, exported_at: int, reveals: Sequence[Reveal] = ()
) -> Bundle:
    """What any third party can rebuild from a relay alone: the provider's chain events and the proofs."""
    events = query(url, [{"kinds": [SIGNAL_KIND, COMMITMENT_KIND], "authors": [provider]}])
    attestations = query(url, [{"kinds": [ANCHOR_KIND], "authors": [provider]}])
    return export_bundle(
        provider,
        events,
        [proof_from_attestation(a) for a in attestations],
        reveals,
        exported_at=exported_at,
    )


def floor_step(value: Decimal) -> Decimal:
    return (value.quantize(Decimal("1e-10")) / STEP).to_integral_value(rounding=ROUND_DOWN) * STEP


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def describe_ms(values: Sequence[float]) -> str:
    return f"n={len(values)} min={min(values):.1f} p50={statistics.median(values):.1f} p95={percentile(values, 0.95):.1f} max={max(values):.1f} ms"
