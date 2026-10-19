# sa-engine notes

Findings from the investigation phase, decisions, and how to run things. Everything below was run, not assumed,
unless marked "not verified".

## Investigation findings

### NautilusTrader version and Python support
- Source: https://pypi.org/pypi/nautilus_trader/json (opened). Latest stable is **1.231.0**, `requires_python >=3.12,<3.15`,
  wheels for CPython 3.12/3.13/3.14 on macOS arm64 (macosx_26_0), manylinux x86_64/aarch64 and Windows. `2.0.0rc1..rc6`
  exist on PyPI as pre-releases and use a different, Rust-first API (see "2.0" below). sa-engine pins
  `nautilus_trader>=1.231,<1.232`; the workspace pins Python 3.13 (`.python-version`) and every package says `>=3.12,<3.14`.
- License of the dependency is LGPL-3.0 (header of `nautilus_trader/adapters/sandbox/*.py`); sa-engine only imports it.
- Nautilus ships compiled modules **without type stubs**. All Nautilus calls are isolated in four adapter modules
  (`compile`, `catalog`, `backtest`, `paper`); the **root** `pyproject.toml` relaxes `mypy --strict` for exactly those modules
  (`disallow_subclassing_any`, `disallow_untyped_calls`, `warn_return_any`, `disallow_any_unimported` off) and
  `ignore_missing_imports` for `nautilus_trader.*`. Everything else (`dsl`, `schema`, `intent`, `brain`, `sizing`,
  `report`, `indicators`, `data`, `cli`) is `--strict` clean with no relaxations.

### Writing a Strategy
- Read from the installed 1.231.0 sources (`nautilus_trader/trading/strategy`, `common/factories.py` signatures via
  `inspect`) and `.../live/node.py`. A strategy subclasses `Strategy` with a `StrategyConfig`, overrides
  `on_start/on_bar/on_quote_tick/on_order_filled/on_order_*`, builds orders with `self.order_factory.market/limit/stop_market`
  and sends them with `submit_order`.
- Gotchas found the hard way:
  1. **Quantities must be built with `instrument.make_qty(...)`.** `Quantity.from_str("0.001")` with a different precision
     than the instrument makes the Rust core panic, which exits the process with code 1 and *no message*
     (reproduced in paper mode against the live instrument, precision 8).
  2. A bar aggregated from a composite subscription (`BTCUSDT.BINANCE-1-HOUR-LAST-INTERNAL@1-MINUTE-EXTERNAL`) arrives with
     the `@source` part stripped, so compare with `bar_type.standard()`.
  3. `Bar.ts_event` is the bar **close** time. Binance klines carry the open time, so `data.read_month` indexes by open + 1 min.
  4. MARGIN account (leverage 1) is used instead of CASH: in a CASH account a take-profit limit and its stop-loss both lock the
     same BTC, so the second one is rejected. MARGIN with `margin_init=0` has no such lock; long-only is enforced by sizing.

### Binance 1m klines into a catalog and a backtest
- Source URL pattern (fetched): `https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-YYYY-MM.zip`
  and the sidecar `...zip.CHECKSUM` (`<sha256>  <file>`). **Spot files switched to microsecond timestamps in 2025**; `data.py`
  detects ms vs us and a header row.
- Flow: zip -> pandas -> `BarDataWrangler(BarType "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", instrument).process(df)` ->
  `ParquetDataCatalog.write_data([instrument])` / `write_data(bars)` -> `catalog.bars(bar_types=[...], start=ns, end=ns)`.
  Months already converted get a marker file, so a second run does no network and no rewrite.
- `BarDataWrangler.process` fails on pandas 3 (`ValueError: buffer source array is read-only`, pandas 3.0.6), so sa-engine pins `pandas>=2.3,<3`.
- Backtest uses `BacktestEngine` (not `BacktestNode`): add venue (NETTING, MARGIN, 10 000 USDT, `bar_execution=True`), instrument,
  the 1m bars, then the compiled strategy. 525 600 bars (one year) run in about 7 s plus data conversion.
- Determinism: same inputs give the same `BacktestReport.fingerprint()` (sha256 of the report JSON), also across processes (checked).

