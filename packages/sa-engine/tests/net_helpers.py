"""Builders for the network tests: throwaway identities, rule sets, intents, a price feed. No real wallet."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sa_core.anchors import TimeBound
from sa_core.delegation import sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import AnchorProof, Delegation, PrivateRule, ProviderRuleSet, Tier
from sa_core.power import DEFAULT_TIERS, sign_ruleset
from sa_core.trackrecord import Candle

from sa_engine.intent import OrderIntent
from sa_engine.network.replay import PriceFeed

FAR = 4_000_000_000
INSTRUMENT = "BINANCE:BTCUSDT"


@dataclass
class Identity:
    sa: SAKey = field(default_factory=SAKey.generate)
    device: DeviceKey = field(default_factory=DeviceKey.generate)
    expires_at: int = FAR

    def __post_init__(self) -> None:
        self.delegation: Delegation = sign_delegation(self.sa, self.device.pubkey_hex, self.expires_at)

    @property
    def pubkey(self) -> str:
        return self.device.pubkey_hex

    def rules(self, timeframe: int = 3600, tiers: Sequence[Tier] = DEFAULT_TIERS) -> ProviderRuleSet:
        return sign_ruleset(self.sa, self.pubkey, timeframe, tiers=tiers)

    def private_rules(self, timeframe: int = 3600, min_power: int = 1000) -> ProviderRuleSet:
        return sign_ruleset(self.sa, self.pubkey, timeframe, private=PrivateRule(min_power=min_power))


def intent(
    *,
    side: str = "buy",
    action: str = "open",
    size: float = 0.5,
    order_type: str = "market",
    limit: float | None = None,
    stop: float | None = None,
    timeframe: int = 3600,
    ts: int = 1_700_000_000_000,
    instrument: str = INSTRUMENT,
) -> OrderIntent:
    return OrderIntent.model_validate(
        {
            "instrument": instrument,
            "side": side,
            "action": action,
            "size_fraction": size,
            "order_type": order_type,
            "limit_price": limit,
            "stop_price": stop,
            "timeframe": timeframe,
            "ts": ts,
        }
    )


def candles(prices: Sequence[float], start_ms: int, step_ms: int = 60_000, spread: float = 0.0) -> list[Candle]:
    out = []
    for i, p in enumerate(prices):
        d = Decimal(str(p))
        out.append(
            Candle(
                open_time_ms=start_ms + i * step_ms,
                open=d,
                high=d + Decimal(str(spread)),
                low=d - Decimal(str(spread)),
                close=d,
            )
        )
    return out


def flat_feed(price: float = 50_000.0, start_ms: int = 0, n: int = 10) -> PriceFeed:
    return PriceFeed(candles([price] * n, start_ms))


class FakeStamper:
    """Stands in for TsaStamper: instant, deterministic, optionally failing the first N calls."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.calls: list[list[bytes]] = []
        self.wait = 0.0

    def seconds_until_ready(self) -> float:
        return self.wait

    def stamp_batch(self, digests: Sequence[bytes]) -> list[AnchorProof]:
        from sa_core.anchors import AnchorError

        self.calls.append(list(digests))
        if len(self.calls) <= self.fail_first:
            raise AnchorError("fake TSA is down")
        return [AnchorProof(kind="fake", digest=d.hex(), data="{}") for d in digests]


class FakeAnchor:
    """Verifier for FakeStamper proofs: anchored at a fixed time."""

    kind = "fake"

    def __init__(self, not_after: int | None = None) -> None:
        self.not_after = not_after

    def stamp(self, digest: bytes) -> AnchorProof:
        return AnchorProof(kind=self.kind, digest=digest.hex(), data="{}")

    def verify(self, proof: AnchorProof, digest: bytes) -> TimeBound:
        from sa_core.anchors import AnchorError

        if proof.data != "{}":
            raise AnchorError("fake proof is forged")
        return TimeBound(not_after=self.not_after if self.not_after is not None else int(time.time()), kind=self.kind)
