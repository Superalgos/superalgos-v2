"""Testnet trading (SEP-3.17): received signals become real orders on the follower's Binance *spot testnet* account.

Wiring (NautilusTrader 1.231): a ``TradingNode`` with the Binance data client and the Binance execution client,
both with ``environment=TESTNET`` (https://testnet.binance.vision). The credentials are the follower's own and
stay on this machine.

Guards, all before any node starts:

* the environment must be ``BinanceEnvironment.TESTNET`` and no ``base_url_*`` override may be given
  (:func:`check_testnet_target`); the built node config is checked again (:func:`assert_testnet_only`);
* the API key and secret are never put in a message, a log line or a ``repr``: they live in
  :class:`~sa_core.keys.ExchangeCredentials` (pydantic ``SecretStr``) and reach Nautilus only inside the config
  object, which this module never prints. Nautilus itself logs neither the node config nor the secret
  (``print_config`` is forced off and only a masked key is ever logged on connect).

There is no mainnet code path here. Spot, long-only, as in paper mode.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from decimal import Decimal

from nautilus_trader.adapters.binance import (
    BinanceAccountType,
    BinanceDataClientConfig,
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
)
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.config import BinanceExecClientConfig, BinanceInstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig, LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.events import OrderEmulated, OrderFilled, OrderReleased
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from pydantic import BaseModel, ConfigDict
from sa_core.keys import ExchangeCredentials

from sa_engine.intent import OrderIntent, normalize_instrument, to_nautilus_id
from sa_engine.network.executor import Execution
from sa_engine.node import IntentRunner, NodeSession
from sa_engine.paper import (
    DisasterStop,
    ExecutionResult,
    FollowerConfig,
    FollowerStrategy,
    refused,
)
from sa_engine.protection import check_disaster_stop_pct


class NotTestnetError(ValueError):
    """The requested Binance environment or endpoint is not the spot testnet."""


def check_testnet_target(
    environment: object = BinanceEnvironment.TESTNET,
    base_url_http: str | None = None,
    base_url_ws: str | None = None,
    base_url_ws_stream: str | None = None,
) -> None:
    """Raise :class:`NotTestnetError` unless the target is the stock spot testnet."""
    if environment is not BinanceEnvironment.TESTNET:
        raise NotTestnetError(
            f"environment {environment!r} is not allowed; the testnet executor only runs on BinanceEnvironment.TESTNET"
        )
    for name, value in (
        ("base_url_http", base_url_http),
        ("base_url_ws", base_url_ws),
        ("base_url_ws_stream", base_url_ws_stream),
    ):
        if value is not None:
            raise NotTestnetError(f"{name}={value!r} is not allowed; remove the override, the testnet URL is built in")


def assert_testnet_only(config: TradingNodeConfig) -> None:
    """Defence in depth: refuse a node config in which any Binance client is not the plain testnet."""
    for clients in (config.data_clients, config.exec_clients):
        for cfg in clients.values():
            check_testnet_target(
                getattr(cfg, "environment", None),
                getattr(cfg, "base_url_http", None),
                getattr(cfg, "base_url_ws", None),
                getattr(cfg, "base_url_ws_stream", None),
            )
            if getattr(cfg, "account_type", None) is not BinanceAccountType.SPOT:
                raise NotTestnetError(f"account_type {getattr(cfg, 'account_type', None)!r} is not SPOT; spot only")


class SpotTestnetSession(NodeSession):
    """One live node on the Binance spot testnet: testnet data and testnet execution, no reconciliation."""

    def __init__(
        self,
        strategy: SpotTestnetStrategy,
        instrument: str,
        credentials: ExchangeCredentials,
        *,
        environment: object = BinanceEnvironment.TESTNET,
        base_url_http: str | None = None,
        base_url_ws: str | None = None,
        base_url_ws_stream: str | None = None,
        log_level: str = "WARNING",
    ) -> None:
        check_testnet_target(environment, base_url_http, base_url_ws, base_url_ws_stream)
        if not credentials.api_key.get_secret_value() or not credentials.api_secret.get_secret_value():
            raise ValueError("the testnet API key and secret must not be empty; run `sa exchange add binance-testnet`")
        super().__init__(strategy, instrument, log_level)
        self._credentials = credentials

    def node_config(self) -> TradingNodeConfig:
        """Holds the API secret: never log or ``repr`` the result."""
        iid = InstrumentId.from_str(to_nautilus_id(self.instrument))
        provider = BinanceInstrumentProviderConfig(load_ids=frozenset({iid}))
        key = self._credentials.api_key.get_secret_value()
        secret = self._credentials.api_secret.get_secret_value()
        config = TradingNodeConfig(
            trader_id=TraderId("TESTNET-001"),
            logging=LoggingConfig(log_level=self.log_level, use_pyo3=False, print_config=False),
            exec_engine=LiveExecEngineConfig(reconciliation=False),
            data_clients={
                self.venue: BinanceDataClientConfig(
                    account_type=BinanceAccountType.SPOT,
                    api_key=key,
                    api_secret=secret,
                    environment=BinanceEnvironment.TESTNET,
                    instrument_provider=provider,
                )
            },
            exec_clients={
                self.venue: BinanceExecClientConfig(
                    account_type=BinanceAccountType.SPOT,
                    api_key=key,
                    api_secret=secret,
                    environment=BinanceEnvironment.TESTNET,
                    instrument_provider=provider,
                )
            },
        )
        assert_testnet_only(config)
        return config

    def add_factories(self, node: TradingNode) -> None:
        node.add_data_client_factory(self.venue, BinanceLiveDataClientFactory)
        node.add_exec_client_factory(self.venue, BinanceLiveExecClientFactory)


class SpotTestnetStrategy(FollowerStrategy):
    """A :class:`FollowerStrategy` that reports what the exchange does with each order as :class:`Execution` events.

    Events are queued in the node thread and taken by :meth:`drain` from any thread. ``ref`` of an event is the id
    the caller gave :meth:`execute` (the signal event id).
    """

    def __init__(self, config: FollowerConfig) -> None:
        super().__init__(config)
        self._events: deque[Execution] = deque()
        self._venue_ids: dict[str, str] = {}

    def drain(self) -> list[Execution]:
        """Thread-safe: the events since the last call, oldest first."""
        out: list[Execution] = []
        while self._events:
            out.append(self._events.popleft())
        return out

    def _record(self, result: ExecutionResult, tag: str | None = None) -> None:
        super()._record(result, tag)
        if not result.placed and result.ref is not None:
            self._events.append(Execution(result.ref, False, reason=result.reason))

    def _accepted(self, tag: str, venue_id: str, ts_ms: int) -> None:
        super()._accepted(tag, venue_id, ts_ms)
        if tag in self._reported:
            return
        self._venue_ids[tag] = venue_id
        self._events.append(
            Execution(tag, True, reason="accepted by the Binance testnet", ts_ms=ts_ms, venue_order_id=venue_id)
        )

    def on_order_emulated(self, event: OrderEmulated) -> None:
        tag = self._tag_by_id.get(event.client_order_id)
        order = self.cache.order(event.client_order_id)
        if tag is None or order is None or tag in self._reported:
            return
        self._events.append(
            Execution(
                tag,
                True,
                reason=f"stop held locally at {order.trigger_price}; the exchange never sees it",
                ts_ms=event.ts_event // 1_000_000,
            )
        )

    def on_order_released(self, event: OrderReleased) -> None:
        tag = self._tag_by_id.get(event.client_order_id)
        if tag is None or tag in self._reported:
            return
        self._events.append(
            Execution(
                tag,
                True,
                reason=f"stop triggered at {event.released_price}: sending a market order",
                ts_ms=event.ts_event // 1_000_000,
            )
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        tag = self._tag_by_id.get(event.client_order_id)
        if tag is not None:
            self._venue_ids[tag] = event.venue_order_id.value
        super().on_order_filled(event)

    def _filled(self, tag: str, price: float, qty: float, ts: int, done: bool) -> None:
        if not self._is_disaster(tag):
            self._report_fill(tag, price, qty, ts, done)
        super()._filled(tag, price, qty, ts, done)  # after the report: a close that waited is placed next

    def _report_fill(self, tag: str, price: float, qty: float, ts: int, done: bool) -> None:
        mine = [f for f in self.fills if f.tag == tag]
        total = sum((Decimal(str(f.qty)) for f in mine), Decimal(0))
        notional = sum((Decimal(str(f.qty)) * Decimal(str(f.price)) for f in mine), Decimal(0))
        self._events.append(
            Execution(
                tag,
                True,
                True,
                reason="filled" if done else "partially filled",
                qty=format(total.normalize(), "f"),
                price=float(notional / total) if total else price,
                fee=sum(f.commission for f in mine),
                ts_ms=ts,
                venue_order_id=self._venue_ids.get(tag),
            )
        )

    def _order_ended(self, tag: str, why: str) -> None:
        if tag not in self._reported:
            self._report_end(tag, why)
        super()._order_ended(tag, why)

    def _report_end(self, tag: str, why: str) -> None:
        self._events.append(
            Execution(
                tag,
                not refused(why),
                reason=why or "cancelled",
                venue_order_id=self._venue_ids.get(tag),
            )
        )


class SpotTestnetReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: str
    duration_s: float
    intents: int
    placed: int
    working_orders: int = 0  # resting orders left at the end of the run
    rejections: list[str]
    disaster_stops: list[DisasterStop] = []  # SEP-14.5: venue order id and how each ended


class SpotTestnetFollower(IntentRunner):
    """Executes received :class:`OrderIntent` s as orders on the follower's Binance spot testnet account.

    Sizing follows the intent contract with the account's real testnet equity: ``open``/``adjust``-buy =
    ``size_fraction x equity``; ``close`` = ``size_fraction x`` the position this node has opened.
    """

    def __init__(
        self,
        instrument: str,
        credentials: ExchangeCredentials,
        *,
        environment: object = BinanceEnvironment.TESTNET,
        base_url_http: str | None = None,
        base_url_ws: str | None = None,
        base_url_ws_stream: str | None = None,
        disaster_stop_pct: float,
        log_level: str = "WARNING",
    ) -> None:
        super().__init__()
        self.instrument = normalize_instrument(instrument)
        self.strategy = SpotTestnetStrategy(
            FollowerConfig(
                instrument_id=to_nautilus_id(self.instrument),
                order_id_tag="003",
                disaster_stop_pct=check_disaster_stop_pct(disaster_stop_pct),
            )
        )
        self.session: NodeSession = SpotTestnetSession(
            self.strategy,
            self.instrument,
            credentials,
            environment=environment,
            base_url_http=base_url_http,
            base_url_ws=base_url_ws,
            base_url_ws_stream=base_url_ws_stream,
            log_level=log_level,
        )

    def _dispatch(self, intent: OrderIntent, ref: str | None) -> None:
        self.strategy.execute(intent, ref)

    def drain(self) -> list[Execution]:
        return self.strategy.drain()

    def run(
        self, duration_s: float | None = None, on_ready: Callable[[SpotTestnetFollower], None] | None = None
    ) -> SpotTestnetReport:
        elapsed = self._run_session(duration_s, on_ready)
        results = self.strategy.results
        return SpotTestnetReport(
            instrument=self.instrument,
            duration_s=elapsed,
            intents=len(results),
            placed=sum(1 for r in results if r.placed),
            working_orders=self.strategy.final_working_orders,
            rejections=list(self.strategy.rejections),
            disaster_stops=self.strategy.disaster_stops(),
        )

    @property
    def results(self) -> list[ExecutionResult]:
        return self.strategy.results
