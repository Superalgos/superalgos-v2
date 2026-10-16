"""Test builders: throwaway keys, signal chains, candles. No real wallet ever."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sa_core.delegation import sign_delegation
from sa_core.keys import DeviceKey, SAKey
from sa_core.models import (
    Bundle,
    Delegation,
    NostrEvent,
    Reveal,
    Signal,
)
from sa_core.signals import build_commitment_event, build_signal_event
from sa_core.trackrecord import Candle

T0_MS = 1_700_000_040_000  # a whole minute
MINUTE = 60_000
FAR = 4_000_000_000  # unix seconds, far future


@dataclass
class Provider:
    """A provider with a throwaway SA key, a device key, and a growing signal chain."""

    strategy_id: str = "s1"
    sa: SAKey = field(default_factory=SAKey.generate)
    device: DeviceKey = field(default_factory=DeviceKey.generate)
    expires_at: int = FAR
    events: list[NostrEvent] = field(default_factory=list)
    reveals: list[Reveal] = field(default_factory=list)
    _seq: int = 0
    _prev: str | None = None

    def __post_init__(self) -> None:
        self.delegation: Delegation = sign_delegation(
            self.sa, self.device.pubkey_hex, self.expires_at
        )

    @property
    def pubkey(self) -> str:
        return self.device.pubkey_hex

    def next_signal(
        self,
        side: str = "buy",
        action: str = "open",
        size: float = 0.5,
        at_ms: int = T0_MS,
        instrument: str = "BINANCE:BTCUSDT",
        order_type: str = "market",
        limit_price: float | None = None,
        timeframe: int = 60,
        stop_price: float | None = None,
    ) -> Signal:
        return Signal.model_validate(
            {
                "strategy_id": self.strategy_id,
                "seq": self._seq + 1,
                "prev": self._prev,
                "instrument": instrument,
                "side": side,
                "action": action,
                "size_fraction": size,
                "order_type": order_type,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "timeframe": timeframe,
                "created_ms": at_ms,
            }
        )

    def publish(self, **kw: object) -> NostrEvent:
        """Public signal appended to the chain."""
        sig = self.next_signal(**kw)  # type: ignore[arg-type]
        ev = build_signal_event(self.device, sig, self.delegation)
        self._seq, self._prev = sig.seq, ev.id
        self.events.append(ev)
        return ev

    def commit(self, reveal: bool = True, **kw: object) -> NostrEvent:
        """Private signal: only the commitment is public; optionally reveal the plaintext."""
        sig = self.next_signal(**kw)  # type: ignore[arg-type]
        ev = build_commitment_event(
            self.device, sig, self.delegation, created_at=sig.created_ms // 1000
        )
        self._seq, self._prev = sig.seq, ev.id
        self.events.append(ev)
        if reveal:
            self.reveals.append(Reveal(commitment_id=ev.id, plaintext=sig.model_dump_json()))
        return ev

    def bundle(self, proofs: tuple = ()) -> Bundle:  # type: ignore[type-arg]
        from sa_core.bundle import export_bundle

        return export_bundle(self.pubkey, self.events, proofs, self.reveals, exported_at=1)


def anchor_all(events: list[NostrEvent], delay_s: int = 1) -> dict[str, int | None]:
    """Anchored time = created_at + delay: a stand-in for a verified proof."""
    return {e.id: e.created_at + delay_s for e in events}


def candle(i: int, o: str, h: str, low: str, c: str, start_ms: int = T0_MS) -> Candle:
    return Candle(
        open_time_ms=start_ms + i * MINUTE,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
    )


# Fixture market for the hand-computed track record (see tests/test_trackrecord.py).
FIXTURE_CANDLES = [
    candle(0, "100", "101", "99", "100"),
    candle(1, "100", "111", "99", "110"),
    candle(2, "110", "111", "104", "105"),
    candle(3, "105", "106", "94", "95"),
    candle(4, "95", "96", "94", "96"),
    candle(5, "96", "121", "95", "120"),
    candle(6, "120", "121", "119", "120"),
]
