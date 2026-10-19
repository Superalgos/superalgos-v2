"""A Binance-spot-like venue for follower tests: the backtest exchange behind a recording proxy.

The simulated exchange matches orders but knows nothing about Binance spot's rules. This proxy sits in front of its
execution client, records every command the exchange receives, and adds the two rules the follower must respect:

* a STOP_MARKET order is denied (Nautilus 1.231's Binance spot client: UNSUPPORTED_ORDER_TYPE), so a stop price can
  never reach the venue unnoticed;
* a resting sell locks the base asset: a MARKET or LIMIT sell sent while a STOP_LIMIT rests is rejected for
  insufficient balance (``violations`` lists them). Resting means not cancelled yet: a pending cancel still locks.

``hold_cancels`` keeps cancel commands until :meth:`release_cancels`; ``refuse_cancels`` rejects them; ``refuse``
names orders the venue rejects (a callable from the order to a reason, or None).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nautilus_trader.execution.client import ExecutionClient
from nautilus_trader.execution.messages import CancelOrder, SubmitOrder
from nautilus_trader.model.enums import OrderSide, OrderType


class SpotVenue(ExecutionClient):  # type: ignore[misc]  # Nautilus ships no type stubs
    def __init__(
        self,
        real: Any,
        engine: Any,
        *,
        hold_cancels: bool = False,
        refuse_cancels: bool = False,
        refuse: Callable[[Any], str | None] | None = None,
    ) -> None:
        kernel = engine.kernel
        super().__init__(
            client_id=real.id,
            venue=real.venue,
            oms_type=real.oms_type,
            account_type=real.account_type,
            base_currency=real.base_currency,
            msgbus=kernel.msgbus,
            cache=kernel.cache,
            clock=kernel.clock,
        )
        self._set_account_id(real.account_id)
        self.real = real
        self.hold_cancels, self.refuse_cancels, self.refuse = hold_cancels, refuse_cancels, refuse
        self.submitted: list[Any] = []  # every order as the venue received it (a snapshot of type, side, prices)
        self.cancels: list[CancelOrder] = []
        self.violations: list[str] = []
        self._held: list[CancelOrder] = []
        self._at_venue: set[Any] = set()

    @classmethod
    def install(cls, engine: Any, **kw: Any) -> SpotVenue:
        ee = engine.kernel.exec_engine
        (real,) = ee._clients.values()
        proxy = cls(real, engine, **kw)
        ee.deregister_client(real)
        ee.register_client(proxy)
        return proxy

    # -- what the venue holds
    def resting_stop_limits(self) -> list[Any]:
        return [
            o
            for o in self._cache.orders()
            if o.client_order_id in self._at_venue and o.order_type == OrderType.STOP_LIMIT and not o.is_closed
        ]

    # -- lifecycle
    def _start(self) -> None:
        self._set_connected()
        self.real._set_connected()  # the simulated exchange's client is no longer started by the engine

    def _stop(self) -> None:
        self._set_connected(False)

    # -- commands
    def submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        self.submitted.append(
            {
                "cid": order.client_order_id,
                "type": order.order_type,
                "side": order.side,
                "qty": str(order.quantity),
                "trigger": None if not order.has_trigger_price else str(order.trigger_price),
                "price": None if not order.has_price else str(order.price),
            }
        )
        if order.order_type == OrderType.STOP_MARKET:
            self.generate_order_denied(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                "UNSUPPORTED_ORDER_TYPE: STOP_MARKET not supported for SPOT/MARGIN accounts",
                self._clock.timestamp_ns(),
            )
            return
        if order.side == OrderSide.SELL and order.order_type != OrderType.STOP_LIMIT and self.resting_stop_limits():
            self.violations.append(
                f"{order.order_type!r} sell {order.quantity} sent while a stop-limit rests: insufficient balance"
            )
            self.generate_order_rejected(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                "insufficient balance: the base asset is locked by a resting sell",
                self._clock.timestamp_ns(),
            )
            return
        reason = self.refuse(order) if self.refuse is not None else None
        if reason is not None:  # the exchange answers a moment later, not inside the submit call
            self._clock.set_time_alert_ns(
                f"refuse-{order.client_order_id}",
                self._clock.timestamp_ns() + 1_000_000,
                lambda _event, o=order, why=reason: self.generate_order_rejected(
                    o.strategy_id, o.instrument_id, o.client_order_id, why, self._clock.timestamp_ns()
                ),
            )
            return
        if order.order_type == OrderType.STOP_LIMIT and self.resting_stop_limits():
            self.violations.append("a second stop-limit was sent while one still rests")
        self._at_venue.add(order.client_order_id)
        self.real.submit_order(command)

    def cancel_order(self, command: CancelOrder) -> None:
        self.cancels.append(command)
        if self.refuse_cancels:
            self.generate_order_cancel_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                "Unknown order sent",
                self._clock.timestamp_ns(),
            )
        elif self.hold_cancels:
            self._held.append(command)
        else:
            self.real.cancel_order(command)

    def release_cancels(self) -> None:
        held, self._held = self._held, []
        for command in held:
            self.real.cancel_order(command)

    def account_inquiry(self, command: Any) -> None:
        self.real.account_inquiry(command)

    def submit_order_list(self, command: Any) -> None:
        self.real.submit_order_list(command)

    def modify_order(self, command: Any) -> None:
        self.real.modify_order(command)

    def cancel_all_orders(self, command: Any) -> None:
        self.real.cancel_all_orders(command)