### Paper trading with live Binance data and simulated fills (no keys)
- **Possible without API keys.** `nautilus_trader.adapters.sandbox.SandboxExecutionClient` (opened:
  `adapters/sandbox/{config,factory,execution}.py`) wraps a `SimulatedExchange` matching engine and subscribes to
  `data.*.<VENUE>.*` on the message bus, so it fills against whatever the live data client publishes (quotes, bars).
  `BinanceDataClientConfig` with `api_key=None` streams public data; `BinanceLiveDataClientFactory` (opened `adapters/binance/factories.py`)
  only resolves env keys for the *execution* client, which we never create. Verified live: quotes flow (about 100 per second on
  BTCUSDT), orders fill in the simulator, positions and fees update.
- Upstream confirms the pattern: https://raw.githubusercontent.com/nautechsystems/nautilus_trader/master/examples/live/sandbox/exec_tester.py
  ("streams public Binance Spot data while the sandbox matching engine simulates execution locally ... No credentials or real funds").
  That file targets the 2.0 API. I also grepped `docs/concepts/live.md` and `docs/integrations/binance.md` on master for "sandbox": no hits.
- Details that matter (all in `paper.py`):
  - The keyless Binance instrument has `maker_fee = taker_fee = 0`. A tiny start-up `Actor` replaces it in the cache and in the
    sandbox exchange with 0.1% fees (Binance spot default), so paper PnL is not flattered. Configurable via `fee_rate`.
  - `LiveExecEngineConfig(reconciliation=False)`: there is nothing to reconcile in a simulator and it skips a 10 s start-up delay.
  - `node.dispose()` closes the event loop itself; pending tasks are cancelled before it, otherwise Nautilus' executor logs
    "Event loop is closed". `NodeSession.run` must be called from the main thread (Nautilus installs signal handlers);
    other threads use `call_soon` / `FollowerExecutor.submit` (thread-safe).
  - Live bars: strategies subscribe to the native Binance timeframe (`...-1-HOUR-LAST-EXTERNAL`) and request up to 1000 bars of
    history at start to warm the indicators. Quote ticks are subscribed so limit/stop orders can fill between bars.
- 2.0 migration (not done, not verified): on master the same idea is `LiveNode.builder(...).add_simulated_exec_client(...)`.
  The seam (`OrderIntent`, `StrategyBrain`) is Nautilus-free, so only `compile.py`/`paper.py` would change.

## What was built

