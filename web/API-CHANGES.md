# API changes and assumptions (web needs vs. the contract)

Proposed additions, marked so the real `apps/sa-api` can match. The UI degrades without the optional ones.

1. `GET /api/backtests` list items must include `id` (detail path uses it). Detail returns the same shape. Required.
2. `GET /api/me` should add optional `balances: [{chain, balance}]` so "My power" can show token power across BSC / Ethereum / zkSync. Optional; the chips are hidden when absent.
3. `GET /api/providers/{pubkey}/record` shapes left open by the contract, assumed by the UI:
   - `gaps: [{after_seq, missing}]`
   - `unrevealed_commitments: [{id, seq | null, committed_ms}]`
4. Units assumed: `t`, `open_t`, `close_t`, `created_ms`, `anchored_ms`, `at_ms` are epoch milliseconds; `win_rate`, `completeness`, `anchor_coverage` are fractions 0..1; `pnl_pct`, `max_dd_pct` and `verification.anchored_pct` are percent numbers (0..100); `start`/`end` are `YYYY-MM-DD`; `deadline` is ISO 8601; `power_backing` and `token_power` are SA units (not wei).
5. `release[].tier` is an index into the provider's `tiers` array as returned by `/api/providers`. `anchored_ms` may be `null` (unanchored).
6. Leaderboard rows carry only `record_summary`, so badges there derive `chain_complete` from `completeness >= 1` and `anchored_pct` from `anchor_coverage`. `signatures_ok` is not in the summary; the list shows it as true. Proposal: add `signatures_ok: boolean` to `record_summary` (or `verification` to each provider row). The detail page uses the real `verification` object.
7. `chains[].block` may be `null` when the chain is down.

## Wave 3 web fix (2026-10-13): additions the backend must match

The UI tolerates their absence (time scale is then inferred from `release[]`, `observed` reads as false, `signed_in` as false), but the mock and `mock/contract.json` carry them, so `apps/sa-api/tests/test_contract.py` will require them.

8. `GET /api/network` adds `time_scale: number`: the relay's `time_scale` (1 live, `1/speed` in a demo replay; the demo is `1/1440`). The Provider page states "Demo replay: Nx faster than real time" when it is not 1.
9. `release[]` items add `observed: boolean`: `true` when `at_ms` is a follower's real receipt time (ledger), `false` when it is computed from the schedule. `release[]` must hold one entry per tier of the rule set (sa-api already does); the UI draws missing tiers from the schedule anyway. `at_ms` stays wall clock (scheduled offset = `delay_fraction x timeframe_s x time_scale`).
   - Timeline semantics: each marker sits at the scheduled offset `delay_fraction x timeframe` in market time over a track that spans `max(timeframe, largest delay)`, so replay scaling never collapses the markers. The wall-clock receive time is in the marker tooltip. An `observed` receipt is ringed and, when it differs visibly from the schedule, also drawn as a tick at its market-time-equivalent position (`(at_ms - created_ms) / time_scale`).
10. `GET /api/me` adds `signed_in: boolean`: `false` for the server's demo identity (no session), `true` when a wallet session cookie is present, in which case `sa_address` is that address and every figure is that address's.
11. Sign in with Ethereum (EIP-4361). These are the only non-GET routes the dashboard uses; the session lives in an `HttpOnly; SameSite=Strict; Path=/` cookie (`sa_session`), never readable by the page.
    - `POST /api/auth/nonce` body `{address}` (0x-prefixed EVM address; the server needs it because an EIP-4361 message embeds it) returns `{nonce, message}`. `message` is the complete EIP-4361 text the server built (domain, address, statement, URI, Version 1, Chain ID, Nonce, Issued At). The nonce is single use. 400 `{error}` on a malformed address.
    - `POST /api/auth/verify` body `{message, signature}` (`signature` is the `personal_sign` result, 0x + 130 hex digits) returns `{address}` and sets the cookie. 401 `{error}` for an unknown/reused nonce or a signature that does not recover to the message's address; 400 for a malformed body.
    - `POST /api/auth/logout` returns `{}` and clears the cookie (`Max-Age=0`).
    - The page calls `personal_sign` with params `[0x-hex(utf8(message)), address]`.
    - CORS is not involved: the dashboard is served by the API on the same origin (`--web-dist`). Docs that say "GET only" need the exception for `/api/auth/*`.
