"""Paper trading: live Binance public market data, simulated fills, no API keys, no real orders.

Wiring (NautilusTrader 1.231): a ``TradingNode`` with the Binance *data* client (public streams, no
credentials) and Nautilus' ``SandboxExecutionClient`` (a ``SimulatedExchange`` matching engine fed by the live
quotes/bars). Nothing here can reach an order endpoint: no execution client for Binance is ever created.

* :func:`run_paper` runs a strategy document live.
* :class:`FollowerExecutor` executes external :class:`OrderIntent` s (received signals) as paper orders.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.binance import BinanceAccountType, BinanceDataClientConfig, BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance.config import BinanceInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, LiveExecEngineConfig, LoggingConfig, StrategyConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TraderId
from nautilus_trader.model.instruments import CurrencyPair
from pydantic import BaseModel, ConfigDict

from sa_engine.compile import ExecutionBase, IntentHook, compile_strategy
from sa_engine.intent import OrderIntent, from_nautilus_id, normalize_instrument, to_nautilus_id
from sa_engine.report import Fill, Trade, match_trades
from sa_engine.schema import StrategyDoc, validate_strategy

DEFAULT_FEE_RATE = Decimal("0.001")  # Binance spot standard taker/maker; public (keyless) instrument data reports 0
STARTUP_TIMEOUT_S = 90.0


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
    trades: list[Trade]
    rejections: list[str]


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


@dataclass
class PaperSession:
    """One live paper-trading node around a single strategy. ``run`` blocks the calling (main) thread."""

    strategy: ExecutionBase
    instrument: str  # VENUE:SYMBOL
    capital: float
    fee_rate: Decimal = DEFAULT_FEE_RATE
    log_level: str = "ERROR"
    started: threading.Event = field(default_factory=threading.Event)
    _loop: asyncio.AbstractEventLoop | None = None
    _stop: asyncio.Event | None = None
    _node: TradingNode | None = None

    def sandbox_client(self) -> Any:  # pragma: no cover - needs the live node
        assert self._node is not None
        return self._node.kernel.exec_engine._clients.get(ClientId(self.instrument.partition(":")[0]))

    def node_config(self) -> TradingNodeConfig:
        venue = self.instrument.partition(":")[0]
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

    def stop(self) -> None:
        """Thread-safe: ask ``run`` to shut down."""
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Thread-safe: run ``fn`` inside the node's event loop (where Nautilus objects may be touched)."""
        if self._loop is None:
            raise RuntimeError("session is not running; call run() first")
        self._loop.call_soon_threadsafe(fn)

    def run(  # pragma: no cover - needs network; tests/test_paper_online.py
        self, duration_s: float | None = None, on_ready: Callable[[PaperSession], None] | None = None
    ) -> None:
        venue = self.instrument.partition(":")[0]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        node = TradingNode(self.node_config(), loop=loop)
        self._node = node
        node.add_data_client_factory(venue, BinanceLiveDataClientFactory)
        node.add_exec_client_factory(venue, SandboxLiveExecClientFactory)
        node.build()
        fee_cfg = _FeeConfig(instrument_id=to_nautilus_id(self.instrument), fee_rate=str(self.fee_rate))
        node.trader.add_actor(_SandboxFees(fee_cfg, self))
        node.trader.add_strategy(self.strategy)
        try:
            loop.run_until_complete(self._main(node, duration_s, on_ready))
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            node.dispose()  # also closes the loop
            if not loop.is_closed():
                loop.close()
            asyncio.set_event_loop(None)

    async def _main(  # pragma: no cover - needs network; tests/test_paper_online.py
        self, node: TradingNode, duration_s: float | None, on_ready: Callable[[PaperSession], None] | None
    ) -> None:
        self._stop = asyncio.Event()
        runner = asyncio.ensure_future(node.run_async())
        waited = 0.0
        while not node.trader.is_running:
            if runner.done():
                runner.result()
                raise RuntimeError("paper node stopped before starting; see the Nautilus log (set log_level='INFO')")
            if waited > STARTUP_TIMEOUT_S:
                raise TimeoutError(
                    f"paper node did not start within {STARTUP_TIMEOUT_S:.0f}s; check network access to Binance"
                )
            await asyncio.sleep(0.2)
            waited += 0.2
        self.started.set()
        if on_ready is not None:
            on_ready(self)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=duration_s)
        self.strategy.final_equity = self.strategy.equity()  # read before the portfolio is torn down
        await node.stop_async()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


