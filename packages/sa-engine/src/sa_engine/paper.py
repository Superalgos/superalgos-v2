"""Paper trading: live Binance public market data, simulated fills, no API keys, no real orders.

Wiring (NautilusTrader 1.231): a ``TradingNode`` with the Binance *data* client (public streams, no
credentials) and Nautilus' ``SandboxExecutionClient`` (a ``SimulatedExchange`` matching engine fed by the live
quotes/bars). Nothing here can reach an order endpoint: no execution client for Binance is ever created.

* :func:`run_paper` runs a strategy document live.
* :class:`FollowerExecutor` executes external :class:`OrderIntent` s (received signals) as paper orders.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.adapters.binance import BinanceAccountType, BinanceDataClientConfig, BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance.config import BinanceInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.component import TimeEvent
from nautilus_trader.config import ActorConfig, LiveExecEngineConfig, LoggingConfig, StrategyConfig, TradingNodeConfig
from nautilus_trader.execution.algorithm import ExecAlgorithm
from nautilus_trader.execution.config import ExecAlgorithmConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OrderSide, TriggerType
from nautilus_trader.model.events import OrderAccepted, OrderCancelRejected
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, ExecAlgorithmId, InstrumentId, TraderId
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import Order
from pydantic import BaseModel, ConfigDict

from sa_engine.compile import ExecutionBase, IntentHook, compile_strategy
from sa_engine.intent import OrderIntent, from_nautilus_id, normalize_instrument, to_nautilus_id
from sa_engine.node import IntentRunner, NodeSession
from sa_engine.protection import DISASTER_LIMIT_OFFSET, check_disaster_stop_pct
from sa_engine.report import Fill, Trade, match_trades
from sa_engine.schema import StrategyDoc, validate_strategy
from sa_engine.sizing import floor_to, uses_capital_basis

CLOSE_WAIT_TIMEOUT_S = 30.0  # longest a close waits for its entry order before it is dropped
DEFAULT_FEE_RATE = Decimal("0.001")  # Binance spot standard taker/maker; public (keyless) instrument data reports 0


class DisasterStop(BaseModel):
    """One disaster stop the follower rested at the exchange: its venue order id and how it ended."""

    model_config = ConfigDict(frozen=True)

    order_id: str | None  # the exchange's id (None until the exchange accepted it)
    qty: str
    trigger_price: str
    limit_price: str
    state: Literal["working", "cancel_requested", "cancelled", "filled", "rejected"]
    detail: str = ""  # why it was cancelled or refused, or that its cancel failed


class PaperReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: str
    duration_s: float
    starting_capital: float
    final_equity: float
    quotes_seen: int
    bars_seen: int
    intents: int
    orders_filled: int
    fees: float
    open_qty: float
    working_orders: int = 0  # resting orders left at the end of the run
    trades: list[Trade]
    rejections: list[str]
    disaster_stops: list[DisasterStop] = []  # SEP-14.5: the follower's disaster stops and how each ended


class _FeeConfig(ActorConfig, frozen=True, kw_only=True):
    instrument_id: str
    fee_rate: str


class _SandboxFees(Actor):  # pragma: no cover - needs the live node; tests/test_paper_online.py
    """Starts before strategies and gives the keyless (fee-less) live instrument Binance's standard 0.1% fees."""

    def __init__(self, config: _FeeConfig, session: PaperSession) -> None:
        super().__init__(config)
        self._cfg = config
        self._session = session

    def on_stop(self) -> None:
        pass

    def on_start(self) -> None:
        iid = InstrumentId.from_str(self._cfg.instrument_id)
        inst = self.cache.instrument(iid)
        if not isinstance(inst, CurrencyPair):
            self.log.error(f"{iid} is not a spot CurrencyPair in the cache; fees not applied")
            return
        data = CurrencyPair.to_dict(inst)
        data["maker_fee"] = data["taker_fee"] = self._cfg.fee_rate
        patched = CurrencyPair.from_dict(data)
        self.cache.add_instrument(patched)
        client = self._session.sandbox_client()
        if client is not None:
            client.exchange.update_instrument(patched)


