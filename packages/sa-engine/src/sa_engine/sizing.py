"""Turn an :class:`OrderIntent` into a base-asset quantity. Pure; shared by compiled strategies and followers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from sa_engine.intent import OrderIntent

_NOISE = Decimal("1e-10")


@dataclass(frozen=True, slots=True)
class SizeResult:
    quantity: Decimal | None
    reason: str = ""  # why quantity is None


def floor_to(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError(f"size increment must be positive, got {increment}")
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def uses_capital_basis(intent: OrderIntent) -> bool:
    """open, and adjust-buys, are sized from capital; closes and adjust-sells from the current position."""
    return intent.action == "open" or (intent.action == "adjust" and intent.side == "buy")


def size_intent(
    intent: OrderIntent,
    *,
    equity: float,
    position_qty: float,
    ref_price: float,
    size_increment: Decimal,
    min_quantity: Decimal,
) -> SizeResult:
    """Quantity for ``intent``: ``size_fraction x equity / price`` to open, ``size_fraction x position`` to close."""
    if intent.side == "sell" and intent.action == "open":
        return SizeResult(None, "short selling is not supported on spot; sell intents must be close/adjust")
    if uses_capital_basis(intent):
        price = intent.limit_price or intent.stop_price or ref_price
        if price <= 0 or equity <= 0:
            return SizeResult(None, f"cannot size an open: price={price}, equity={equity}")
        raw = Decimal(str(intent.size_fraction)) * Decimal(str(equity)) / Decimal(str(price))
    else:
        if position_qty <= 0:
            return SizeResult(None, "nothing to close: no open position in this instrument")
        raw = Decimal(str(intent.size_fraction)) * Decimal(str(position_qty))
        raw = min(raw, Decimal(str(position_qty)))
    qty = floor_to(raw.quantize(_NOISE), size_increment)  # quantize first: 0.0096569999999 must floor to 0.009657
    if qty < min_quantity or qty <= 0:
        return SizeResult(None, f"quantity {qty} is below the minimum {min_quantity}; raise capital or size_fraction")
    return SizeResult(qty)