def _binance_timeframe_bar_type(instrument_id: str, timeframe: str) -> str:
    n, unit = int(timeframe[:-1]), {"m": "MINUTE", "h": "HOUR", "d": "DAY"}[timeframe[-1]]
    return f"{instrument_id}-{n}-{unit}-LAST-EXTERNAL"


def _report(
    s: ExecutionBase, instrument: str, duration_s: float, capital: float, intents: int, bars: int
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
        trades=trades,
        rejections=list(s.rejections),
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


class FollowerConfig(StrategyConfig, frozen=True, kw_only=True):
    instrument_id: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent: OrderIntent
    placed: bool
    reason: str = ""  # why it was not placed


class FollowerStrategy(ExecutionBase):
    """Executes external intents. Intents arriving before the first quote wait for it (a market needs a price)."""

    def __init__(self, config: FollowerConfig) -> None:
        super().__init__(config, config.instrument_id)
        self._waiting: list[OrderIntent] = []
        self._n = 0
        self.results: list[ExecutionResult] = []

    def on_start(self) -> None:
        if self._load_instrument() is not None:
            self.subscribe_quote_ticks(self._iid)

    def _quote(self, tick: object) -> None:
        waiting, self._waiting = self._waiting, []
        for intent in waiting:
            self._execute_now(intent)

    def execute(self, intent: OrderIntent) -> None:
        """Must run inside the node's event loop (use :meth:`FollowerExecutor.submit` from other threads)."""
        if self._last_price <= 0:
            self._waiting.append(intent)
        else:
            self._execute_now(intent)

    def _execute_now(self, intent: OrderIntent) -> None:
        if intent.instrument != from_nautilus_id(str(self._iid)):
            self.results.append(
                ExecutionResult(intent, False, f"intent is for {intent.instrument}, this executor trades {self._iid}")
            )
            return
        self._n += 1
        reason = self._place(intent, f"follow{self._n}", reduce_only=intent.action == "close")
        self.results.append(ExecutionResult(intent, not reason, reason))
        if reason:
            self.log.warning(f"intent not executed: {reason}")


class FollowerExecutor:
    """Executes received :class:`OrderIntent` s as paper orders on live data.

    Sizing follows the intent contract: ``open``/``adjust``-buy = ``size_fraction x equity`` (equity starts at
    ``capital``); ``close`` = ``size_fraction x`` the follower's own position.
    """

    def __init__(
        self,
        instrument: str,
        capital: float,
        *,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
        log_level: str = "ERROR",
    ) -> None:
        if capital <= 0:
            raise ValueError(f"capital must be positive, got {capital}")
        self.instrument = normalize_instrument(instrument)
        self.capital = capital
        self.strategy = FollowerStrategy(
            FollowerConfig(instrument_id=to_nautilus_id(self.instrument), order_id_tag="002")
        )
        self.session = PaperSession(self.strategy, self.instrument, capital, fee_rate=fee_rate, log_level=log_level)
        self._lock = threading.Lock()
        self._backlog: list[OrderIntent] = []

    def submit(self, intent: OrderIntent) -> None:
        """Thread-safe. Intents submitted before the node is ready are executed as soon as it is."""
        with self._lock:
            if not self.session.started.is_set():
                self._backlog.append(intent)
                return
        self.session.call_soon(lambda: self.strategy.execute(intent))

    def _ready(self, session: PaperSession) -> None:
        with self._lock:
            backlog, self._backlog = self._backlog, []
        for intent in backlog:
            self.strategy.execute(intent)

    def run(
        self, duration_s: float | None = None, on_ready: Callable[[FollowerExecutor], None] | None = None
    ) -> PaperReport:
        import time

        def ready(session: PaperSession) -> None:
            self._ready(session)
            if on_ready is not None:
                on_ready(self)

        t0 = time.monotonic()
        self.session.run(duration_s, ready)
        return _report(
            self.strategy, self.instrument, time.monotonic() - t0, self.capital, len(self.strategy.results), 0
        )

    @property
    def results(self) -> list[ExecutionResult]:
        return self.strategy.results