class PaperSession(NodeSession):
    """One live paper-trading node around a single strategy: public data, sandbox fills, no order endpoint."""

    def __init__(
        self,
        strategy: ExecutionBase,
        instrument: str,
        capital: float,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
        log_level: str = "ERROR",
    ) -> None:
        super().__init__(strategy, instrument, log_level)
        self.capital = capital
        self.fee_rate = fee_rate

    def sandbox_client(self) -> Any:  # pragma: no cover - needs the live node
        assert self._node is not None
        return self._node.kernel.exec_engine._clients.get(ClientId(self.venue))

    def node_config(self) -> TradingNodeConfig:
        venue = self.venue
        iid = InstrumentId.from_str(to_nautilus_id(self.instrument))
        return TradingNodeConfig(
            trader_id=TraderId("PAPER-001"),
            logging=LoggingConfig(log_level=self.log_level, use_pyo3=False),
            exec_engine=LiveExecEngineConfig(reconciliation=False),
            data_clients={
                venue: BinanceDataClientConfig(
                    account_type=BinanceAccountType.SPOT,
                    api_key=None,  # public market data only
                    api_secret=None,
                    instrument_provider=BinanceInstrumentProviderConfig(load_ids=frozenset({iid})),
                )
            },
            exec_clients={
                venue: SandboxExecutionClientConfig(
                    venue=venue,
                    starting_balances=[f"{self.capital} USDT"],
                    account_type="MARGIN",
                    oms_type="NETTING",
                    base_currency=None,
                )
            },
        )

    def add_factories(self, node: TradingNode) -> None:
        node.add_data_client_factory(self.venue, BinanceLiveDataClientFactory)
        node.add_exec_client_factory(self.venue, SandboxLiveExecClientFactory)

    def prepare(self, node: TradingNode) -> None:  # pragma: no cover - needs the live node
        fee_cfg = _FeeConfig(instrument_id=to_nautilus_id(self.instrument), fee_rate=str(self.fee_rate))
        node.trader.add_actor(_SandboxFees(fee_cfg, self))
        super().prepare(node)


def _binance_timeframe_bar_type(instrument_id: str, timeframe: str) -> str:
    n, unit = int(timeframe[:-1]), {"m": "MINUTE", "h": "HOUR", "d": "DAY"}[timeframe[-1]]
    return f"{instrument_id}-{n}-{unit}-LAST-EXTERNAL"


def _report(
    s: ExecutionBase,
    instrument: str,
    duration_s: float,
    capital: float,
    intents: int,
    bars: int,
    disaster_stops: list[DisasterStop] | None = None,
) -> PaperReport:
    fills = [Fill(f.ts, f.side, f.qty, f.price, f.commission) for f in s.fills]
    trades, open_qty = match_trades(fills)
    return PaperReport(
        instrument=instrument,
        duration_s=duration_s,
        starting_capital=capital,
        final_equity=s.final_equity if s.final_equity is not None else capital,
        quotes_seen=s.quotes_seen,
        bars_seen=bars,
        intents=intents,
        orders_filled=len(fills),
        fees=sum(f.commission for f in fills),
        open_qty=open_qty,
        working_orders=s.final_working_orders,
        trades=trades,
        rejections=list(s.rejections),
        disaster_stops=disaster_stops or [],
    )


