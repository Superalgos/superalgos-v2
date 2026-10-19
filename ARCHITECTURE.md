# Superalgos v2 MVP — architecture contract

The contract every package builds against. The SEPs in `seps/` hold what done means; this file holds the seams. Change a seam here first, then in code.

## Goal of the MVP

One command brings up a working network on a laptop: a provider runs a strategy that was backtested on real Binance data, its signals are signed, anchored and published to a tiered relay, two followers at different power tiers receive them at their scheduled delays and paper-trade them, and a dashboard shows each provider's verifiable track record recomputed from signed history.

## Repository layout

```
superalgos-v2/
  ARCHITECTURE.md  AGENTS.md  README.md  LICENSE (Apache-2.0)
  seps/                    # Superalgos Enhancement Proposals: the criteria that define done
  pyproject.toml            # uv workspace root (Python 3.12+), shared ruff/mypy/pytest config
  packages/
    sa-core/   (import sa_core)    models, crypto, verification, chains, power, anchors, track record, bundles, governance
    sa-engine/ (import sa_engine)  strategy schema + DSL, Nautilus compile, data, backtest, paper trading, provider + follower bots (`sa_engine.network`; imports sa_core, never the reverse)
  apps/
    sa-cli/    (import sa_cli)     `sa` command
    sa-api/    (import sa_api)     HTTP API for the dashboard (FastAPI): read-only data, plus Sign-In with Ethereum under `/api/auth/*` and the two strategy-designer routes (`/api/strategy/validate`, `/api/backtests/run`)
  relay/       Go module, khatru tiered relay (standalone binary)
  web/         TypeScript dashboard (bun, Vite, React, strict TS, Biome)
  deploy/      docker-compose.yml, Dockerfile and relay config for the demo network in containers
  tests/e2e/   end-to-end tests across processes
```

Python never imports Go or TS; they talk over Nostr (WebSocket) and HTTP only.

## Wire contracts (Nostr)

| Kind | Name | Replaceable | Signed by | Notes |
|---|---|---|---|---|
| 9317 | Signal | no | device key with `sa-delegation` tag | public content, or NIP-44 ciphertext for private signals |
| 9318 | Commitment | no | device key | `sha256(signal plaintext)` only; for private/delayed reveal (SEP-3.28) |
| 9332 | Power allocation | no (history kept; highest valid nonce wins; an equal-nonce pair resolves to the smallest event id, in sa-core and in the relay) | device key with exactly one valid `sa-delegation` tag whose EVM address equals `content.address`; content carries EIP-191 sig by the SA address | whole split in basis points: `{"governance": bps, "providers": {pubkey: bps}}`, sum ≤ 10000. Only events signed by a device key that address delegated count (and, for a private provider, only such a key is the NIP-44 recipient), so a stranger cannot re-wrap someone else's allocation under their own key |
| 9334 | Revocation | no | any key (the EIP-191 signature in the content is the authority) | `{v, device_pubkey, effective_at, address, sig}`, `sig` = EIP-191 by `address` over `sa-revocation:{device_pubkey}:{effective_at}`. From `effective_at` that device key no longer speaks for that address: followers, the API and the relay reject its signals, allocations and proposals. Anyone may publish it, so a stolen key can be revoked from any machine |
| 37318 | Provider rule set | yes, `d` = provider pubkey | provider device key + EIP-191 by provider SA address | tiers `[{min_power, delay_fraction}]` or `{"private": {min_power}}`; `timeframe` bound here (SEP-3.40) |
| 9330 / 9331 | Proposal / Vote | no | device key + delegation | per prototype 4. A proposal's delegating address must hold at least `min_proposer_power` whole tokens at the snapshot (default 1,000; `sa gov tally --min-proposer-power`, `ApiConfig.min_proposer_power`), else it is ignored |
| 1040 | Anchor attestation | no | anyone | NIP-03 style; carries OTS / TSA / BSC proof for an event id. Content limit 64 KiB (a 3-token TSA proof is about 27 KB; other kinds stay at 16 KiB) |

- **Tag names:** custom tags never reuse a NIP-reserved name. The delegation tag is `["sa-delegation", evm_address, expires_at, sig]`, where the signature is EIP-191 over `sa-delegation:{nostr_pubkey_hex}:{expires_at}`. `sa identity --demo --days` caps a delegation at 90 days; `sa revoke` publishes a kind 9334 for a lost key.
- **Signal chain:** each signal content carries `seq` (starting at 1, per provider strategy) and `prev` (the previous signal's event id, or null). This makes gaps detectable (SEP-3.15).
- **Signal content fields:**
  - `v`, `strategy_id`, `seq`, `prev`
  - `instrument` (`BINANCE:BTCUSDT`)
  - `side` (`buy`/`sell`), `action` (`open`/`close`/`adjust`)
  - `size_fraction` (0–1], `order_type` (`market`/`limit`/`stop`; `stop` requires `stop_price`), `limit_price?`, `stop_price?`
  - `timeframe` (seconds), `created_ms`

## Strategy documents (`packages/sa-engine`, schema version 2)

A strategy is one typed JSON document (`schema_version: 2`, JSON Schema in `packages/sa-engine/schema/strategy.schema.json`, regenerated by `python -m sa_engine schema`; a test fails when the file drifts). Version 1 documents are refused with the way forward; there is no compatibility layer. The field reference is the JSON Schema; the timing rules are below.

- **Events** are v1's logic: a *situation* is an AND of boolean conditions, an *event* is an OR of situations, the first passing situation wins and its name is the audit label (SEP-5.1).
- **Formulas** (order `size` and `rate`, phase `level`) are numeric expressions of the same safe DSL as conditions: arithmetic, `min max abs coalesce`, prices, indicators, the position fields `entry_price bars_in_position ...` and the level in force (`stop_price`, `target_price`). Nothing is ever passed to `eval`/`exec` (a test scans the engine for them) (SEP-5.2).
- **Manage stage:** ordered *phases* for the stop and for the target, each with a formula and `next` / `goto` events; per bar: `next`, else `goto`s in order, at most one move, then the current phase's formula (SEP-5.3).
- **Orders** have `create` and `cancel` events and can `rearm`, so a ladder is one strategy whose filled levels are placed again; total exposure is capped at 100% of equity (SEP-5.4).
- **Multi-timeframe:** `ema(close, 20, "4h")`, `close("1d")`. Only closed higher-timeframe bars are read (SEP-5.5).
- **Errors** are `{path, column, message}`: the JSON path of the failing string and, inside a condition or formula, the 0-based column. The same shape is what `POST /api/strategy/validate` returns.
- **Long-only spot** stays; the signed-signal contract (`OrderIntent`, the signal fields above) is unchanged. A managed stop or target exit is a plain market `close` intent, so followers need nothing new and never hold a stale stop they cannot cancel.

### Timing rules

Enforced by `packages/sa-engine/tests/test_timing.py` and `test_phases.py` on hand-made one-minute data.

1. **Decisions are made on a bar close.** Events, sizes, rates, phase moves and phase formulas are evaluated when a strategy bar closes, never in between.
2. **A decision on bar N never meets bar N's own range.** Resting orders (limit, stop) are matched against bar N+1 onward. A market order fills at the first price after the close of N (a backtest: N's close, which is N+1's open). Stop and target levels computed at the close of N are compared only with bars that close after N.
3. **The stop is checked before the target.** If one bar touches both, the stop wins. The levels are watched on the finest bar available (1m in a backtest, delivered before the strategy bar that closes at the same instant; quotes in paper mode); a hit sells the whole position at market, decided at that bar's close.
4. Per-order `take_profit_pct` / `stop_loss_pct` brackets rest at the venue from the fill and follow the simulated venue's order for a bar touching both (target first). Use phases when the tie matters. A phased stop starts at the first bar close after the entry fills.

## Power and tiers

- **Token power** = Σ SA balance over the chain registry (`chains.toml`: BSC, Ethereum, zkSync; each entry is id, rpc, contract, decimals).
- **Power allocated to provider P** = token power × bps(P) / 10000, from the newest valid allocation. It takes effect D after its anchored time (SEP-3.35; D is configurable, default 7 days, and 0 in demo mode).
- **Two clocks for D (SEP-3.35).** sa-core counts D from the allocation's *anchored* time (an upper bound on its publication time, so it lags the relay's receipt by the anchor latency: seconds for a TSA, hours for OpenTimestamps). The relay cannot verify anchor proofs and counts D from *receipt*. With equal D the relay would put an allocation in force earlier than sa-core. The relay therefore takes `core_delay` (sa-core's D) and `max_anchor_latency` and refuses to start unless `allocation_delay >= core_delay + max_anchor_latency` (defaults 192h = 168h + 24h): it can be later than sa-core by up to the anchor latency, never earlier. Demos run D = 0 on both sides and set all three to `0s`.
- **Tier with P:** the highest tier in P's rule set whose `min_power` is ≤ allocated power. If none matches, the client falls into the last public tier, or gets nothing if P is private.
- **Release time:** relay receipt time + `delay_fraction × timeframe`.

## Anchors

`sa_core.anchors.Anchor` is the protocol: `stamp(digest) -> Proof`, `verify(proof, digest) -> TimeBound(not_after, kind)`.

- **Implementations:**
  - `TsaAnchor` (kind `tsa`, **signals**): RFC 3161 tokens from at least k=2 distinct organisations over a Merkle root of event ids, verified offline against a versioned pinned-roots bundle (`sa_core/tsa_trust/`). Stamping is free and takes seconds. The stamper batches, contacts each TSA at most every 15 s, and stops on HTTP 429 or a refusal. Operators are counted by signing organisation (DigiCert = QuoVadis, Sectigo = Entrust), never by URL.
  - `OtsAnchor` (kind `ots`, **votes and allocations**): Bitcoin-anchored, hours of slack, no per-event cost, long-term backstop.
  - `BscCommitAnchor` (kind `bsc-commit`, optional second signal anchor; simulated chain in tests). Nothing in the MVP sends a real transaction.
- **Kind map:** `anchors.verifier_map(*anchors)` builds the kind -> verifier map that `trackrecord.compute(verifiers=...)` and the governance tally take.
- **Anchored time** = the earliest `not_after` across the proofs an event carries. For a TSA proof `not_after` is the k-th smallest per-organisation bound (genTime + accuracy), so k-1 lying TSAs cannot backdate.
- **No anchor:** the event counts as unanchored. It never enters a tally as if on time. In a track record a published signal that is unanchored, or anchored later than the tolerance, is scored as a miss, never dropped (see Track record).

## Track record

- **Input:** `sa_core.trackrecord.compute(bundle, candles)`.
  - `bundle` = ordered signed signals + commitments + reveals + anchor proofs.
  - `candles` = public 1m klines.
- **Output:** a deterministic JSON report: PnL, win rate, max drawdown, trade count, unrevealed commitments counted as misses, completeness (gaps), anchor coverage, gross exposure.
- **Determinism:** the same input gives byte-identical output.
- **Late or missing anchors are never a way to hide a trade (cherry-picking).** A published signal (in the bundle, or on a relay) that is unanchored or anchored later than `tolerance_s` is replayed like any other signal, and the round trip it belongs to is a miss: it counts as a loss in `win_rate` whatever it earned, and contributes at most 0 to PnL (a gain is forfeited, a loss stays). This is the same rule as an unrevealed commitment (SEP-3.12). The fill never depends on the anchor time, so withholding or delaying any subset of anchors can never raise `pnl` or `win_rate` (a Hypothesis test states it). `signals.unanchored`, `signals.late_anchor`, `signals.delegation_expired`, `signals.penalized` and `signals.penalized_trades` are in the report; `anchor_coverage` counts on-time anchors only; the API's `record_summary` carries `excluded`, `late_anchor` and `unanchored`, and the dashboard shows them.
- **Exposure.** Total open notional is capped at 1.0 of capital: an `open`/`adjust` that would exceed it is rejected (`signals.excluded.exposure_cap`). The report gives `max_gross_exposure` and `pnl_per_exposure` (PnL divided by the peak), so stacking size cannot inflate a percentage return.

## Execution

NautilusTrader (LGPL-3.0, unmodified dependency) runs both backtest and paper trading. Paper trading means live Binance market data with Nautilus's simulated (sandbox) execution: no exchange keys and no real orders, ever.

- **Provider bot:** runs a compiled strategy; every order intent becomes a signal.
- **Follower bot:** subscribes, verifies (signature, delegation, chain, anchor), then maps `size_fraction` × its allocated capital to a paper order.

## Network bots (`sa_engine.network`, driven by `sa`)

- **Provider bot:** one signed signal per order intent, journaled (fsync) before it is sent, so the `seq`/`prev` chain survives a restart. A private provider publishes a 9318 commitment, then one NIP-44 ciphertext per entitled follower (entitled = the allocation in force on the relay, times token power, meets `private.min_power`). Every chain event (public signal id, or commitment id) is stamped in TSA batches (>= 15 s) and the proofs go out as 1040 attestations. The bot holds a device key and a delegation, never the SA key.
- **Follower bot:** AUTH with the device key + delegation, publish the allocation (9332) once, subscribe, verify, execute on paper, keep a SQLite ledger (receive time, checks, fill). A signal file takes the same path (SEP-3.29).
- **Delivery order.** The relay keeps one lane per (provider, delay) and drains it with a single goroutine in relay receipt order, so signals that reach a subscriber at the same moment (an entry and its stop published in one millisecond) arrive in the order the relay received them, in every tier. Receipt order is a counter assigned under the relay's write lock and persisted with the release schedule, so a restart re-arms pending releases in the same order. The follower still holds a signal that jumps ahead of its chain head for `reorder_wait` (default 1 s) and executes in `seq` order, a predecessor that never comes is a gap, the signal is then rejected and the head moves: defence in depth for a lossy network or several relays, no longer needed for correctness against one relay.
- **Fixture balances:** `{"0xaddress": "whole tokens"}`, one JSON file read by the relay (`kind = "fixture"` chain) and by Python (`sa_engine.network.fixture`), so demo identities hold different power and land in different tiers.
- **Replay:** historical bars are mapped onto the wall clock (`wall = wall0 + (hist - hist0) / speed`) because the relay rejects a `created_ms` far from its own clock. Set relay `time_scale = 1/speed`. Time spans that are protocol seconds (anchor latency, a signal's `timeframe` window for limit/stop fills) are `speed` times longer in market time than live, so a replayed track record shows that the machinery works, not what a live record would be; `sa record --replay-spec` loads a candle tail for late-anchored fills.
- **Executors:** `SimulatedPaperExecutor` (deterministic spot account on a price feed, same `size_intent` as Nautilus) for replays and tests; `NautilusPaperExecutor` wraps `FollowerExecutor` for live data. Neither can reach an exchange order endpoint. `NautilusTestnetExecutor` wraps `SpotTestnetFollower`: real orders on the follower's own Binance spot testnet account (credentials from `sa exchange add binance-testnet`); it refuses any other environment or endpoint. All implement the `Executor` protocol.

## Security baseline

- **Keys:** stored encrypted at rest (SEP-1.11).
- **Crypto libraries, no hand-written primitives:** `coincurve` (libsecp256k1) for BIP-340 and ECDH, `eth-account` for EIP-191, `pycryptodome` for ChaCha20, `hmac`/`hashlib` from the standard library for HKDF and scrypt, `asn1crypto` + `cryptography` for RFC 3161. NIP-44 v2 follows the spec (conversation key = HKDF-extract PRK; message keys = HKDF-expand with the nonce as info) and is checked against the official vectors (paulmillr/nip44) in Python and against go-nostr's `nip44` in the relay tests.
- **Server:** no server ever receives an exchange secret or wallet private key (SEP-1.13). Every wire content schema is closed, in the relay (Go: unknown JSON keys are refused, `invalid: ... unknown field "api_secret"`) and in sa-core (pydantic `extra="forbid"`): a signal, commitment, allocation, rule set or delegation carrying an extra field is rejected, never stored. The relay's EVM signing helpers exist only in `internal/evmsig/evmsigtest`, imported by tests; the shipped binary only verifies signatures.
- **API:** bound to localhost by default; GET only except `/api/auth/*`, whose state-changing routes require a valid signature or session (SEP-1.8, SEP-2.1, SEP-2.2), `POST /api/strategy/validate` (pure) and `POST /api/backtests/run` (refused unless the API is bound to loopback; see "Dashboard API").
- **Key files from disk are untrusted:** scrypt cost parameters are bounded on load (`n` within 2^10..2^20, `n*r` at most 2^21, `n*r*p` at most 2^22), so a crafted key file cannot make `sa` allocate gigabytes or burn CPU.
- **Sign-in is rate limited:** `POST /api/auth/nonce` allows 30 requests per client IP per minute (`nonce_rate_per_min`), so one client cannot fill the shared pending-nonce pool. Behind a reverse proxy start `sa-api` with `--proxy-headers` (and `--forwarded-allow-ips`): without it every user shares the proxy's IP and the session cookie is `Secure` only if `siwe_uri` is https (the app sees plain http). Never enable it when clients can reach the API directly.
- **No dynamic code:** no `eval`/`exec`/`new Function` anywhere (SEP-1.9).

### Accepted risks

- **L6, re-wrap plagiarism by a top-tier follower.** A follower in the best tier of a public provider receives signals first and can republish them as its own provider. Signals are public by design and carry no secret; the copy is visibly later than the original and its provenance is the original's anchor. Nothing in the MVP prevents it; reputation is the defence.
- **L7, flat `personal_sign` texts.** Delegations, revocations, allocations and rule sets are signed as plain EIP-191 text (`sa-delegation:...`), which wallets show as an opaque string. Typed-data signing (EIP-712) would let a wallet display the fields. Deferred: the texts are domain-separated by their prefix, and a signature for one cannot be replayed as another.
- **No transactions:** nothing sends a real-chain transaction in the MVP.

## Quality gates (every package)

- **Python:**
  - `uv run ruff check`, `ruff format --check`, `mypy --strict`;
  - `pytest` with `fail_under = 80`;
  - Hypothesis for invariants (signatures, allocations, chains, tiers, determinism).
- **Go:** `gofmt`, `go vet`, `go test -race`.
- **TS:** `biome check`, `tsc --noEmit` (strict), `bun test`.
- **Git:** a pre-commit hook runs the fast checks.
- **CI** (`.github/workflows/ci.yml`): jobs `lint`, `typecheck`, `test` (bare `uv run pytest`, combined coverage gate), `relay` (gofmt, vet, `go test -race`), `web` and `e2e`; each is a separate required-able check (SEP-1.4). A bare `uv run pytest` at the repo root runs every Python package's tests in one process with `fail_under = 80` over `sa_core`, `sa_engine`, `sa_cli` and `sa_api`; test helper modules have package-unique names (`core_helpers`, `cli_support`, `api_fake_relay`, ...) so nothing collides, and `make test` keeps the per-package gates.

## Dashboard API (`apps/sa-api`, default `127.0.0.1:8787`; read-only except Sign-In with Ethereum and the two strategy-designer routes)

The TypeScript types in `web/src/api/types.ts` and the mock in `web/mock/` are the reference shape. `apps/sa-api/tests/test_contract.py` runs `web/mock/contract.json` (a dump of the mock, regenerated with `bun run contract` and checked against the mock by a web test) against live API responses: same keys, JSON types and units. Accepted refinements from `web/API-CHANGES.md` (2026-10-13):
- Units: timestamps are epoch milliseconds. `win_rate`, `completeness` and `anchor_coverage` are fractions 0–1. `*_pct` fields are percent numbers. `release[].tier` indexes the provider's `tiers`, best tier first. `anchored_ms` and `chains[].block` may be null.
- `/api/backtests` items carry `id`.
- `/api/me` adds `balances: [{chain, balance}]`.
- `gaps: [{after_seq, missing}]`; `unrevealed_commitments: [{id, seq|null, committed_ms}]`.
- `record_summary` adds `signatures_ok`, and (security review H3) `excluded`, `late_anchor` and `unanchored` counts; `anchor_coverage` counts on-time anchors only.
- `/api/network` adds `time_scale` (the relay's, 1 live and `1/speed` in a replay); `release[]` items add `observed` (true when `at_ms` is a follower's real receipt time); `/api/me` adds `signed_in`.
- `GET /api/providers/{pubkey}` returns a single provider in the same shape as the list items.

Endpoints (GET only except the three `/api/auth/*` routes and the two strategy-designer routes below, which are **the only non-GET routes**; any other write verb answers 405, SEP-1.8; the API holds no key and publishes nothing):

| Endpoint | Real data behind it |
|---|---|
| `/api/providers`, `/api/providers/{pubkey}` | rule sets (37318) from the relay; `followers` and `power_backing` from the newest valid allocation (9332) of each address times its token power; `record_summary` from the track record below; `name` is a configured label, else the strategy id of its chain |
| `/api/providers/{pubkey}/record` | `sa_core.trackrecord.compute` over a bundle built from the provider's signals, commitments and anchor attestations on the relay, merged with exported bundles (these carry the reveals of private signals), against the replay's candles or Binance REST klines. `equity` is the report's equity curve (fraction of capital, times `capital`), `trades` its round trips, `verification` the bundle check (`signatures_ok` = no event failed its id, signature or delegation) |
| `/api/providers/{pubkey}/signals?limit=` | the verified chain items, newest first. `release[]` is the schedule the relay applies: signal `created_ms` (the relay requires it within seconds of its receipt time) plus `delay_fraction × timeframe × time_scale` per tier; when a configured follower ledger holds that signal, that follower's tier shows its real receipt time and the other tiers are offset from the receipt time it implies. A private provider's signals appear once revealed in a bundle, with a single release for entitled followers; until then only the commitments are public and the record lists them as unrevealed |
| `/api/backtests`, `/api/backtests/{id}` | `BacktestReport` JSON files from `sa backtest --json -o` or from a finished `POST /api/backtests/run` job; `id` is the file name (a job's report is `<job id>.json`), `fills` are the chart overlay |
| `/api/backtests/jobs/{id}` | status of a backtest job started by `POST /api/backtests/run`: `{id, state: running\|done\|failed\|timeout, started_ms, finished_ms, backtest_id, error}`; 404 names an unknown id. `backtest_id` is the id to open in `/api/backtests/{id}` once `state` is `done` |
| `/api/me` | with a valid session cookie: that wallet's address, its token power from the balance oracle and its newest valid allocation (9332) on the relay, `signed_in: true`. Without one: the public files of one `sa` home (`identity.json`, `allocation.json`) and the balance oracle, `signed_in: false` |
| `/api/governance/proposals` | proposals (9330), votes, allocations and delegations from the relay, tallied by `sa_core.governance.tally` with the balance oracle; `relays_answered` lists the relays that answered |
| `/api/network` | a connection test per configured relay; per chain `eth_blockNumber` (fixture chains: ok, no block); the relay `time_scale` |

**Sign-In with Ethereum (EIP-4361, SEP-2.1, SEP-2.2)**, the first group of non-GET routes (the strategy-designer routes follow). The session lives in an `HttpOnly; SameSite=Strict; Path=/` cookie `sa_session` (`Secure` over https), never readable by the page; sessions and pending nonces are in memory (bounded; a restart signs everybody out). JSON bodies only (415 otherwise); errors are `{"error": "..."}`.

| Route | Body -> answer | Rules |
|---|---|---|
| `POST /api/auth/nonce` | `{address}` -> `{nonce, message}` | `message` is the complete EIP-4361 text the server built: domain, EIP-55 address, statement, URI, `Version: 1`, `Chain ID`, a 128-bit random `Nonce`, `Issued At`, `Expiration Time` (at most 10 minutes later, `siwe_nonce_ttl_s`). 400 on a malformed address. The one unauthenticated POST: it only reserves one single-use nonce (at most 10,000 pending, and 30 per client IP per minute, 429 beyond) |
| `POST /api/auth/verify` | `{message, signature}` -> `{address}` + cookie | `signature` is the EIP-191 `personal_sign` result (0x + 130 hex). The nonce is consumed first (a replay, or a retry after a failure, needs a new nonce); then the message must be unexpired, name this server's domain (`siwe_domain`, else the Host header), equal the message issued for that nonce, and the signature must recover the address in it. 400 malformed body, message or signature; 401 unknown/used nonce, expired, wrong domain, altered message, signature by another key |
| `POST /api/auth/logout` | `{}` -> `{}`, clears the cookie | needs a valid session (401 otherwise): a state-changing route never answers 2xx unauthenticated |

**Strategy designer routes (F4)**, the only other non-GET routes. Both take and return JSON only (415 otherwise, 413 over 256 KiB) and count against `market_rate_per_min` per client IP.

| Route | Body -> answer | Rules |
|---|---|---|
| `POST /api/strategy/validate` | a strategy document -> `{ok, errors: [{path, column, message}]}` (always 200 for a body it could read) | **Pure**: parses and checks, reads and writes nothing, starts nothing, so it is allowed on any bind address. `path` is the document's JSON path (`$.stages.manage.stop.phases[0].level`), `column` the 0-based column inside a condition or formula, else `null`. Malformed JSON or a non-object is reported at `$`. All problems are listed at once (the structure first; timeframe and phase-reference checks once the structure is valid) |
| `POST /api/backtests/run` | `{strategy, instrument, timeframe?, start, end}` -> `202 {id, state: "running", ...}` | **Loopback only**, 403 otherwise: the server must be *bound* to a loopback address (`--host 127.0.0.1`/`::1`/`localhost`; `create_app(bind_host=...)`, an unknown bind counts as not loopback), `proxy_headers` must be off, no `X-Forwarded-For`/`Forwarded` header may be present, the `Host` header must be a loopback name (DNS rebinding) and, if sent, `Origin` must be the page's own host. **One job at a time** (409 with the running job's id), **bounded time**: the job runs `python -m sa_engine backtest` in a child process killed after `backtest_timeout_s` (default 900; state `timeout`), and the window may span at most `backtest_max_days` (default 400). 422 `{error, errors:[{path, column, message}]}` for a bad request: the strategy's own issues keep the document's `$...` paths, request fields use `request.start`, `request.instrument`, ...; `timeframe`, when sent, must equal the document's (the document decides). Instruments: `BINANCE:BTCUSDT`, `BINANCE:ETHUSDT`. Data: `data_dir` (as the market routes; a missing month is downloaded, checksum-verified, by the engine). On success the report is written atomically to `backtests_dir/<job id>.json`, so it is in `/api/backtests` with its fills at once; jobs are kept in memory (the last 50) and lost on a restart, the reports are not |

Why these are allowed next to a "read-only" API: `validate` is a pure function of its body (no key, no state, no I/O). `run` changes nothing the network trusts: it writes a backtest report into the user's own `backtests_dir`, never a key, signal or network event, and it only answers a user at the machine itself (loopback bind, no proxy, loopback Host and Origin), so a deployment on a public address or behind a proxy never offers it. It can burn CPU and disk, hence one job, a wall-clock limit, a window limit and a body limit. The demo containers bind `0.0.0.0` inside the container and so answer 403 here by design.

Tests: `apps/sa-api/tests/test_f4_designer.py` (shape and purity of validate; every 403 reason; request validation; a real job end to end whose report equals a direct run; busy, timeout, failure; the route table has no other write verb) and `apps/sa-api/tests/test_siwe.py` (round trip with a throwaway key, replay, expiry, wrong domain, tampered signature and message with Hypothesis, cookie flags, `/api/me` follows the session) and `test_api.py::test_only_the_sign_in_routes_change_state_...` (the route table has no write verb besides these five).

Configuration is one JSON file (`ApiConfig` in `apps/sa-api/src/sa_api/config.py`) and a few flags; `--web-dist` serves the built dashboard at `/`, so the whole UI runs from one port. The API reads the relay as an anonymous, zero-power client: it sees a signal when the slowest public tier does, and never reads a private signal. Seam additions this needed, all backwards compatible: `sa_core.trackrecord.TrackRecord.series` (equity curve and trades, not part of the report bytes), `BundleCheck.signature_failures`, `sa_core.tsa_lab` (the local TSA farm, moved from test code), a `lab` flag and loopback exemption for the TSA minimum gap, and `sa backtest -o`.

## Demo and containers

- `sa demo` (`apps/sa-cli/src/sa_cli/cmd_demo.py`, plan in `demo_plan.py`) is the whole network in one command: it builds `sarelay` and `web/dist` once, starts the relay with fixture balances and `time_scale = 1/speed`, three throwaway TSAs in-process (or `--public-tsa`), creates the cast (atlas public, fox private at 10,000 SA, whale 1,000,000, mid 20,000, zero 0), registers and allocates, proposes and votes, backtests, starts both providers on one replay clock (`sa provider run --start-at`), the followers and the API, and stops everything on Ctrl-C. `--live` runs Atlas on live paper trading and idles Fox (one relay has one time scale).
- Governance in the demo is tallied with event times standing in for anchors (`governance_anchor = "created-at"`, shown nowhere as verified): votes and allocations are anchored by OpenTimestamps in real use, which takes hours.
- `deploy/docker-compose.yml` runs the same code, one `sa demo --role ...` per container (`tsa`, `setup`, `provider`, `follower`, `api`) plus the relay, sharing a `work` volume; only the dashboard port is published, on the host's loopback.
- `make demo-smoke` (`scripts/demo_smoke.py`) starts `sa demo` headless, asserts providers, signals with tier releases and anchors, a verified track record, a revealed private record, a backtest, network status, My power, a governance tally, the dashboard and the absence of write endpoints, then stops it with SIGINT and checks the ports and processes are gone.

## Market chart API (F7, read-only)

- `GET /api/market/symbols?q=` → `[{instrument, base, quote}]` (Binance spot `exchangeInfo`, cached).
- `GET /api/market/candles?instrument=&tf=&end_ms=&limit=` → `[{t, o, h, l, c, v}]` (`t` epoch ms; `tf` ∈ 1m,5m,15m,1h,4h,1d; limit ≤ 1000; local catalog first, Binance public klines otherwise).
- `GET /api/market/indicators?instrument=&tf=&end_ms=&limit=&spec=ema(20),rsi(14),bbands(20,2)` → `{"<spec>": [{t, value}] or [{t, upper, mid, lower}]}`, computed by `sa_engine` indicator code (no second implementation).
- `GET /api/backtests/{id}` adds `fills: [{t, side, price, qty}]` and `instrument`, `timeframe`.
- `GET /api/providers/{pubkey}/export?format=bundle|csv` → file download (`Content-Disposition: attachment`); bundle is the signed history `sa verify` accepts.
- Charting: TradingView Lightweight Charts bundled in `web/` (no CDN). No Pine, no TradingView alerts, nothing executes on a third-party platform.
- Refinements as built (2026-10-13): symbols return the bare symbol (`ETHUSDT`); market `instrument` accepts `ETHUSDT` or `BINANCE:ETHUSDT`; `tf` and `instrument` are required; candle `end_ms` is inclusive of the candle containing it (omitted = now); indicators are warmed up before the window and the warm-up is not returned; backtest list and detail `fills` are `{t, side, price, qty, kind}` with `kind` entry/exit; CSV export has the mock's 11 columns followed by `order_type, limit_price, stop_price, timeframe_s`.
