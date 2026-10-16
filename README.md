# Superalgos v2

Superalgos v2 is a new take on the original Superalgos requirements, rebuilt on a current tech base. It starts with the
part of the plan that gives the SA token its purpose: trading signals you can check. A provider runs a strategy and
publishes each order as a signed, time-stamped message. Followers copy those signals on their own account, at a delay
that depends on how much SA token power they have given that provider. Anyone can recompute a provider's track record
from the signed history, without trusting a server or the provider's word.

v2 does not replace v1. It gathers v1's features and recreates the ones that still make sense, on
[NautilusTrader](https://nautilustrader.io) for backtesting and execution, Nostr for transport and typed code
throughout. It is an open-source, self-hosted project: nothing here holds user funds or exchange keys, and nobody runs
a shared service on your behalf.

This repository is a proposal to the Superalgos community. The direction is for SA holders to decide.

## From the original requirements to v2

| Original Superalgos idea | How v2 does it today |
|---|---|
| **Signals network**: one bot emits, another receives, with signed messages (2021 design) | Every signal is a signed Nostr event, relayed by `sarelay`. Provider and follower bots run end to end. |
| **Per-device signing keys**: the funds key never touches a hot device | The SA address signs a delegation to a device key, with an expiry. Only the device key signs signals, and a revocation takes effect at a stated time. |
| **Token power as priority**: more power, earlier access | A follower's tier comes from the power its SA address allocates to that provider, never from the raw balance. The relay releases each signal at receipt plus that tier's delay. |
| **Token Power Switch**: one budget split across uses | A signed power allocation splits an address's SA between governance and providers. No tokens move. |
| **Social trading bots**: public or premium signals | Providers choose public tiers, or private signals encrypted to each entitled follower (NIP-44). |
| **Trust in a provider** (in v1, social) | Track records are recomputed from signed, time-stamped history. A signal anchored after the move it claims cannot count. |
| **Governance by token holders** | Proposals and votes are signed events, tallied by power at a snapshot, with one-hop vote delegation. |
| **Visual strategy design, data mining** | A typed strategy schema and expression language (no `eval`), NautilusTrader's indicators, backtests, and a chart that draws exactly what the engine computes. |
| **Run it yourself** | One command starts a whole local network. Containers run each role separately. |

## Try it

You need [uv](https://docs.astral.sh/uv/), Go 1.27 or newer and [bun](https://bun.sh). The relay and the dashboard are
built once, from source.

```sh
git clone <this repository> && cd superalgos-v2
uv sync --all-packages
uv run sa demo          # or: make demo
```

After a few seconds it prints `READY http://127.0.0.1:8787`. Open that address. `sa demo` starts, on your machine:

- the tiered relay (`sarelay`) with test balances, at a demo time scale (a replayed day passes in 60 seconds);
- two providers with throwaway identities: **Atlas** (public, four tiers) and **Fox** (private, 10,000 SA to be
  entitled), replaying real BTCUSDT 1m candles from Binance's public archive;
- three followers: **whale** (1,000,000 SA, first tier), **mid** (20,000 SA, third tier; the dashboard's "My power")
  and **zero** (no SA, slowest tier), each paper-trading what verifies;
- a backtest per strategy, one governance proposal with two votes, and the read-only API behind the dashboard.

Open **Dev views** to watch each signal reach each tier. Ctrl-C stops everything. Run `uv run sa demo --help` for the
flags: `--days`, `--speed`, `--port`, `--public-tsa` (real public time-stamp authorities), `--live` (Atlas
paper-trades live Binance data).

The demo uses throwaway keys and test balances. It never touches a real wallet or exchange, and it places no real
order. A replay shows the machinery working; it is not a live record.

Prefer containers? `docker compose -f deploy/docker-compose.yml up --build` runs the same network, one role per
container, on `http://127.0.0.1:8787`. `down -v` resets it.

## Why you can trust a record

1. **Signed and delegated.** Every signal is signed by a device key that the SA address delegated. Each signal carries
   `seq` and the previous signal's id, so an omitted, reordered or edited signal breaks the chain and shows up as a gap.
2. **Time-stamped before the outcome.** Signals are anchored with RFC 3161 time-stamp authorities (two of three
   organisations must agree) and checked offline against pinned roots. A commitment to a private signal that is never
   revealed counts as a miss.
3. **Recomputable.** A track record is a pure function of the signed bundle and public candles. The same input gives
   byte-identical output on any machine. The dashboard shows what the code computes, not a stored number.
4. **Access by allocated power.** Tiers follow the power an address signs over to a provider, and the relay releases
   signals in the order it received them.
5. **Nothing to steal.** No server holds an exchange secret or wallet key. Followers execute on their own account, and
   the dashboard API has no write endpoint.

A relay is trusted for timing, not for content: it cannot read private signals, but it can drop or delay events.
Providers keep their own signed archive (`sa provider export`), which anyone can verify without the relay.

## What v2 deliberately leaves out

- **No custody and no hosted service.** Everything runs on the user's own machine and account.
- **No new token distribution** and no paid token bounties.
- **No token price promises.** SA gets a use (signal access and priority, governance), not a forecast.
- **No social feed.** Providers are found by their verified record, not by posts.
- **No v1 code.** v1's ideas are carried over as requirements and rebuilt; its code is not ported.

## Where things are

`ARCHITECTURE.md` describes the seams between the parts: wire formats, tiers, anchors, the track record and the
dashboard API.

| Part | What it is |
|---|---|
| `packages/sa-core` | models, keys, Nostr, delegations, token power, anchors, bundles, the deterministic track record, governance |
| `packages/sa-engine` | strategy schema and expression language, Nautilus backtests and paper trading, provider and follower bots |
| `apps/sa-cli` | the `sa` command, including `sa demo` |
| `apps/sa-api` | the read-only HTTP API (FastAPI) behind the dashboard |
| `relay/` | `sarelay`, the tiered relay (Go) |
| `web/` | the dashboard (TypeScript, React) |
| `deploy/` | containers for the demo network and a standalone relay |
| `tests/e2e` | end-to-end tests across real processes |

## Checks

```sh
make check          # ruff, mypy --strict, pytest with coverage >= 80% for every Python package
make -C relay check # gofmt, go vet, go test -race
make web-check      # biome, tsc strict, bun test with coverage, production build
make e2e            # real processes: relay, providers, followers, record
make demo-smoke     # `sa demo` headless for about a minute, every API endpoint checked against real data
```

## Licence

Apache-2.0 (see `LICENSE`), like Superalgos v1. NautilusTrader is LGPL-3.0 and is used as an unmodified dependency.
