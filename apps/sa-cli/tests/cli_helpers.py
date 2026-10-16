"""Strategy file, intents and fakes shared by the CLI tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from sa_core.anchors import AnchorError, TimeBound
from sa_core.models import AnchorProof
from sa_engine.intent import OrderIntent

STRATEGY = {
    "schema_version": 2,
    "name": "cli-test",
    "timeframe": "1h",
    "stages": {
        "trigger": {"on": {"any": [{"name": "always", "all": ["true"]}]}},
        "open": {
            "when": {"any": [{"name": "up", "all": ["cross_above(ema(close, 2), ema(close, 5))"]}]},
            "orders": [{"id": "e", "type": "market", "size_fraction": 0.5}],
        },
        "close": {
            "when": {
                "any": [{"name": "down", "all": ["cross_below(ema(close, 2), ema(close, 5))"]}]
            }
        },
    },
}


def strategy_file(directory: Path, doc: dict[str, object] | None = None) -> Path:
    path = directory / "strategy.json"
    path.write_text(json.dumps(doc or STRATEGY))
    return path


def intents(hist0_ms: int, n: int = 3, step_ms: int = 60_000) -> list[OrderIntent]:
    out = []
    for i in range(n):
        buy = i % 2 == 0
        out.append(
            OrderIntent(
                instrument="BINANCE:BTCUSDT", side="buy" if buy else "sell", action="open" if buy else "close",
                size_fraction=0.5 if buy else 1.0, order_type="market", timeframe=3600, ts=hist0_ms + i * step_ms,
            )
        )  # fmt: skip
    return out


class FakeStamper:
    def __init__(self) -> None:
        self.calls = 0

    def seconds_until_ready(self) -> float:
        return 0.0

    def stamp_batch(self, digests: Sequence[bytes]) -> list[AnchorProof]:
        self.calls += 1
        return [AnchorProof(kind="fake", digest=d.hex(), data="{}") for d in digests]


class FakeSettings:
    def __init__(self) -> None:
        self.fake = FakeStamper()

    def stamper(self) -> FakeStamper:
        return self.fake

    def verifiers(self) -> dict[str, object]:
        return {"fake": FakeAnchor()}


class FakeAnchor:
    kind = "fake"

    def verify(self, proof: AnchorProof, digest: bytes) -> TimeBound:
        if proof.data != "{}":
            raise AnchorError("forged")
        return TimeBound(not_after=0, kind="fake")