| Module | Purpose |
|---|---|
| `schema.py` | Pydantic strategy document, **schema version 2**: `validate_strategy` / `validate_issues` (every error is `{path, column, message}`), `json_schema()`; exported file `schema/strategy.schema.json` |
| `dsl.py` | Condition (boolean) and formula (numeric) expression language: recursive-descent parser to a typed AST, evaluator, higher-timeframe references, no `eval`/`exec` |
| `events.py` | v1's event logic: situation = AND of conditions, event = OR of situations, first match wins |
| `indicators.py` | Pure-Python streaming EMA, SMA, RSI (Wilder), Bollinger, ATR (Wilder), Donchian + `IndicatorBank` with history for `prev()`/`cross_*`, and `HigherTimeframeFeed` (closed higher-timeframe bars built from the strategy's bars) |
| `brain.py` | Nautilus-free state machine: bars/fills in, `Submit`/`Cancel` out (trigger, open cycle with create/cancel/re-arm per order, brackets with OCO, manage rules, phased stop and target, close) |
| `intent.py` | `OrderIntent` (the signal seam) and instrument id helpers |
| `sizing.py` | `OrderIntent` to quantity (shared by strategies and followers) |
| `compile.py` | `CompiledStrategy` (Nautilus `Strategy` around the brain); `on_order_intent` hook; shared `ExecutionBase` |
| `data.py`, `catalog.py` | Download + sha256 verification, cache, catalog conversion |
| `backtest.py`, `report.py` | `run_backtest(...) -> BacktestReport`; FIFO trade matching, drawdown; `BacktestReport.fills` keeps every executed order `{t, side, price, qty, fee}` (SEP-8 chart markers; older report files load with `fills: []`) |
| `node.py` | `NodeSession` (node lifecycle), `IntentRunner` (thread-safe intent intake) |
| `paper.py` | `run_paper`, `PaperSession`, `FollowerExecutor` |
| `testnet.py` | `SpotTestnetSession`, `SpotTestnetStrategy`, `SpotTestnetFollower`, `check_testnet_target` |
| `cli.py` | `python -m sa_engine {validate,schema,backtest,paper}` |
| `network/` | provider bot, follower bot, journal, ledger, paper executors, replay clock, fixture oracle, entitlements (depends on `sa_core`; see ARCHITECTURE.md "Network bots"). Tests: `tests/test_network_*.py` against an in-process relay; the real relay is exercised by `tests/e2e` |

### Strategy document, schema 2 (mapped from v1's strategy model)
One JSON document, `schema_version: 2` (version 1 documents are refused with the way forward; there is no compatibility layer).

```
trigger  { on: Event, off?: Event }
open     { when: Event, orders[ { id, type market|limit|stop, size_fraction | size(formula), offset_pct | rate(formula),
                                  create?: Event, cancel?: Event, rearm, take_profit_pct?, stop_loss_pct? } ] }
manage   { rules[ { id, when: Event, action cancel_pending|reduce, size_fraction?, once } ],
           stop?:   { phases[ { id, level(formula), next?: Event, goto[ { when: Event, to: phase id } ] } ] },
           target?: { phases[ ... same ... ] } }
close    { when: Event }
Event     = { any: [ Situation ] }          OR   (first passing situation, in array order, wins and is the audit label)
Situation = { name?, all: [ condition ] }   AND  (a condition is a boolean DSL string)
```

* **Events** (SEP-5.1). Exactly v1's rule: a situation passes when every condition is true, an event fires
  when any situation passes, the first one wins. v1 let an empty situation or event exist and never fire; v2 rejects them
  (`min_length=1`) because that is a silent dead end, while `sa_engine.events` still treats them as never firing. The name of the
  situation that fired is recorded in `StrategyBrain.log` (`Fired(ts, event, situation)`), the audit label v1 kept on every record.
* **Formulas** (SEP-5.2): `size` (fraction of equity), `rate` (limit/stop price), phase `level` (stop or target price). Same safe DSL
  as conditions, numeric: `+ - * /`, parentheses, `min max abs coalesce`, prices, indicators, `prev`, and the position fields
  `entry_price position_size bars_in_position unrealized_pnl_pct` plus `stop_price` / `target_price` (the level in force, so a
  formula can say "never loosen": `max(coalesce(stop_price, 0), close - 2.5 * atr(14))`). A formula with no value (warm-up, divide by
  zero) means "keep the previous value" (levels) or "not this bar" (size, rate); a stop of 0 is v1's "no stop".
* **Orders** (SEP-5.4). `create` is checked on every bar of the cycle while the order is not placed (absent: placed with the cycle);
  `cancel` while it is working; `rearm` puts the slot back to "not placed" after it fills or is cancelled, so the level is placed
  again by `create` at the then-current price (v1 `spawnMultipleOrders`). A cycle ends when the position is flat, nothing is working
  and nothing is left to place. Total exposure (held + working) is capped at 100% of equity: a larger order shrinks to what is left
  (v1 "Order Size Shrinked") and one under 0.01% is skipped. An order the venue or sizing refuses is not re-armed (no dust loop).
  Limit rates are capped at the bar close and stop rates floored at it, as the static `offset_pct` rules already required.
* **Phases** (SEP-5.3). Each bar close, while a position is held: the current phase's `next` event (-> the following phase) is
  checked first, else its `goto` events in array order (-> the named phase), at most one move per bar; then the formula of the phase
  that is current sets the level. Phase 1 is current when the position opens; levels reset when it is flat. Stop and target are two
  independent lists.
* **Multi-timeframe** (SEP-5.5). A last argument names a higher timeframe: `ema(close, 20, "4h")`, `ema(20, "4h")` (source defaults
  to close), `atr(14, "1d")`, `close("4h")`. The value is the one of the last **closed** bar of that timeframe at the moment the
  strategy bar closes, built from the strategy's own bars; it must be a whole multiple of the strategy timeframe (checked with a
  path and column). `prev(...)` of such a value is what the strategy saw that many strategy bars ago.
