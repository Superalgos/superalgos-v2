# sa-api

The read-only HTTP API behind the dashboard (`import sa_api`, FastAPI). It serves exactly the contract of
`web/src/api/types.ts` from real data and, with `--web-dist web/dist`, the built dashboard at `/`, so the whole UI runs from one port.

```sh
uv run python -m sa_api --config api-config.json --web-dist web/dist     # 127.0.0.1:8787
uv run sa demo                                                           # a whole network behind it
```

- Binds `127.0.0.1:8787` by default. GET only except `POST /api/strategy/validate` and `POST /api/backtests/run` (see "Strategy designer" below) and `POST /api/auth/nonce|verify|logout` (Sign-In with Ethereum, EIP-4361: the
  wallet signs a server-built message, the session is the recovered address in an `HttpOnly; SameSite=Strict` cookie, nonces are
  single use and expire in at most 10 minutes; `/api/me` then shows that address's real power and allocation, else the demo
  identity). Every other verb answers 405. Behind a proxy set `siwe_domain` (and `siwe_uri`) in the config. It holds no key, publishes nothing, and reads
  the relay as an anonymous client (so it never sees a private signal, only commitments, and a signal only once the
  slowest public tier does).
- Data: rule sets, allocations, signals, commitments, anchor attestations and proposals from the relay; track records from
  `sa_core.trackrecord` over a bundle built from those events plus exported bundles (reveals of private signals); backtests from
  `BacktestReport` JSON files; token power and chain status from the balance oracle (the fixture file in the demo, the real BSC,
  Ethereum and zkSync registry otherwise); optional follower ledgers for real receipt times. See `ApiConfig` for every field.
- Errors are `{"error": "..."}` with a status code; an unknown provider is a 404 that names it.
- Tests: `uv run pytest apps/sa-api` (an in-process relay seeded with signed, TSA-anchored events), including the contract test
  against the web mock. Interactive docs at `/api/docs`.

- Security: `POST /api/auth/nonce` is limited to 30 requests per client IP per minute (`nonce_rate_per_min`). Behind a TLS-terminating
  proxy start with `--proxy-headers` (and `--forwarded-allow-ips`, default `127.0.0.1`): otherwise the app sees plain http, the real client
  IP is lost, and the session cookie is `Secure` only if `siwe_uri` is an https URI. Allocations (9332) and rule sets (37318) count only
  from a delegated, unrevoked device key (kind 9334 revocations are read with the other kinds); a rule set is judged at the current time;
  proposals need a delegated proposer holding `min_proposer_power` (default 1,000 SA). `record_summary` carries `excluded`, `late_anchor`
  and `unanchored` counts, and `anchor_coverage` counts on-time anchors only. Tests: `tests/test_security.py`.

## Market chart (F7, read-only)

All GET, no API key anywhere (Binance public endpoints only), nothing executes on a third-party platform. Each `/api/market/*`
request counts against `market_rate_per_min` (default 120) per client IP: beyond it, 429 with `Retry-After`.

| Route | Answer |
|---|---|
| `GET /api/market/symbols?q=&limit=` | `[{instrument, base, quote}]` from Binance spot `exchangeInfo` (TRADING and spot-allowed only), cached `market_symbols_ttl_s` (1 h; a stale list is served if a refresh fails). `instrument` is the bare symbol (`BTCUSDT`). `q`: at most 20 characters, matches symbol or base (exact, prefix, contains; USDT pairs first). `limit` 1..100, default 50. |
| `GET /api/market/candles?instrument=&tf=&end_ms=&limit=` | `[{t, o, h, l, c, v}]` ascending, `t` = open time (ms). `instrument` is `BTCUSDT` or `BINANCE:BTCUSDT`; `tf` is required, one of `1m 5m 15m 1h 4h 1d`; `limit` 1..1000 (default 500). The window is the `limit` candles opening at or before `end_ms` (inclusive of the candle that contains it; default now, which includes the forming candle). |
| `GET /api/market/indicators?instrument=&tf=&end_ms=&limit=&spec=` | `{"<spec>": [{t, value}] or [{t, upper, mid, lower}]}` for the same window. `spec`: up to 8 of `ema(n) sma(n) rsi(n) atr(n) bbands(n[,k]) donchian(n)` (a price source may lead: `ema(high,20)`), at most 200 characters, keyed as sent without spaces. |

- **Candle sources.** The local Nautilus catalog first (`<data_dir>/catalog`, where `data_dir` is the config field, else `$SA_DATA_DIR`
  or `./.sa-data`): 1m bars aggregated on the UTC grid Binance uses, but only when every month the window touches is marked converted,
  the window is closed (not the forming candle) and at most 300 000 minutes, and the data-dir lock (the one downloads and catalog
  conversions hold) is free within `market_lock_wait_s` (1 s). Anything else goes to Binance public klines (`/api/v3/klines`, paged
  1000 at a time). The built-in catalog only holds BTCUSDT and ETHUSDT; every other symbol is Binance. Values equal Binance's klines
  for the same window (tests against a recorded day, for every timeframe, on both paths).
