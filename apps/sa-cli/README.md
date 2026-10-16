# sa-cli

`sa`, the Superalgos v2 command line (`import sa_cli`). Short errors: `sa: error: <what failed, with the value>; <what to do>`
(exit 2; verification failures exit 1).

```
sa keys init                         # encrypted device key; passphrase from $SA_PASSPHRASE or a prompt
sa identity --demo [--tokens N --balances FILE]   # throwaway SA key + delegation; FILE is the relay's fixture balances
sa identity --import-address 0x...   # read-only: token power only
sa identity --import-delegation FILE # delegation signed by your own wallet (the SA key never reaches the CLI)
sa allocate --provider PUBKEY=BPS --governance BPS [--relay URL]
sa provider register STRATEGY --relay URL [--tier MIN@DELAY ... | --private MIN]
sa provider run STRATEGY --relay URL (--replay START END --speed S | --paper) [--export BUNDLE [--reveal]]
sa provider export --state DIR -o BUNDLE [--reveal]
sa follow --relay URL --provider PUBKEY [--replay-spec FILE]   # or: --signal-file FILE (off-grid)
sa backtest STRATEGY --start D --end D     sa paper STRATEGY
sa record BUNDLE [--replay-spec FILE]      # deterministic track record
sa verify BUNDLE_OR_SIGNAL_FILE            # completeness, anchors, delegation, signature
sa gov propose|vote|tally
sa demo [--days N --speed S --port P --live --public-tsa --duration SECONDS]   # the whole network + dashboard, Ctrl-C to stop
```

Keys live in `--home` / `$SA_HOME` (default `~/.superalgos`): `device.key.json` and `sa.key.json` are scrypt + ChaCha20-Poly1305
files (mode 0600). `$SA_SCRYPT_N` lowers the KDF cost for tests and demos only. `--tsa-config FILE` replaces the public TSAs
and the bundled pinned roots. Demo walk-through: `make e2e` (`tests/e2e`) runs all of this across real processes.

## `sa demo`

One command, no Docker: builds `sarelay` (Go) and `web/dist` (bun) on first use, starts the relay with fixture balances and a
demo time scale, creates throwaway identities (atlas public, fox private, whale 1,000,000 SA, mid 20,000 SA, zero 0 SA),
registers both providers, allocates, proposes and votes, backtests both strategies, starts the providers on one replay clock
of real BTCUSDT candles, the followers, and `sa-api` serving the dashboard (`http://127.0.0.1:8787`). `--live` runs atlas on live
paper trading; `--public-tsa` anchors with the real public TSAs; the files and logs are in `.sa-demo/` (wiped on every start,
only if it carries the `.sa-demo` marker). `--role tsa|setup|provider|follower|api` is one part of it, for `deploy/docker-compose.yml`.
`make demo-smoke` runs it headless and asserts every endpoint against real data.