* **Per-order brackets** (`take_profit_pct`, `stop_loss_pct`) are unchanged: when that order fills, a limit and a stop rest at
  the venue (OCO). They are what protects a lot from its fill, which the phased stop (first evaluated on the next bar close) cannot.
  Documents may use both; a phase exit cancels every working order first.
* Long-only spot stays: `side` is `buy`, a `sell` entry is refused.
* Closed by design (v1 features not ported): user JavaScript, engine order slots, simulated-exchange nodes, signals and portfolio
  manager nodes, announcements.

### Timing rules (written down in ARCHITECTURE.md too; enforced by `tests/test_timing.py`, `test_phases.py`)
1. **Decisions are made on a bar close.** Events, sizes, rates, phase moves and phase formulas are evaluated when a strategy bar
   closes and never in between.
2. **A decision on bar N never meets bar N's own range.** Resting orders (limit, stop) are matched against bar N+1 onward; a market
   order fills at the first price after the close of N (in a backtest: N's close, which is N+1's open, never a price from N+1's
   range). Stop and target levels computed at the close of N are compared with the bars that close *after* N.
3. **Stop before target.** When one bar touches both levels the stop wins. The levels are watched on the finest bar the engine has
   (1m in a backtest, delivered before the strategy bar that closes at the same instant; quotes in paper mode), and a hit sells the
   whole position at market, so a managed exit is a plain market `close` intent (the signed-signal contract is untouched; nothing
   new for followers, and no stale stop orders that a follower could not cancel). The per-order brackets above rest at the venue and
   inherit the simulated venue's order for a bar touching both (high before low, so the target); use phases when the tie matters.