def run_paper(
    strategy_doc: StrategyDoc | Mapping[str, Any] | str,
    instrument: str,
    capital: float,
    *,
    duration_s: float | None = None,
    intent_hook: IntentHook | None = None,
    fee_rate: Decimal = DEFAULT_FEE_RATE,
    log_level: str = "ERROR",
) -> PaperReport:
    """Run ``strategy_doc`` on live Binance data with simulated fills until ``duration_s`` (or Ctrl-C)."""
    import time

    doc = (
        strategy_doc
        if isinstance(strategy_doc, StrategyDoc)
        else validate_strategy(strategy_doc if isinstance(strategy_doc, str) else dict(strategy_doc))
    )
    inst = normalize_instrument(instrument)
    if capital <= 0:
        raise ValueError(f"capital must be positive, got {capital}")
    nid = to_nautilus_id(inst)
    strategy = compile_strategy(
        doc,
        nid,
        _binance_timeframe_bar_type(nid, doc.timeframe),
        intent_hook=intent_hook,
        warmup_bars=min(doc.warmup_bars(), 1000),
        subscribe_quotes=True,
    )
    session = PaperSession(strategy, inst, capital, fee_rate=fee_rate, log_level=log_level)
    t0 = time.monotonic()
    session.run(duration_s)
    return _report(strategy, inst, time.monotonic() - t0, capital, len(strategy.intents), strategy.bars_seen)


# ---------------------------------------------------------------------------------------------- follower

# SEP-3.45: a signalled stop never reaches the exchange. Nautilus holds it locally (order emulation) and releases a
# market order when the trade price crosses it. LAST_PRICE, not BID_ASK: a BID_ASK emulation makes the emulator
# subscribe to the order book as well, and the exchange's own stop-loss orders also trigger on the last trade.
STOP_EMULATION = TriggerType.LAST_PRICE
SELL_GATE_ID = "SELL_GATE"

DISASTER_PREFIX = "disaster-"  # tag of the strategy's own protective orders, never a signal id


class FollowerConfig(StrategyConfig, frozen=True, kw_only=True):
    instrument_id: str
    disaster_stop_pct: float  # 0 = no disaster stop; see check_disaster_stop_pct


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent: OrderIntent
    placed: bool
    reason: str = ""  # why it was not placed
    ref: str | None = None  # the caller's id for this intent (a signal event id), if it gave one


@dataclass(slots=True)
class _Disaster:
    tag: str
    cid: ClientOrderId
    qty: Decimal
    trigger: Decimal
    limit: Decimal
    order_id: str | None = None
    state: Literal["working", "cancel_requested", "cancelled", "filled", "rejected"] = "working"
    detail: str = ""
    cancel_failed: bool = False  # the exchange refused a cancel: nothing retries it by itself

    def model(self) -> DisasterStop:
        return DisasterStop(
            order_id=self.order_id,
            qty=format(self.qty.normalize(), "f"),
            trigger_price=format(self.trigger.normalize(), "f"),
            limit_price=format(self.limit.normalize(), "f"),
            state=self.state,
            detail=self.detail,
        )


@dataclass(slots=True)
class _WaitingClose:
    intent: OrderIntent
    ref: str | None
    alert: str  # name of the clock alert that fires the timeout


@dataclass(slots=True)
class _HeldSell:
    """A sell that waits until the disaster stop is off the book: ``send`` runs then, or ``fail`` says why not."""

    send: Callable[[], None]
    fail: Callable[[str], None]


class SellGate(ExecAlgorithm):
    """Receives each released (triggered) stop from the order emulator and lets the strategy decide when it goes.

    The emulator would send a triggered stop's market order straight to the exchange; routing it through this
    algorithm gives the strategy the moment to cancel the disaster stop first (SEP-14.5).
    """

    def __init__(self, strategy: FollowerStrategy) -> None:
        super().__init__(ExecAlgorithmConfig(exec_algorithm_id=SELL_GATE_ID))
        self._strategy = strategy

    def on_order(self, order: Order) -> None:
        self._strategy.release_stop(order, self.submit_order)


