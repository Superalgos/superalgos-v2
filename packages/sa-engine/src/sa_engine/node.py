"""The live Nautilus node shared by paper trading and testnet trading.

* :class:`NodeSession` owns the ``TradingNode`` lifecycle for one strategy: build from the subclass's wiring,
  start, run until stopped, tear down. ``run`` blocks the calling (main) thread.
* :class:`IntentRunner` is the thread-safe front of a session that executes external intents: intents submitted
  before the node is ready wait in a backlog.

The wiring (which data and execution clients exist) is the subclass's: :class:`sa_engine.paper.PaperSession`
creates no Binance execution client; :class:`sa_engine.testnet.TestnetSession` creates one for the testnet only.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode

from sa_engine.compile import ExecutionBase
from sa_engine.intent import OrderIntent

STARTUP_TIMEOUT_S = 90.0


class NodeSession(ABC):
    """One live node around a single strategy. ``run`` blocks the calling (main) thread."""

    def __init__(self, strategy: ExecutionBase, instrument: str, log_level: str = "ERROR") -> None:
        self.strategy = strategy
        self.instrument = instrument  # VENUE:SYMBOL
        self.log_level = log_level
        self.started = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._node: TradingNode | None = None

    @property
    def venue(self) -> str:
        return self.instrument.partition(":")[0]

    @abstractmethod
    def node_config(self) -> TradingNodeConfig:
        """The node's clients. May hold credentials: never log or ``repr`` the result."""

    @abstractmethod
    def add_factories(self, node: TradingNode) -> None:
        """Register the data and execution client factories for :attr:`venue`."""

    def prepare(self, node: TradingNode) -> None:  # pragma: no cover - needs the live node
        """Hook between ``build`` and ``run``: add actors, the strategy and its execution algorithms."""
        for algorithm in self.strategy.exec_algorithms():
            node.trader.add_exec_algorithm(algorithm)
        node.trader.add_strategy(self.strategy)

    def stop(self) -> None:
        """Thread-safe: ask ``run`` to shut down."""
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Thread-safe: run ``fn`` inside the node's event loop (where Nautilus objects may be touched)."""
        if self._loop is None:
            raise RuntimeError("session is not running; call run() first")
        self._loop.call_soon_threadsafe(fn)

    def run(  # pragma: no cover - needs a node; tests/test_paper_online.py, tests/test_testnet_online.py
        self, duration_s: float | None = None, on_ready: Callable[[NodeSession], None] | None = None
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        node = TradingNode(self.node_config(), loop=loop)
        self._node = node
        self.add_factories(node)
        node.build()
        self.prepare(node)
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

    async def _main(  # pragma: no cover - needs a node; tests/test_paper_online.py, tests/test_testnet_online.py
        self, node: TradingNode, duration_s: float | None, on_ready: Callable[[NodeSession], None] | None
    ) -> None:
        self._stop = asyncio.Event()
        runner = asyncio.ensure_future(node.run_async())
        waited = 0.0
        while not node.trader.is_running:
            if runner.done():
                runner.result()
                raise RuntimeError("node stopped before starting; see the Nautilus log (set log_level='INFO')")
            if waited > STARTUP_TIMEOUT_S:
                raise TimeoutError(
                    f"node did not start within {STARTUP_TIMEOUT_S:.0f}s; check network access and the credentials"
                )
            await asyncio.sleep(0.2)
            waited += 0.2
        self.started.set()
        if on_ready is not None:
            on_ready(self)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=duration_s)
        self.strategy.final_equity = self.strategy.equity()  # read before the portfolio is torn down
        self.strategy.final_working_orders = self.strategy.working_orders()
        await node.stop_async()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


class IntentRunner(ABC):
    """Thread-safe intake for a session that executes intents. Subclasses say how one intent is dispatched."""

    session: NodeSession

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backlog: list[tuple[OrderIntent, str | None]] = []

    @abstractmethod
    def _dispatch(self, intent: OrderIntent, ref: str | None) -> None:
        """Hand one intent to the strategy. Runs inside the node's event loop."""

    def submit(self, intent: OrderIntent, ref: str | None = None) -> None:
        """Thread-safe. Intents submitted before the node is ready are executed as soon as it is."""
        with self._lock:
            if not self.session.started.is_set():
                self._backlog.append((intent, ref))
                return
        self.session.call_soon(lambda: self._dispatch(intent, ref))

    def _ready(self, session: NodeSession) -> None:
        with self._lock:
            backlog, self._backlog = self._backlog, []
        for intent, ref in backlog:
            self._dispatch(intent, ref)

    def _run_session(self, duration_s: float | None, on_ready: Callable[[Any], None] | None) -> float:
        """Run the session until ``duration_s`` (or stop); returns the elapsed seconds."""
        import time

        def ready(session: NodeSession) -> None:
            self._ready(session)
            if on_ready is not None:
                on_ready(self)

        t0 = time.monotonic()
        self.session.run(duration_s, ready)
        return time.monotonic() - t0