12. Leaderboard order: providers with no trades sort last in every direction (UI only, no contract change).

## 2026-10-13 (security review)

- `record_summary` adds `excluded`, `late_anchor`, `unanchored` (integers). A published signal that is not anchored on time is scored as a miss, not left out; the provider page shows the counts. `anchor_coverage` (and `verification.anchored_pct`) now count on-time anchors only.

## 2026-10-13 (F7 market chart)

The mock and `mock/contract.json` implement ARCHITECTURE.md "Market chart API (F7)" exactly; what the contract left open and the chart assumes:

13. `instrument` spelling: `/api/market/*` and `symbols[].instrument` use the bare Binance symbol (`BTCUSDT`), while signals and backtests say `BINANCE:BTCUSDT`. The chart strips the prefix up to the last `:` and upper-cases (`marketSymbol`), so either spelling works; the API should accept the bare symbol in `instrument=` and may accept the prefixed one.
14. `GET /api/backtests` list items and `GET /api/backtests/{id}` both carry `timeframe` (one of `1m 5m 15m 1h 4h 1d`) and `fills: [{t, side, price, qty}]` in time order (the contract says detail adds them; the mock puts them on both, the web type requires both). `qty` is positive; `side` is `buy`/`sell`.
15. Fills have no `entry`/`exit` flag. The chart infers it by walking the signed position (buy +qty, sell -qty): moving away from flat is an entry, moving toward flat an exit, crossing zero an entry. Proposed optional `kind: "entry" | "exit"` per fill if the engine knows it; the web would then use it.
16. Backtest `equity[]` times must lie inside `start`..`end` (the mock had them in September for January backtests; fixed) so the equity pane aligns with the candles.
17. Candles: `end_ms` is inclusive of the candle that contains it (last candle opens at `floor(end_ms/tf)*tf`), result ascending, `t` = open time. Omitted `end_ms` = now. The backtest overlay asks for `end_ms` = day after `end` and `limit` up to 1000 (so a 1m backtest longer than ~16 h shows only its last 1000 candles).
18. Indicators: `end_ms`/`limit` select the same window as candles; the response has one point per candle once the indicator is warmed up, and series are warmed up on candles before the window (first window point has a value). A spec that cannot be parsed gives 400 `{error}` naming it. Spec names: `ema(n)`, `sma(n)`, `bbands(n,k)` (series of `{t, upper, mid, lower}`), `rsi(n)`, `atr(n)`. Series are keyed by the spec string exactly as sent (no spaces).
19. Errors: unknown instrument 404 `{error}`; bad `tf`/`end_ms`/`format` 400 `{error}`.
20. Export: `format=bundle` serves `sa_core.Bundle` JSON (`Content-Type: application/json`), `format=csv` serves `text/csv; charset=utf-8`; both with `Content-Disposition: attachment; filename=...`. CSV columns (mock): `seq,id,created_ms,created_iso,anchored_ms,instrument,side,action,size_fraction,private,releases` with `releases` = `tier:at_ms` pairs joined by `;`. The mock bundle has the Bundle shape but placeholder ids/signatures: only the real API's bundle passes `sa verify`. In `contract.json` the two export entries hold the file text, not a JSON object.
21. Provider-page mini chart: signal times are wall clock. In a compressed demo replay the signals sit minutes apart, so the mini chart picks a finer timeframe than the provider's own (`fitTimeframe`) so markers are not all on one candle. It needs candles for those times (real Binance has none for a replayed future; the real API must serve something for the window or the chart shows the API's error).
22. `apps/sa-api/tests/data/web-contract.json` is a copy of `web/mock/contract.json` that the Python contract test compares; it must be refreshed (and the new keys implemented) by the API side.