4. **A managed stop does not exist before the first bar close after the entry fills** (phase 1's formula runs then). Use
   `stop_loss_pct` on the order for protection from the fill.

### Schema 1 to 2 (how the repo's documents were migrated)
| Schema 1 | Schema 2 |
|---|---|
| `"schema_version": 1` | `2` |
| any condition string `"c"` (`trigger.on/off`, `open.when`, `close.when`, rule `when`) | `{"any": [{"name": "...", "all": ["c"]}]}`; `a and b` may become two conditions in one situation, `a or b` two situations |
| order `"when": "c"` | order `"create": <event>` (per-order condition checked at emission -> checked every bar until it fires) |
| `"manage": [rules]` | `"manage": {"rules": [rules]}` |
| `trigger.on` defaulted to `"true"` | still defaults to an `always` event |
| (not possible) | `size`, `rate`, `cancel`, `rearm`, `manage.stop`, `manage.target`, timeframe arguments |

### Order-intent seam (SEP-3.5)
Every order a compiled strategy places, including bracket exits and manual closes, goes through
`CompiledStrategy.on_order_intent(intent)` before submission. Pass `intent_hook=` or subclass.
Decisions to confirm with the sa-core maintainers:
1. **`order_type` has a third value `"stop"`** (stop-market, uses `stop_price`) besides `market`/`limit`, because stop-loss exits are intents too.
   ARCHITECTURE.md lists `market`/`limit` plus `stop_price?`; if sa-core must stay with two types, drop `stop_loss_pct` from signalled strategies or map it.
2. **Sizing contract:** `open`/`adjust`-buy: `size_fraction x equity / price`. `close`/`adjust`-sell: `size_fraction x` the receiver's own position (1 = flatten).
   Fractions of capital for closes would not self-correct on a follower whose position drifted.
3. **Fail closed:** if the hook raises, the order is not submitted, the error is logged and returned in `BacktestReport.rejections`.
   No executed order without its signal.
4. `ts` is the bar close time (backtest) or fill time (exits), in ms since epoch.

### Scope limits (deliberate, say so in review)
- Spot, long-only, leverage 1. A `sell` with `action="open"` is refused with a message. Trailing stops, break-even moves and indicator stops are phases (see "Strategy document, schema 2").
- **Rules chosen for v1 features that needed a decision** (change them here first): phases act on the whole position (not per lot); a phase exit is a market sell of everything and cancels all working orders; a re-armed order is placed again by its `create` event only while the cycle lasts (trigger off, close, `cancel_pending` and a level exit end re-arming); partial entry fills keep the order working and size each bracket to the filled part (unchanged); a formula that has no value skips the bar; exposure is capped at 100% of equity.
- Built-in instrument definitions for backtests: BTCUSDT and ETHUSDT (Binance precision, 0.1% fees). Paper mode loads the live instrument from Binance.
- `FollowerExecutor` sizes from the paper account's equity (equal to `capital` at start), not a fixed `capital`. This is the paper variant of SEP-3.17.
  `SpotTestnetFollower` (`testnet.py`) is the real-API variant: the same strategy on a node with Binance spot **testnet** data and execution clients
  (`environment=TESTNET`, any other environment or any `base_url_*` override raises before a node starts), sized from the testnet account's real
  equity. Reconciliation is off, so a position opened before the node started is unknown to it: a `close` only sizes against positions this
  run opened. Order ids, rejections and fills come back as `Execution` events (`venue_order_id` is stored in the ledger); a market order's id
  is taken from its fill if the venue sends no accepted event. Fees are the commission Binance reports, in its own currency. Credentials:
  `sa exchange add binance-testnet`; the live probe is `tests/test_testnet_online.py` (`-m testnet`) needs a testnet **Ed25519** key: Nautilus 1.231's spot exec client logs on over the WebSocket API, which refuses HMAC keys (`session.logon failed: HMAC-SHA-256 API key is not supported`). `SA_BINANCE_TESTNET_API_KEY` is the API key Binance shows after you register the public key; `SA_BINANCE_TESTNET_API_SECRET` is the private PKCS#8 PEM. How to generate and register the pair: `apps/sa-cli/README.md`, "Binance testnet key". Verified 2026-10-19: 1 passed in 19.23s.
- Fills in backtests come from 1m bars (`bar_execution`), not ticks or the order book; stops and limits fill within the bar's range.
- Tests of the three example strategies on synthetic data prove mechanics; the 2025 backtest results below say nothing about edge.

## Results (BTCUSDT 1m, 2025-01-01 to 2026-01-01, 525 600 bars, 10 000 USDT, 0.1% fees)

| Example | Timeframe | PnL | PnL % | Trades | Win rate | Max DD | Fees |
|---|---|---|---|---|---|---|---|
| `ema_crossover` (EMA 20/50, 5% stop) | 4h | -1 400.33 | -14.00% | 21 | 23.8% | 30.94% | 421.56 |
| `rsi_mean_reversion` (RSI14<30 and under BB(20,2); 4% stop) | 1h | -2 829.39 | -28.29% | 70 | 54.3% | 37.43% | 1 078.08 |
| `grid_10_level` (10 limit buys, 0.5%..5% below) | 15m | +287.81 | +2.88% | 121 | 62.8% | 1.03% | 144.41 |

Trades are FIFO-matched lots (the grid closes lots, not whole positions). The example parameters were set once, before running, and not tuned.

### SEP-5 (schema 2): the three examples before and after, and the two new ones
Same window, same data (`BTCUSDT` 1m, `data.binance.vision`, sha256-checked), run with the schema-1 engine (before this repository's history) and with schema 2.
The migrated examples change structure only (events instead of strings, `create`/`manage.rules`), not logic.

| Example | Window | Before (schema 1) | After (schema 2) | Difference |
|---|---|---|---|---|
| `ema_crossover` | 2025 | -1 400.33 (-14.00%), 21 trades, 23.8% wins, DD 30.94%, fees 421.56, 44 fills | same | none: the full `BacktestReport` JSON (every fill, the equity curve) is byte-identical |
| `rsi_mean_reversion` | 2025 | -2 829.39 (-28.29%), 70 trades, 54.3%, DD 37.43%, fees 1 078.08, 140 fills | same | none (byte-identical report) |
| `grid_10_level` | 2025 | +287.81 (+2.88%), 121 trades, 62.8%, DD 1.03%, fees 144.41, 158 fills | same | none (byte-identical report) |
| the three | 2026-09-01..08 (the e2e week) | ema 0 trades, rsi 0 trades, grid +21.57 (+0.22%), 10 trades | same | none (byte-identical) |
| `trend_phases` (new) | 2025 | n/a | -2 447.00 (-24.47%), 59 trades, 25.4%, DD 28.26%, fees 969.78, 118 fills | 1h EMA 20/50 cross, 4h trend filter, ATR phased stop and target. Not tuned |
| `ladder_rearm` (new) | 2025 | n/a | -1 132.69 (-11.33%), 2 645 trades, 63.5%, DD 27.30%, fees 1 590.71, 2 715 fills | 10 re-arming levels in one cycle; 0.1% fees on both legs of a 0.6% scalp eat the edge. Not tuned |
| `ladder_rearm` / `trend_phases` | 2026-09-01..08 | n/a | ladder +54.35 (+0.54%), 34 trades; trend 0 trades (its 4h EMA 50 needs more than a week) | |

Why the old three do not move: the new brain evaluates the same conditions on the same bars in the same order, places the same
orders on the same bar, and the only new pieces (exposure cap, `MIN_FRACTION`, finer-bar subscription) do not apply to them (a
document without `manage.stop`/`manage.target` subscribes to no extra bars; their order fractions sum to at most 0.95; no formula).
`tests/test_schema.py::test_examples_are_in_the_canonical_json_form_the_designer_writes` and `test_examples_f4.py` keep them running.

### Shared data dir (SEP-8)
`data.file_lock(path, timeout_s=None)` is the one lock for downloads and catalog conversions. `sa-api` reads the catalog for chart candles under the same lock with a short `timeout_s` (`LockTimeout`) and asks Binance instead when it is busy. The indicators on the chart are `indicators.make_indicator` (the constructor `IndicatorBank` uses), called from `sa_api.market`.

## How to run

```bash
cd packages/sa-engine
uv sync
uv run python -m sa_engine validate examples/grid_10_level.json            # --json prints {ok, errors:[{path, column, message}]}
uv run python -m sa_engine backtest examples/grid_10_level.json --start 2025-01-01 --end 2026-01-01   # downloads to ./.sa-data (gitignored; SA_DATA_DIR overrides)
uv run python -m sa_engine paper examples/ema_crossover.json --duration 120                            # live data, simulated fills
uv run python -m sa_engine schema -o schema/strategy.schema.json                                       # regenerate the JSON Schema
uv run pytest                              # offline, seconds
uv run pytest -m slow -o addopts=""        # 1 year x 3 strategies (downloads about 40 MB first time)
uv run pytest -m online -o addopts=""      # live paper smoke, about 3 minutes, needs network
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests
```

Tests: `tests/` uses pytest + Hypothesis (DSL round trip and evaluation invariants, indicator bounds, schema round trip against the
JSON Schema, sizing and trade-matching invariants). The default run is offline and uses a synthetic 5-day fixture catalog.

### Coverage
Default offline run: **98.8%** line+branch for `sa_engine` (420 tests, `make test`); bare `uv run pytest` at the repo root runs all four packages
in one process at 97.4% (gate 80%). Excluded with `# pragma: no cover` (about 60 lines, all needing a live node; exercised by
`tests/test_paper_online.py`): `NodeSession.run`, `NodeSession._main`, `NodeSession.prepare`, `PaperSession.sandbox_client`, the `_SandboxFees` actor, and the
history warm-up in `CompiledStrategy` (`request_bars`, `on_historical_data`). The rest of the Nautilus glue is covered offline through
`BacktestEngine`; the stop/target delivery rules of `CompiledStrategy` (finer bar first, quotes in paper mode) by `tests/test_compile_exec.py`.

SEP-5 tests, by criterion: SEP-5.1 `test_events.py` (exhaustive truth table against a transcription of v1's logic, Hypothesis);
SEP-5.2 `test_formulas.py` (random formulas evaluate to a finite number or no value, mutated and arbitrary text fails with path and
column, injection strings, no `eval`/`exec` in the source tree), `test_schema.py` (error shape); SEP-5.3 `test_phases.py` (v1 transition
order, stop before target, Hypothesis) and `test_timing.py` (the three timing rules in full backtests on hand-made bars); SEP-5.4
`test_ladder.py` (create/cancel/re-arm, exposure cap, a 10-level ladder in a backtest); SEP-5.5 `test_mtf.py` (reference built from
closed windows only, prefix and open-window perturbation invariance, `prev`); examples end to end `test_examples_f4.py`.
