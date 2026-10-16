# Superalgos v2 web dashboard

Dashboard (Vite, React, strict TypeScript, Biome). Read-only apart from an optional Sign in with Ethereum (EIP-4361) control on My power, shown only when a wallet (`window.ethereum`) exists, and the strategy designer (`#/designer`), which edits a document locally and calls the pure validate route and the loopback-only backtest route; no keys or secrets in the page, no external CDNs at runtime. Equity charts are inline SVG; the market chart (F7) uses TradingView Lightweight Charts, bundled by Vite.

## Run

```sh
bun install
bun run mock     # fixture API on http://127.0.0.1:8787 (GET, plus the POST /api/auth/* sign-in endpoints; signatures are not verified)
bun run dev      # Vite dev server; proxies /api to http://127.0.0.1:8787
```

Open the URL Vite prints. Without the mock, run the real `apps/sa-api` on port 8787 instead (`uv run sa demo` starts it with a whole network behind it and serves the built `dist/` at `/`); the types in `src/api/types.ts` are its contract, and `apps/sa-api/tests/test_contract.py` checks live responses against `mock/contract.json`. After changing the fixture or the routes run `bun run contract` to regenerate that file (a test fails until you do). Set `MOCK_PORT` to move the mock.

## Checks

```sh
bun run lint        # biome check
bun run typecheck   # tsc --noEmit (strict)
bun test --coverage # unit tests (logic in src/lib, src/api, mock/)
bun run build       # typecheck + production bundle in dist/
```

## Screens

Hash routes: `#/` Providers leaderboard, `#/providers/{pubkey}` Provider detail (equity, trades, per-tier signal timeline), `#/power` My power, `#/designer` Strategy designer, `#/backtests[/{id}]`, `#/governance`, `#/network`, `#/chart?instrument=BTCUSDT&tf=1h[&ind=ema(20)&ind=rsi(14)][&provider=<pubkey>][&bt=<id>]` Market chart.

## Layout

- `src/api/` typed client and API types
- `src/lib/designer/` pure designer logic (schema reading, edit rules, graph, errors, validator, view file, backtest flow), unit tested
- `src/lib/` pure logic (formatting, chart geometry, power split, tier timeline, tallies, routing), unit tested
- `src/components/`, `src/pages/` React views (visual, not unit tested)
- `mock/` deterministic fixture and the mock server

Contract gaps and unit assumptions are listed in `API-CHANGES.md`.

## Market chart (F7)

- **Library**: `lightweight-charts` 5.2.1 (published 2026-08-12), licence Apache-2.0 (with the NOTICE attribution link the library draws on the chart, which stays on). Pinned exactly in `package.json`, imported as an npm package and bundled by Vite: no CDN, no script tags, no TradingView service (SEP-8.7). It only draws; no Pine, no alerts, no strategy runs there.
- **Data**: candles from `/api/market/candles`, symbol search from `/api/market/symbols` (debounced 250 ms), indicator values from `/api/market/indicators` (EMA, SMA, Bollinger on price; RSI, ATR in sub-panes). The browser never computes an indicator (SEP-8.2); `src/lib/market.ts` only builds spec strings, pane placement and the crosshair readout.
- **Signals** (SEP-8.3): pick a provider; one marker per signal at its created time (buy below, sell above); hover the candle for seq, action, size, anchor status and each tier's release time. The provider page has a mini chart of its main instrument plus "Export signals" buttons (bundle JSON, CSV) that download `/api/providers/{pubkey}/export` (SEP-8.6).
- **Backtests** (SEP-8.5): pick a stored backtest; the chart switches to its instrument/timeframe, draws each fill as an entry (arrow) or exit (circle) at the fill's time and price, and puts the equity curve in a pane below.
- Theme follows `prefers-color-scheme` like the rest of the app. The chart pages use `bun run mock` data (deterministic synthetic candles) standalone.
- **Chart lifecycle**: `src/lib/chartScene.ts` binds all series and the markers plugin to the one chart object that created them, removes them only while that chart is alive, and disposes the chart once. `ErrorBoundary` wraps every page and the chart, with a Retry. `ind=` accepts `ema(20)`, `ema%2820%29` and comma lists (`ind=ema(20),rsi(14)`).