## 2026-10-13 (F4 strategy designer)

The designer needed no API or schema change: everything it offers comes from `strategy.schema.json` and the three existing routes. Notes for the owners of those:

15. **Mock routes, not in `mock/contract.json`.** `bun run mock` now answers `POST /api/strategy/validate`, `POST /api/backtests/run` and `GET /api/backtests/jobs/{id}` (`mock/designer.ts`). They are deliberately not added to `contractDump()`: `apps/sa-api/tests/test_contract.py` requires `web/mock/contract.json` to equal its copy `tests/data/web-contract.json` and checks the live API key for key, and that copy lives outside `web/`. `bun run contract` was run and left `contract.json` unchanged. If you want the three routes in the contract, add them to `contractDump()`, re-run `bun run contract`, copy the file, and extend `check_units` and the live test.
16. **Field order.** The exported JSON Schema sorts every model's properties alphabetically, but byte-identical saves (SEP-5.6) and tidy new nodes need the engine's declaration order (`id, type, size_fraction, ...`). The designer carries that order in `KEY_ORDER` (`src/lib/designer/schema.ts`, guarded by a test that its keys equal the schema's). Cleaner: have `python -m sa_engine schema` emit the declaration order (e.g. keep `properties` in declaration order, or add an `x-order` list per model) so the table can go.
17. **Cross references.** `Goto.to` is "an id of another phase of the same list" only in prose; the designer special-cases it (a select of the sibling phases). An annotation like `"x-ref": "phases[].id"` would make that schema-driven too.
18. **Timeframes.** The strategy schema allows `30m` and `2h`; the dashboard chart (`Timeframe` in `src/api/types.ts`) and `/api/market/*` list `1m 5m 15m 1h 4h 1d`. "Open on chart" sends `tf` only when the chart supports it and relies on `bt=` to switch the chart otherwise; a `30m` report's `timeframe` in `GET /api/backtests/{id}` is outside the web type until the market API supports it.
19. **Number text.** Saves use JavaScript's number formatting, the engine's files use Python's. They agree for every example (checked byte for byte) and for ordinary values; they differ for `1.0` (JS writes `1`) and for exponents (`1e-7` vs `1e-07`). Documents written by the engine from user input with such numbers would not round-trip byte-identically.

## Per-party views (2026-10-15): the dashboard shows each party its own data

21. `GET /api/providers/{pubkey}/signals` items drop `release[]` and add `status`: `"out"` (signed, time-stamped and on the relay) or `"waiting_for_time_stamp"`. Nothing about followers is on the provider's pages.
22. `GET /api/me/signals?limit=` is the viewer's own follower ledger (`me_home`), newest first: `{id, provider, seq, received_ms, created_ms, tier, delay_ms, outcome, reason, instrument, side, action}`. `outcome` is `executed`, `rejected` (with `reason`) or `pending`. `tier` and `delay_ms` are the follower's own tier and the wall-clock delay it schedules; null when the provider's rule set is unknown or the follower is not entitled. 404 without `me_home`; empty for a wallet session of another address.
23. `GET /api/network` adds `dev_views: boolean`. Only when true does the API serve `GET /api/dev/providers/{pubkey}/releases` (signals with `release[]`, incl. other followers' observed receipts), and the dashboard shows its "Dev views" page with the per-tier timeline. Config `"dev_views": true` turns it on (`sa demo` does; a production deployment does not).
24. The CSV export drops its `releases` column.
25. Every time on these pages renders in the viewer's local zone with the UTC offset labelled, e.g. `17:35 (UTC+1)`; the provider chart reaches the current time when `time_scale` is 1 and keeps the replay window (last signal plus 20 bars) otherwise.
