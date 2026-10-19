"""Compile a :class:`StrategyDoc` into a Nautilus ``Strategy``.

``CompiledStrategy`` is the Nautilus glue around :class:`sa_engine.brain.StrategyBrain`: it feeds bars and
fills in, executes the commands that come out, and routes **every** order through
:meth:`CompiledStrategy.on_order_intent` first. If that hook raises, the order is *not* submitted
(fail closed: no executed order without its signal) and the error is logged and counted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution.algorithm import ExecAlgorithm
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import (
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import Order
from nautilus_trader.trading.strategy import Strategy

from sa_engine.brain import Cancel, Command, PositionState, StrategyBrain, Submit
from sa_engine.indicators import OHLCV
from sa_engine.intent import OrderIntent, from_nautilus_id
from sa_engine.schema import StrategyDoc, validate_strategy
from sa_engine.sizing import SizeResult, size_intent

IntentHook = Callable[[OrderIntent], None]


@dataclass(frozen=True, slots=True)
class FillRecord:
    ts: int  # ms
    side: str  # buy | sell
    qty: float
    price: float
    commission: float
    tag: str


class CompiledStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    instrument_id: str
    bar_type: str
    strategy_json: str
    warmup_bars: int = 0  # live/paper: history requested at start to prime indicators
    subscribe_quotes: bool = False  # live/paper: quotes drive the simulated exchange's limit/stop fills


def to_ohlcv(bar: Bar) -> OHLCV:
    return OHLCV(
        ts=bar.ts_event // 1_000_000,
        open=bar.open.as_double(),
        high=bar.high.as_double(),
        low=bar.low.as_double(),
        close=bar.close.as_double(),
        volume=bar.volume.as_double(),
    )


class ExecutionBase(Strategy):
    """Shared order plumbing: equity/position lookups, sizing, order creation and fill bookkeeping."""

    def __init__(self, config: StrategyConfig, instrument_id: str) -> None:
        super().__init__(config)
        self._iid = InstrumentId.from_str(instrument_id)
        self._instrument: Instrument | None = None
        self._tag_by_id: dict[ClientOrderId, str] = {}
        self._id_by_tag: dict[str, ClientOrderId] = {}
        self._last_price = 0.0
        self.fills: list[FillRecord] = []
        self.rejections: list[str] = []
        self.quotes_seen = 0
        self.final_equity: float | None = None
        self.final_working_orders: int = 0  # orders still resting when the node stopped

    # -- overridable events
    def _filled(self, tag: str, price: float, qty: float, ts: int, done: bool) -> None:
        """Called for every fill of an order placed through ``_place``."""

    def _order_ended(self, tag: str, why: str) -> None:
        """Called when an order placed through ``_place`` was cancelled/rejected/denied/expired."""

    # -- helpers
    def _load_instrument(self) -> Instrument | None:
        self._instrument = self.cache.instrument(self._iid)
        if self._instrument is None:
            self.log.error(f"instrument {self._iid} not in the cache; add it to the engine/node before starting")
            self.stop()
        return self._instrument

    def equity(self) -> float:
        assert self._instrument is not None
        money = self.portfolio.equity(self._instrument.id.venue).get(self._instrument.quote_currency)
        return money.as_double() if money is not None else 0.0

    def position(self) -> PositionState:
        positions = self.cache.positions_open(instrument_id=self._iid)
        if not positions:
            return PositionState()
        qty = sum(float(p.quantity) for p in positions if p.is_long)
        avg = float(positions[0].avg_px_open) if qty > 0 else None
        return PositionState(qty, avg)

    def working_orders(self) -> int:
        """Orders this strategy has submitted that are not yet filled, cancelled or rejected."""
        return sum(1 for o in self.cache.orders(instrument_id=self._iid, strategy_id=self.id) if not o.is_closed)

    def _size(self, intent: OrderIntent) -> SizeResult:
        inst = self._instrument
        assert inst is not None
        return size_intent(
            intent,
            equity=self.equity(),
            position_qty=self.position().qty,
            ref_price=self._last_price,
            size_increment=inst.size_increment.as_decimal(),
            min_quantity=inst.min_quantity.as_decimal() if inst.min_quantity is not None else Decimal(0),
        )

    def exec_algorithms(self) -> list[ExecAlgorithm]:
        """Execution algorithms the node must register next to this strategy (none by default)."""
        return []

    def _stop_order(self, side: OrderSide, qty: Quantity, trigger: Price, *, reduce_only: bool) -> Order:
        """A signalled stop. Here it goes to the venue as a stop-market order; followers override it."""
        return self.order_factory.stop_market(self._iid, side, qty, trigger, reduce_only=reduce_only)

    def _submit_sized(self, intent: OrderIntent, tag: str, quantity: Decimal, *, reduce_only: bool) -> None:
        inst = self._instrument
        assert inst is not None
        side = OrderSide.BUY if intent.side == "buy" else OrderSide.SELL
        qty = inst.make_qty(quantity)
        if intent.order_type == "market":
            order = self.order_factory.market(self._iid, side, qty, reduce_only=reduce_only)
        elif intent.order_type == "limit":
            assert intent.limit_price is not None
            order = self.order_factory.limit(
                self._iid, side, qty, inst.make_price(intent.limit_price), reduce_only=reduce_only
            )
        else:
            assert intent.stop_price is not None
            order = self._stop_order(side, qty, inst.make_price(intent.stop_price), reduce_only=reduce_only)
        self._tag_by_id[order.client_order_id] = tag
        self._id_by_tag[tag] = order.client_order_id
        self.submit_order(order)

    def _cancel_tag(self, tag: str) -> bool:
        cid = self._id_by_tag.get(tag)
        order = self.cache.order(cid) if cid is not None else None
        if order is not None and not order.is_closed:
            self.cancel_order(order)
            return True
        return False

    # -- nautilus events
    def on_order_filled(self, event: OrderFilled) -> None:
        tag = self._tag_by_id.get(event.client_order_id)
        if tag is None:
            return
        order = self.cache.order(event.client_order_id)
        done = order is not None and order.is_closed
        price, qty = round(float(event.last_px), 8), float(event.last_qty)
        ts = event.ts_event // 1_000_000
        side = "buy" if event.is_buy else "sell"
        self.fills.append(FillRecord(ts, side, qty, price, event.commission.as_double(), tag))
        self._filled(tag, price, qty, ts, done)

    def _ended(self, client_order_id: ClientOrderId, why: str) -> None:
        tag = self._tag_by_id.get(client_order_id)
        if tag is not None:
            if why:
                self.rejections.append(f"{tag}: {why}")
            self._order_ended(tag, why)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._ended(event.client_order_id, "")

    def on_order_expired(self, event: OrderExpired) -> None:
        self._ended(event.client_order_id, "expired")

    def on_order_rejected(self, event: OrderRejected) -> None:
        self.log.warning(f"order rejected: {event.reason}")
        self._ended(event.client_order_id, f"rejected: {event.reason}")

    def on_order_denied(self, event: OrderDenied) -> None:
        self.log.warning(f"order denied: {event.reason}")
        self._ended(event.client_order_id, f"denied: {event.reason}")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.quotes_seen += 1
        self._last_price = (tick.bid_price.as_double() + tick.ask_price.as_double()) / 2.0
        self._quote(tick)

    def _quote(self, tick: QuoteTick) -> None:
        """Hook for subclasses; called after ``_last_price`` is updated."""


class CompiledStrategy(ExecutionBase):
    def __init__(self, config: CompiledStrategyConfig, intent_hook: IntentHook | None = None) -> None:
        super().__init__(config, config.instrument_id)
        self.doc: StrategyDoc = validate_strategy(config.strategy_json)
        self._cfg = config
        self._bar_type = BarType.from_str(config.bar_type)
        self._std_bar_type = self._bar_type.standard()  # aggregated bars arrive without the '@source' part
        self._intent_hook = intent_hook
        self._brain: StrategyBrain | None = None
        self.intents: list[OrderIntent] = []
        self.hook_errors: list[str] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.bars_seen = 0
        self._exec_bar_type: BarType | None = None  # finer bars that watch stop/target levels (backtests)
        self._exec_ts = 0
        self._held_bar: Bar | None = None

    # ------------------------------------------------------------ the seam
    def on_order_intent(self, intent: OrderIntent) -> None:
        """Single hook every order passes through, before it is submitted. Override or pass ``intent_hook``."""
        self.intents.append(intent)
        if self._intent_hook is not None:
            self._intent_hook(intent)

    # ------------------------------------------------------------ lifecycle
    def on_start(self) -> None:
        instrument = self._load_instrument()
        if instrument is None:
            return
        self._brain = StrategyBrain(
            self.doc,
            from_nautilus_id(str(self._iid)),
            price_precision=instrument.price_precision,
            tick=float(instrument.price_increment),
            dust=float(instrument.size_increment) / 2,
        )
        if self._cfg.warmup_bars > 0:  # pragma: no cover - live/paper only, see tests/test_paper_online.py
            span = timedelta(seconds=self.doc.timeframe_seconds * (self._cfg.warmup_bars + 2))
            self.request_bars(self._bar_type, start=self.clock.utc_now() - span, limit=self._cfg.warmup_bars)
        self.subscribe_bars(self._bar_type)
        if self.doc.needs_exec_bars and not self._cfg.subscribe_quotes and self._std_bar_type != self._one_minute():
            self._exec_bar_type = (
                self._one_minute()
            )  # levels are compared with 1m bars, decisions use the strategy bars
            self.subscribe_bars(self._exec_bar_type)
        if self._cfg.subscribe_quotes:
            self.subscribe_quote_ticks(self._iid)

    def _one_minute(self) -> BarType:
        return BarType.from_str(f"{self._iid}-1-MINUTE-LAST-EXTERNAL")

    def on_stop(self) -> None:
        self.unsubscribe_bars(self._bar_type)
        if self._exec_bar_type is not None:
            self.unsubscribe_bars(self._exec_bar_type)

    # ------------------------------------------------------------ data
    def on_historical_data(self, data: object) -> None:  # pragma: no cover - live/paper warm-up only
        if (
            isinstance(data, Bar)
            and self._brain is not None
            and data.bar_type == self._std_bar_type
            and data.ts_event <= self.clock.timestamp_ns()
        ):
            self._brain.bank.update(to_ohlcv(data))

    def on_bar(self, bar: Bar) -> None:
        if self._brain is None:
            return
        if self._exec_bar_type is not None and bar.bar_type == self._exec_bar_type:
            self._exec_ts = bar.ts_event
            self._execute(self._brain.on_exec_bar(to_ohlcv(bar), self.position()))
            held, self._held_bar = self._held_bar, None
            if held is not None and held.ts_event <= self._exec_ts:
                self._strategy_bar(held)
            elif held is not None:  # pragma: no cover - a strategy bar always closes on a 1m close
                self._held_bar = held
            return
        if bar.bar_type != self._std_bar_type:
            return
        if self._exec_bar_type is not None and bar.ts_event > self._exec_ts:
            self._held_bar = bar  # the 1m bar that closes at the same instant has not arrived yet: it goes first
            return
        self._strategy_bar(bar)

    def _strategy_bar(self, bar: Bar) -> None:
        assert self._brain is not None
        self.bars_seen += 1
        ohlcv = to_ohlcv(bar)
        if not self._cfg.subscribe_quotes:
            self._last_price = ohlcv.close
        equity = self.equity()
        self.equity_curve.append((ohlcv.ts, equity))
        if self._exec_bar_type is None and self.doc.needs_exec_bars and not self._cfg.subscribe_quotes:
            self._execute(self._brain.on_exec_bar(ohlcv, self.position()))  # 1m strategy: its bar is the finest bar
        self._execute(self._brain.on_bar(ohlcv, self.position(), equity))

    def _quote(self, tick: QuoteTick) -> None:
        """Live/paper: the mid price watches the stop and target levels between bars."""
        if self._brain is None or not self.doc.needs_exec_bars:
            return
        mid = self._last_price
        ts = tick.ts_event // 1_000_000
        self._execute(self._brain.on_exec_bar(OHLCV(ts, mid, mid, mid, mid, 0.0), self.position()))

    # ------------------------------------------------------------ brain <-> orders
    def _filled(self, tag: str, price: float, qty: float, ts: int, done: bool) -> None:
        if self._brain is not None:
            self._execute(self._brain.on_fill(tag, price, qty, self.position(), ts, done=done))

    def _order_ended(self, tag: str, why: str) -> None:
        if self._brain is not None:
            self._brain.on_order_end(tag, rejected=bool(why))

    def _execute(self, commands: list[Command]) -> None:
        for cmd in commands:
            if isinstance(cmd, Cancel):
                if not self._cancel_tag(cmd.tag) and self._brain is not None:
                    self._brain.on_order_end(cmd.tag)
            else:
                self._submit(cmd)

    def _submit(self, cmd: Submit) -> None:
        assert self._instrument is not None and self._brain is not None
        intent = cmd.intent
        sized = self._size(intent)
        if sized.quantity is None:
            self.log.warning(f"{cmd.tag}: not submitted: {sized.reason}")
            self.rejections.append(f"{cmd.tag}: {sized.reason}")
            self._brain.on_order_end(cmd.tag, rejected=True)
            return
        try:
            self.on_order_intent(intent)
        except Exception as e:  # fail closed: no order without its signal
            msg = f"{cmd.tag}: on_order_intent raised {type(e).__name__}: {e}; order NOT submitted"
            self.log.error(msg)
            self.hook_errors.append(msg)
            self._brain.on_order_end(cmd.tag, rejected=True)
            return
        self._submit_sized(intent, cmd.tag, sized.quantity, reduce_only=cmd.reduce_only)


def compile_strategy(
    doc: StrategyDoc | dict[str, object] | str,
    instrument_id: str,
    bar_type: str,
    *,
    intent_hook: IntentHook | None = None,
    warmup_bars: int = 0,
    subscribe_quotes: bool = False,
) -> CompiledStrategy:
    """Validate ``doc`` and build the Nautilus strategy. ``instrument_id`` is Nautilus style (``BTCUSDT.BINANCE``)."""
    parsed = doc if isinstance(doc, StrategyDoc) else validate_strategy(doc)
    cfg = CompiledStrategyConfig(
        instrument_id=instrument_id,
        bar_type=bar_type,
        strategy_json=parsed.model_dump_json(),
        warmup_bars=warmup_bars,
        subscribe_quotes=subscribe_quotes,
        order_id_tag="001",
    )
    return CompiledStrategy(cfg, intent_hook)
