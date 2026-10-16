"""Paper executors for followers.

* :class:`SimulatedPaperExecutor` is a deterministic spot account (long-only, 0.1% fees) that fills
  against a :class:`~sa_engine.network.replay.PriceFeed`. It sizes every order with the shared
  :func:`sa_engine.sizing.size_intent`, so it follows the same sizing contract as Nautilus-backed strategies.
  Used for replayed demos and tests: no network, no Nautilus node.
* :class:`NautilusPaperExecutor` hands intents to :class:`sa_engine.paper.FollowerExecutor` (live Binance
  data, simulated fills). Nautilus must run in the main thread, so the bot runs in a helper thread.

Neither can reach an exchange order endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from sa_engine.intent import OrderIntent, normalize_instrument
from sa_engine.network.replay import PriceFeed
from sa_engine.sizing import size_intent

DEFAULT_FEE_RATE = Decimal("0.001")


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class Execution:
    """Outcome of handing one received signal to a paper account."""

    ref: str  # id of the signal event
    placed: bool
    filled: bool = False
    reason: str = ""  # why it was not placed, or a note
    qty: str | None = None  # Decimal as text
    price: float | None = None
    fee: float | None = None
    ts_ms: int | None = None
    equity_before: float | None = None
    ref_price: float | None = None


class PaperExecutor(Protocol):
    async def execute(self, ref: str, intent: OrderIntent, now_ms: int) -> Execution: ...
    async def poll(self, now_ms: int) -> list[Execution]: ...
    def snapshot(self, now_ms: int) -> dict[str, Any]: ...


@dataclass
class _Working:
    ref: str
    intent: OrderIntent
    qty: Decimal
    placed_ms: int


@dataclass
class SimulatedPaperExecutor:
    """Spot, long-only account filling market orders at the feed price and limit/stop orders when the
    feed's low/high reaches them. Closing the position cancels the working reduce-only orders."""

    instrument: str
    capital: float
    feed: PriceFeed
    fee_rate: Decimal = DEFAULT_FEE_RATE
    size_increment: Decimal = Decimal("0.00001")
    min_quantity: Decimal = Decimal("0.00001")
    cash: Decimal = field(init=False)
    qty: Decimal = field(init=False, default=Decimal(0))
    working: list[_Working] = field(init=False, default_factory=list)
    fills: list[Execution] = field(init=False, default_factory=list)
    _last_poll_ms: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ValueError(f"capital must be positive, got {self.capital}")
        self.instrument = normalize_instrument(self.instrument)
        self.cash = Decimal(str(self.capital))

    # ------------------------------------------------------------------ account
    def equity(self, now_ms: int) -> float:
        return float(self.cash + self.qty * Decimal(str(self.feed.price_at(now_ms))))

    def snapshot(self, now_ms: int) -> dict[str, Any]:
        return {
            "cash": float(self.cash),
            "position_qty": _text(self.qty),
            "equity": self.equity(now_ms),
            "working_orders": len(self.working),
            "fills": len(self.fills),
        }

    # ------------------------------------------------------------------ execution
    async def execute(self, ref: str, intent: OrderIntent, now_ms: int) -> Execution:
        if intent.instrument != self.instrument:
            return Execution(
                ref, False, reason=f"signal is for {intent.instrument}, this account trades {self.instrument}"
            )
        price = self.feed.price_at(now_ms)
        equity = self.equity(now_ms)
        sized = size_intent(
            intent,
            equity=equity,
            position_qty=float(self.qty),
            ref_price=price,
            size_increment=self.size_increment,
            min_quantity=self.min_quantity,
        )
        if sized.quantity is None:
            return Execution(ref, False, reason=sized.reason, equity_before=equity, ref_price=price)
        quantity = sized.quantity
        if intent.order_type == "market":
            return self._fill(ref, intent, quantity, price, now_ms, equity, price)
        trigger = intent.limit_price if intent.order_type == "limit" else intent.stop_price
        assert trigger is not None  # OrderIntent validates this
        if self._marketable(intent, trigger, price):
            return self._fill(ref, intent, quantity, price, now_ms, equity, price)
        self.working.append(_Working(ref, intent, quantity, now_ms))
        return Execution(ref, True, reason="working order", qty=_text(quantity), equity_before=equity, ref_price=price)

    @staticmethod
    def _marketable(intent: OrderIntent, trigger: float, price: float) -> bool:
        if intent.order_type == "limit":
            return price <= trigger if intent.side == "buy" else price >= trigger
        return price >= trigger if intent.side == "buy" else price <= trigger  # stop

    def _fill(
        self,
        ref: str,
        intent: OrderIntent,
        quantity: Decimal,
        price: float,
        ts_ms: int,
        equity: float,
        ref_price: float,
    ) -> Execution:
        px = Decimal(str(price))
        if intent.side == "buy":
            affordable = ((self.cash / (px * (1 + self.fee_rate))) / self.size_increment).to_integral_value(
                rounding=ROUND_DOWN
            ) * self.size_increment
            quantity = min(quantity, affordable)
            if quantity < self.min_quantity:
                return Execution(
                    ref,
                    False,
                    reason="not enough cash for the minimum quantity",
                    equity_before=equity,
                    ref_price=ref_price,
                )
            fee = quantity * px * self.fee_rate
            self.cash -= quantity * px + fee
            self.qty += quantity
        else:
            quantity = min(quantity, self.qty)
            if quantity < self.min_quantity:
                return Execution(
                    ref, False, reason="nothing to sell: no open position", equity_before=equity, ref_price=ref_price
                )
            fee = quantity * px * self.fee_rate
            self.cash += quantity * px - fee
            self.qty -= quantity
            if self.qty < self.min_quantity:
                self.qty = Decimal(0)
                self.working = [w for w in self.working if w.intent.action != "close"]
        done = Execution(
            ref,
            True,
            True,
            qty=_text(quantity),
            price=price,
            fee=float(fee),
            ts_ms=ts_ms,
            equity_before=equity,
            ref_price=ref_price,
        )
        self.fills.append(done)
        return done

    async def poll(self, now_ms: int) -> list[Execution]:
        """Fill working limit/stop orders that the price has reached since the last poll."""
        out: list[Execution] = []
        since = self._last_poll_ms
        self._last_poll_ms = now_ms
        for w in list(self.working):
            if w not in self.working:
                continue  # cancelled by an earlier fill in this loop
            low, high = self.feed.range_between(max(since, w.placed_ms), now_ms)
            trig = w.intent.limit_price if w.intent.order_type == "limit" else w.intent.stop_price
            assert trig is not None
            hit = {
                ("limit", "buy"): low <= trig,
                ("limit", "sell"): high >= trig,
                ("stop", "buy"): high >= trig,
                ("stop", "sell"): low <= trig,
            }[(w.intent.order_type, w.intent.side)]
            if hit:
                self.working.remove(w)
                res = self._fill(w.ref, w.intent, w.qty, trig, now_ms, self.equity(now_ms), self.feed.price_at(now_ms))
                out.append(res)
        return out


class NautilusPaperExecutor:
    """Adapter to :class:`sa_engine.paper.FollowerExecutor`: submit is thread-safe, fills show in its report."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._n = 0

    async def execute(self, ref: str, intent: OrderIntent, now_ms: int) -> Execution:
        self.inner.submit(intent)
        self._n += 1
        return Execution(
            ref, True, reason="submitted to the Nautilus paper node; fills are in its report", ts_ms=now_ms
        )

    async def poll(self, now_ms: int) -> list[Execution]:
        return []

    def snapshot(self, now_ms: int) -> dict[str, Any]:
        return {"submitted": self._n, "results": len(self.inner.results)}