- **Cache.** Windows that are closed are cached 10 minutes, windows with the forming candle `market_candle_ttl_s` (2 s), at most 256
  entries; `Cache-Control` says the same.
- **Indicators are the engine's.** The spec is parsed by `sa_engine.dsl` (its argument and period checks, 1..1000) and each series runs
  through `sa_engine.indicators.make_indicator`, the constructor `IndicatorBank` uses inside backtests and paper trading: there is no
  second implementation, and `tests/test_market.py` compares the API to `IndicatorBank` for EMA, SMA, RSI, Bollinger, ATR and
  Donchian (SEP-8.2). The indicators run over the window preceded by a warm-up of `max(period + 1, min(3 x longest period, 1000))`
  candles that is not returned, so the first window point already has a value when history exists. Before the listing starts
  (or where the catalog and Binance have nothing) the series start later.
- **Errors** are `{"error": "..."}`: 400 for a bad `tf`, `limit`, `end_ms`, `instrument` spelling, `spec` or `format` (the message says the value and what to pass),
  404 for a symbol Binance does not know, 429 beyond the rate limit, 502 when Binance cannot be reached or answers nonsense, 503 when Binance
  rate-limits this server.
- Config: `binance_base_url`, `market_rate_per_min`, `market_symbols_ttl_s`, `market_candle_ttl_s`, `market_lock_wait_s`, `data_dir`.
- Tests: `uv run pytest apps/sa-api/tests/test_market.py` (offline, against a recorded Binance day, `tests/data/binance_btcusdt_20260901.json`);
  one `online` test hits the live Binance public API: `uv run pytest apps/sa-api/tests/test_market.py -m online -o addopts=""`.

## Backtest fills and signal export

- `GET /api/backtests` and `GET /api/backtests/{id}` carry `timeframe` and `fills: [{t, side, price, qty, kind}]` from the stored
  `BacktestReport` (`sa backtest -o` / `python -m sa_engine backtest --json` now write `fills`, every executed order in time
  order; reports written before F7 have none and serve `fills: []`, re-run the backtest). `kind` is `entry` when a fill moves the
  signed position away from flat (or across it) and `exit` when it moves toward flat. `instrument` is the report's (`BINANCE:BTCUSDT`).
- `GET /api/providers/{pubkey}/export?format=bundle|csv` (default `bundle`), `Content-Disposition: attachment`.
  - `bundle`: `application/json`, the provider's signed history as `sa_core.Bundle` (signal and commitment events, reveals the API holds,
    anchor proofs), canonical JSON. `sa verify <file>` accepts it (`bundle is complete`, tested by running `sa verify`; a bundle with a signal cut out is refused).
  - `csv`: `text/csv; charset=utf-8`, one row per signal the API can show, oldest first. Columns: `seq,id,created_ms,created_iso,anchored_ms,
    instrument,side,action,size_fraction,private,releases` (the dashboard's documented set; `releases` is `tier:at_ms` pairs joined by `;`)
    then `order_type,limit_price,stop_price,timeframe_s`. A private signal appears only once its reveal is in `bundles_dir` (exactly as in `/signals`);
    the API never has plaintext it was not given.

## Strategy designer (F4)

The only non-GET routes besides sign-in (`ARCHITECTURE.md`, "Dashboard API", says why they are allowed):

- `POST /api/strategy/validate`: body = a strategy document (schema version 2), answer `{ok, errors: [{path, column, message}]}`.
  Pure: it reads and writes nothing, so it works on any bind address. `column` is the 0-based column inside a condition or formula, `null` elsewhere.
- `POST /api/backtests/run`: body `{strategy, instrument, timeframe?, start, end}` (dates `YYYY-MM-DD`). **403 unless the API is bound to loopback**
  (`--host 127.0.0.1`), has no proxy in front, and the request's `Host` and `Origin` are this machine. One job at a time (409), killed after
  `backtest_timeout_s` (default 900 s), window at most `backtest_max_days` (400). Answers `202` with the job; poll
  `GET /api/backtests/jobs/{id}` (`running` -> `done` | `failed` | `timeout`). A finished report is written to `backtests_dir/<job id>.json`
  and is at once in `GET /api/backtests` with its fills, which is what the chart overlay draws. Needs `backtests_dir` (and `data_dir`, default
  `$SA_DATA_DIR` or `./.sa-data`, where missing Binance months are downloaded by the engine).
- Tests: `uv run pytest apps/sa-api/tests/test_f4_designer.py`.
