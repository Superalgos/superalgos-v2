"""``sa backtest`` and ``sa paper``: thin wrappers over sa_engine with the same flags as `python -m sa_engine`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sa_cli.cmd_provider import load_strategy
from sa_cli.home import CliError, Home
from sa_engine.data import DataError


def backtest(args: argparse.Namespace, home: Home) -> int:
    from sa_engine.backtest import run_backtest  # noqa: PLC0415 - Nautilus loads only when it is needed

    doc = load_strategy(args.strategy)
    try:
        report = run_backtest(
            doc, args.instrument, args.start, args.end, starting_capital=args.capital,
            data_dir=Path(args.data_dir) if args.data_dir else None,
        )  # fmt: skip
    except (DataError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    if args.output:
        try:
            Path(args.output).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise CliError(
                f"cannot write the report to {args.output}: {exc.strerror}; pick a writable path"
            ) from exc
    print(report.model_dump_json(indent=2) if args.json else report.summary())
    return 0


def paper(args: argparse.Namespace, home: Home) -> int:
    from sa_engine.paper import run_paper  # noqa: PLC0415 - Nautilus loads only when it is needed

    doc = load_strategy(args.strategy)
    try:
        result = run_paper(
            doc, args.instrument, args.capital, duration_s=args.duration, log_level="WARNING"
        )
    except (ValueError, RuntimeError, TimeoutError) as exc:
        raise CliError(f"paper trading failed: {exc}") from exc
    print(json.dumps(result.model_dump(exclude={"trades"}), indent=2))
    return 0
