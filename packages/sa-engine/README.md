# sa-engine

Strategy schema and DSL, Nautilus compilation, Binance data, backtest and paper trading, and the provider and follower
bots (`sa_engine.network`). It imports `sa_core`, never the reverse. The seams are in the repository's `ARCHITECTURE.md`
("Strategy documents", "Timing rules", "Network bots"); the criteria are in `seps/`.

## How to run

From the repository root, `uv sync --all-packages` once. Then, from this directory:

```sh
uv run python -m sa_engine validate examples/grid_10_level.json            # --json prints {ok, errors:[{path, column, message}]}
uv run python -m sa_engine backtest examples/grid_10_level.json --start 2025-01-01 --end 2026-01-01   # downloads to ./.sa-data (gitignored; SA_DATA_DIR overrides)
uv run python -m sa_engine paper examples/ema_crossover.json --duration 120                            # live data, simulated fills
uv run python -m sa_engine schema -o schema/strategy.schema.json                                       # regenerate the JSON Schema
uv run pytest                              # offline, seconds
uv run pytest -m slow -o addopts=""        # 1 year x 3 strategies (downloads about 40 MB the first time)
uv run pytest -m online -o addopts=""      # live paper smoke, about 3 minutes, needs network
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests
```

The default test run is offline and uses a synthetic 5-day fixture catalog. Binance 1m klines come from
`data.binance.vision` (sha256-checked) and are cached per month; a second run does no network. Paper mode needs no API keys.
Examples are in `examples/`.