## Strategy designer (F4)

`#/designer` is a node-graph editor over the same typed strategy JSON the engine runs (`packages/sa-engine/schema/strategy.schema.json`, schema version 2).

- **Library**: `@xyflow/react` (React Flow) 12.12.0, published 2026-09-24, licence **MIT**. Pinned exactly in `package.json`, bundled by Vite (CSS imported in `main.tsx`): no CDN, no script tags. `fast-check` 4.10.2 (MIT) is a dev dependency for the property tests.
- **Tree**: strategy, then the stages (trigger on/off, take position = open, manage with stop and target phases and rules, close), then events (OR), situations (AND), conditions; orders carry their create and cancel events and rate / size formulas; each phase has a level formula, a next-phase event and move-to-phase events. The `stages` wrapper of the schema is not drawn.
- **The schema drives the editor.** `src/lib/designer/schema.ts` reads the JSON Schema (it is imported from `packages/`, not copied): which properties are nodes, which are fields, the allowed lists (`minItems` / `maxItems`), required fields (not removable), defaults and the `x-dsl` expression kinds. `rules.ts` turns that into the menu: add a child only where a property is absent or a list is under `maxItems`, remove only what is optional or above `minItems`, reorder list items. The only hand-kept facts are labels, a few seed values (`SEEDS`) and `KEY_ORDER`, the engine's field order, which the exported schema does not carry (it sorts keys); a test fails when a model gains a field that `KEY_ORDER` does not list.
- **Properties panel**: scalar fields from the schema (enum, number with range, boolean, text; `goto.to` picks one of the list's phases); formulas and conditions get a text editor. The whole document is sent to `POST /api/strategy/validate` 400 ms after the last edit; each `{path, column, message}` is attached to the longest node path that prefixes it, drawn in red on the node (and a marker "problem inside" on its ancestors), listed under Problems, and shown in the editor as a caret under the failing column.
- **Files**: open an example (the five in `packages/sa-engine/examples/`, bundled) or a local `.json`; Save downloads the document in the engine's canonical form (`JSON.stringify(doc, null, 2)` plus a newline, which equals `json.dumps(indent=2, ensure_ascii=False)` for these documents; key order is kept as opened, new keys are inserted in the engine's order). Opening and saving an unchanged example is byte-identical (tested for every example against the file's bytes). **A document that does not validate (or could not be validated) is never saved silently**: Save asks, and the file is named `<name>.invalid.json`.
- **Layout is not in the document** (SEP-5.10). Node positions you drag are kept in `localStorage` under `sa-designer-view:<strategy name>` and can be downloaded or loaded as a separate `<name>.view.json` (`{view_version, strategy, positions}`). Positions follow their nodes when a list is reordered, inserted into or removed from. Nodes without a saved position are laid out as a tidy tree.
- **Backtest** (SEP-5.7): instrument, timeframe (the document's own) and dates, `POST /api/backtests/run`, then `GET /api/backtests/jobs/{id}` every second until the job leaves `running`; a finished job links to `#/chart?instrument=...&tf=...&bt=<id>`. A 403 shows the server's loopback reason, 409 offers to follow the running job, 422 puts the document's problems on the tree.
- **Mock**: `bun run mock` also answers the validate, run and job routes (`mock/designer.ts`). Its validator is the schema structure plus a small expression checker (parentheses, unknown names, dangling operators, with columns), a stand-in for the engine's parser; the real API is the authority.
- **Undo/redo** (toolbar, Cmd/Ctrl+Z), Delete removes the selected node when the schema allows, Cmd/Ctrl+S saves. Right click a node for its menu. The edit history is lost on reload (no draft autosave yet).
- **Layout switch**: Radial (default; v1 icons, per-node spread, distance and pin relative to the parent, collapse) and Cards (the editor view above with free positions); a view setting only, never in the strategy. See `docs/designer.md`.
- **Performance** (SEP-5.11): `tests/designer-page.test.tsx` renders a generated 230-node document and applies one edit, each asserted under 100 ms in happy-dom (about 35-65 ms and 15-35 ms locally); real-browser interaction latency is measured separately.