class FollowerStrategy(ExecutionBase):
    """Executes external intents. Intents arriving before the first quote wait for it (a market needs a price).

    A close (or adjust-sell) that arrives while an entry order is still in flight waits for it: it is sized
    against the position the entry opens, then placed. If the entry ends without a fill, or ``CLOSE_WAIT_TIMEOUT_S``
    passes, the close is dropped with the reason. When the position goes flat, its working reduce-only orders are
    cancelled.

    A signalled stop is emulated: held in this process, never shown to the exchange, released as a market order.

    With ``disaster_stop_pct > 0`` one stop-limit rests at the exchange at ``avg entry x (1 - pct)`` for the whole
    position, for when this process is not running. It is replaced when the position size changes and cancelled
    when flat. Binance spot locks the base asset of a resting sell, so every sell this strategy sends (a close or
    a triggered stop) is held until the disaster stop's cancel is confirmed.
    """

    def __init__(self, config: FollowerConfig) -> None:
        super().__init__(config, config.instrument_id)
        self._disaster_pct = Decimal(str(check_disaster_stop_pct(config.disaster_stop_pct)))
        self._waiting: list[tuple[OrderIntent, str | None]] = []
        self._n = 0
        self.results: list[ExecutionResult] = []
        self._result_at: dict[str, int] = {}
        self._entries: set[str] = set()  # tags of buy orders submitted and not yet filled/ended
        self._entry_end = ""  # how the last entry ended, for the drop reason
        self._parked: list[_WaitingClose] = []
        self._parked_n = 0
        self._gate = SellGate(self)
        self._disaster: _Disaster | None = None  # the one disaster stop on the book, if any
        self._disaster_log: list[_Disaster] = []
        self._disaster_n = 0
        self._disaster_refused: tuple[Decimal, Decimal] | None = None  # (qty, trigger) the exchange refused
        self._held: list[_HeldSell] = []
        self._stop_intents: dict[str, tuple[OrderIntent, str | None]] = {}
        self._reported: set[str] = set()  # tags whose end is reported by their own path

    def exec_algorithms(self) -> list[ExecAlgorithm]:
        return [self._gate]

    def disaster_stops(self) -> list[DisasterStop]:
        """Every disaster stop this node rested, oldest first, with how each ended."""
        return [d.model() for d in self._disaster_log]

    def on_start(self) -> None:
        if self._load_instrument() is not None:
            self.subscribe_quote_ticks(self._iid)

    def on_stop(self) -> None:
        self._drop_parked("the node stopped while the close waited for its entry order")

    def _quote(self, tick: object) -> None:
        waiting, self._waiting = self._waiting, []
        for intent, ref in waiting:
            self._execute_now(intent, ref)

    def execute(self, intent: OrderIntent, ref: str | None = None) -> None:
        """Must run inside the node's event loop (use :meth:`FollowerExecutor.submit` from other threads).

        ``ref`` names the order in the events subclasses report (default ``follow<n>``).
        """
        if self._last_price <= 0:
            self._waiting.append((intent, ref))
        else:
            self._execute_now(intent, ref)

    def _record(self, result: ExecutionResult, tag: str | None = None) -> None:
        """Keep the outcome of one intent; a later outcome for the same ``tag`` replaces the earlier one."""
        if tag is not None and tag in self._result_at:
            self.results[self._result_at[tag]] = result
            return
        if tag is not None:
            self._result_at[tag] = len(self.results)
        self.results.append(result)

    def _execute_now(self, intent: OrderIntent, ref: str | None) -> None:
        if intent.instrument != from_nautilus_id(str(self._iid)):
            self._record(
                ExecutionResult(
                    intent, False, f"intent is for {intent.instrument}, this executor trades {self._iid}", ref
                )
            )
            return
        if self._entries and not uses_capital_basis(intent) and intent.side == "sell":
            self._park(intent, ref)
            return
        self._place_now(intent, ref)

    def _place_now(self, intent: OrderIntent, ref: str | None) -> None:
        self._n += 1
        tag = ref or f"follow{self._n}"
        sized = self._size(intent)
        if sized.quantity is None:
            self._record(ExecutionResult(intent, False, sized.reason, ref), tag)
            self.log.warning(f"intent not executed: {sized.reason}")
            return
        quantity = sized.quantity

        def send() -> None:
            self._record(ExecutionResult(intent, True, "", ref), tag)  # before the submit: a denial can follow at once
            self._submit_sized(intent, tag, quantity, reduce_only=intent.action == "close")
            if intent.order_type == "stop":
                self._stop_intents[tag] = (intent, ref)
            if intent.side == "buy":
                order = self.cache.order(self._id_by_tag[tag])
                if order is not None and not order.is_closed:  # a synchronous fill or denial is already over
                    self._entries.add(tag)

        def fail(why: str) -> None:
            self._record(ExecutionResult(intent, False, why, ref), tag)
            self.log.warning(f"intent not executed: {why}")

        if intent.side == "sell" and intent.order_type != "stop":  # a stop sells later, when it is released
            self._when_book_clear(send, fail)
        else:
            send()

    # -- emulated stops
    def _stop_order(self, side: OrderSide, qty: Quantity, trigger: Price, *, reduce_only: bool) -> Order:
        return self.order_factory.stop_market(
            self._iid,
            side,
            qty,
            trigger,
            reduce_only=reduce_only,
            emulation_trigger=STOP_EMULATION,
            exec_algorithm_id=ExecAlgorithmId(SELL_GATE_ID),
        )

    def release_stop(self, order: Order, submit: Callable[[Order], None]) -> None:
        """The emulator released a triggered stop as a market order; ``submit`` sends it to the exchange."""
        if order.side == OrderSide.BUY:
            submit(order)
            return
        self._when_book_clear(lambda: submit(order), lambda why: self._drop_released(order, why))

    def _drop_released(self, order: Order, why: str) -> None:
        tag = self._tag_by_id.get(order.client_order_id, str(order.client_order_id))
        self._reported.add(tag)
        known = self._stop_intents.get(tag)
        if known is not None:
            self._record(ExecutionResult(known[0], False, why, known[1]), tag)
        self.log.warning(f"triggered stop not sent: {why}")
        if not order.is_closed:
            self.cancel_order(order)  # a released order no one will send: close it locally

    # -- the disaster stop and the sells that wait for it
    def _when_book_clear(self, send: Callable[[], None], fail: Callable[[str], None]) -> None:
        """Run ``send`` once no disaster stop rests; cancel it first. ``fail`` says why if that is impossible."""
        if self._disaster is None:
            send()
            return
        self._held.append(_HeldSell(send, fail))
        self._cancel_disaster("a sell is about to be sent", force=True)

    def _cancel_disaster(self, why: str, *, force: bool = False) -> None:
        d = self._disaster
        if d is None or d.state == "cancel_requested" or (d.cancel_failed and not force):
            return
        order = self.cache.order(d.cid)
        if order is None or order.is_closed:
            return
        d.state, d.detail, d.cancel_failed = "cancel_requested", why, False
        self.cancel_order(order)

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        d = self._disaster
        if d is None or d.cid != event.client_order_id:
            return
        d.state, d.cancel_failed = "working", True
        d.detail = f"cancel failed: {event.reason}"
        held, self._held = self._held, []
        why = (
            f"disaster stop {d.order_id or d.tag} could not be cancelled ({event.reason}); the sell was not sent: "
            "cancel the stop on the exchange, then send the sell again"
        )
        for h in held:
            h.fail(why)
        self.log.warning(why)

    def _run_held(self) -> None:
        held, self._held = self._held, []
        for h in held:
            if self.position().qty <= 0:
                h.fail("the position is already closed; the sell was not sent")
            else:
                h.send()

    def _venue_sell_in_flight(self) -> bool:
        """A sell (not the disaster stop) is working at the exchange: it holds the base asset a stop would need."""
        return any(
            o.side == OrderSide.SELL
            and not o.is_closed
            and not o.is_emulated
            and not self._is_disaster(self._tag_by_id.get(o.client_order_id, ""))
            for o in self.cache.orders(instrument_id=self._iid, strategy_id=self.id)
        )

    def _reconcile_disaster(self) -> None:
        """Make the book match the position: one stop for all of it, none when flat."""
        if self._disaster_pct <= 0 or self._held:
            return
        inst, d = self._instrument, self._disaster
        assert inst is not None
        held = self.position()
        qty = floor_to(Decimal(str(held.qty)), inst.size_increment.as_decimal())
        if qty < (inst.min_quantity.as_decimal() if inst.min_quantity is not None else Decimal(0)) or qty <= 0:
            self._cancel_disaster("the position is flat")
            return
        trigger = inst.make_price(Decimal(str(held.avg_price)) * (1 - self._disaster_pct / 100))
        if d is not None:
            if (d.qty, d.trigger) != (qty, trigger.as_decimal()):
                self._cancel_disaster(f"the position is now {qty}, replacing the stop")
            return
        if self._venue_sell_in_flight() or self._disaster_refused == (qty, trigger.as_decimal()):
            return
        limit = inst.make_price(trigger.as_decimal() * (1 - DISASTER_LIMIT_OFFSET))
        self._place_disaster(qty, trigger, limit)

    def _place_disaster(self, qty: Decimal, trigger: Price, limit: Price) -> None:
        inst = self._instrument
        assert inst is not None
        self._disaster_n += 1
        tag = f"{DISASTER_PREFIX}{self._disaster_n}"
        order = self.order_factory.stop_limit(
            self._iid, OrderSide.SELL, inst.make_qty(qty), limit, trigger, reduce_only=True
        )
        entry = _Disaster(tag, order.client_order_id, qty, trigger.as_decimal(), limit.as_decimal())
        self._disaster, self._disaster_refused = entry, None
        self._disaster_log.append(entry)
        self._tag_by_id[order.client_order_id] = tag
        self._id_by_tag[tag] = order.client_order_id
        self._reported.add(tag)
        self.submit_order(order)

    def _disaster_gone(self, state: Literal["cancelled", "filled", "rejected"], why: str) -> None:
        d = self._disaster
        assert d is not None
        d.state = state
        if why:
            d.detail = why
        elif state == "cancelled" and not d.detail:
            d.detail = "cancelled"
        if state == "rejected":
            self._disaster_refused = (d.qty, d.trigger)
        self._disaster = None
        self._run_held()
        self._reconcile_disaster()

    def _is_disaster(self, tag: str) -> bool:
        return tag.startswith(DISASTER_PREFIX)

    def _accepted(self, tag: str, venue_id: str, ts_ms: int) -> None:
        """The exchange (or the sandbox) accepted the order."""
        for d in self._disaster_log:
            if d.tag == tag:
                d.order_id = venue_id

    def on_order_accepted(self, event: OrderAccepted) -> None:
        tag = self._tag_by_id.get(event.client_order_id)
        if tag is not None:
            self._accepted(tag, event.venue_order_id.value, event.ts_event // 1_000_000)

    # -- closes that wait for their entry
    def _park(self, intent: OrderIntent, ref: str | None) -> None:
        self._parked_n += 1
        alert = f"close-wait-{self._parked_n}"
        self.clock.set_time_alert(
            alert, self.clock.utc_now() + timedelta(seconds=CLOSE_WAIT_TIMEOUT_S), self._timed_out
        )
        self._parked.append(_WaitingClose(intent, ref, alert))
        self.log.info(f"close waits for entry order(s) {sorted(self._entries)}")

    def _timed_out(self, event: TimeEvent) -> None:
        late = [w for w in self._parked if w.alert == event.name]
        self._parked = [w for w in self._parked if w.alert != event.name]
        for w in late:
            self._drop(
                w,
                f"timeout: the entry order did not fill within {CLOSE_WAIT_TIMEOUT_S:g}s; close dropped",
            )

    def _drop(self, waiting: _WaitingClose, reason: str) -> None:
        self._record(ExecutionResult(waiting.intent, False, reason, waiting.ref))
        self.log.warning(f"intent not executed: {reason}")

    def _drop_parked(self, reason: str) -> None:
        parked, self._parked = self._parked, []
        for w in parked:
            if w.alert in self.clock.timer_names:
                self.clock.cancel_timer(w.alert)
            self._drop(w, reason)

    def _release_parked(self) -> None:
        """Every entry is over: place the waiting closes against the position they left, or drop them."""
        parked, self._parked = self._parked, []
        for w in parked:
            if w.alert in self.clock.timer_names:
                self.clock.cancel_timer(w.alert)
            if self.position().qty > 0:
                self._place_now(w.intent, w.ref)
            else:
                self._drop(w, f"the entry order ended without a fill ({self._entry_end or 'no fill'}); close dropped")

    # -- order events
    def _filled(self, tag: str, price: float, qty: float, ts: int, done: bool) -> None:
        if self._is_disaster(tag):
            if done and self._disaster is not None and self._disaster.tag == tag:
                self._cancel_reduce_only()
                self._disaster_gone("filled", "triggered at the exchange")
            return
        if done:
            self._entries.discard(tag)
            self._entry_end = "no position was opened"
            if self.position().qty <= 0:
                self._cancel_reduce_only()
            if not self._entries:
                self._release_parked()
        self._reconcile_disaster()

    def _order_ended(self, tag: str, why: str) -> None:
        if self._is_disaster(tag):
            if self._disaster is not None and self._disaster.tag == tag:
                self._disaster_gone("rejected" if why else "cancelled", why)
            return
        if refused(why) and tag in self._result_at:  # the exchange turned it down after it was handed over
            known = self.results[self._result_at[tag]]
            self._record(ExecutionResult(known.intent, False, why, known.ref), tag)
        if tag in self._entries:
            self._entries.discard(tag)
            self._entry_end = f"{tag} {why or 'cancelled'}"
        if not self._entries:
            self._release_parked()
        self._reconcile_disaster()

    def _cancel_reduce_only(self) -> None:
        """The position is flat: a protective order left resting would open a position of its own."""
        for order in self.cache.orders(instrument_id=self._iid, strategy_id=self.id):
            tag = self._tag_by_id.get(order.client_order_id, "")
            if (
                order.is_reduce_only
                and not order.is_closed
                and not order.is_pending_cancel
                and not self._is_disaster(tag)
            ):
                self.cancel_order(order)


def refused(why: str) -> bool:
    """An order end that is the exchange (or risk check) turning the order down, not a cancel or expiry."""
    return why.startswith(("rejected", "denied"))


class FollowerExecutor(IntentRunner):
    """Executes received :class:`OrderIntent` s as paper orders on live data.

    Sizing follows the intent contract: ``open``/``adjust``-buy = ``size_fraction x equity`` (equity starts at
    ``capital``); ``close`` = ``size_fraction x`` the follower's own position.
    """

    def __init__(
        self,
        instrument: str,
        capital: float,
        *,
        disaster_stop_pct: float,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
        log_level: str = "ERROR",
    ) -> None:
        super().__init__()
        if capital <= 0:
            raise ValueError(f"capital must be positive, got {capital}")
        self.instrument = normalize_instrument(instrument)
        self.capital = capital
        self.strategy = FollowerStrategy(
            FollowerConfig(
                instrument_id=to_nautilus_id(self.instrument),
                order_id_tag="002",
                disaster_stop_pct=check_disaster_stop_pct(disaster_stop_pct),
            )
        )
        self.session = PaperSession(self.strategy, self.instrument, capital, fee_rate=fee_rate, log_level=log_level)

    def _dispatch(self, intent: OrderIntent, ref: str | None) -> None:
        self.strategy.execute(intent)

    def run(
        self, duration_s: float | None = None, on_ready: Callable[[FollowerExecutor], None] | None = None
    ) -> PaperReport:
        elapsed = self._run_session(duration_s, on_ready)
        return _report(
            self.strategy,
            self.instrument,
            elapsed,
            self.capital,
            len(self.strategy.results),
            0,
            self.strategy.disaster_stops(),
        )

    @property
    def results(self) -> list[ExecutionResult]:
        return self.strategy.results
