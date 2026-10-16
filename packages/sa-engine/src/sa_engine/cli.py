"""``python -m sa_engine``: validate / schema / backtest / paper."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sa_engine.schema import StrategyValidationError, schema_text, validate_strategy


def _load(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"cannot read strategy file {path!r}: {e.strerror}") from None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sa_engine", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a strategy JSON file; prints failing JSON paths")
    v.add_argument("file")
    v.add_argument("--json", action="store_true", help='print {"ok": bool, "errors": [{path, column, message}]}')

    s = sub.add_parser("schema", help="print (or write) the strategy JSON Schema")
    s.add_argument("-o", "--output")

    b = sub.add_parser("backtest", help="backtest a strategy on Binance 1m data")
    b.add_argument("file")
    b.add_argument("--instrument", default="BINANCE:BTCUSDT")
    b.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    b.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive)")
    b.add_argument("--capital", type=float, default=10_000.0)
    b.add_argument("--data-dir", type=Path, default=None)
    b.add_argument("--json", action="store_true", help="print the full report as JSON")

    pp = sub.add_parser("paper", help="paper trade a strategy on live Binance data (no keys, simulated fills)")
    pp.add_argument("file")
    pp.add_argument("--instrument", default="BINANCE:BTCUSDT")
    pp.add_argument("--capital", type=float, default=10_000.0)
    pp.add_argument("--duration", type=float, default=None, help="seconds; default until Ctrl-C")

    args = p.parse_args(argv)
    if args.cmd == "schema":
        text = schema_text()
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    try:
        doc = validate_strategy(_load(args.file))
    except StrategyValidationError as e:
        if args.cmd == "validate" and args.json:
            print(json.dumps({"ok": False, "errors": [i.model_dump() for i in e.issues]}, indent=2))
        else:
            for issue in e.issues:
                print(f"{args.file}: {issue}", file=sys.stderr)
        return 2
    if args.cmd == "validate":
        if args.json:
            print(json.dumps({"ok": True, "errors": []}))
            return 0
        print(f"{args.file}: valid ({doc.name}, {doc.timeframe}, {len(doc.stages.open.orders)} order(s))")
        return 0
    if args.cmd == "backtest":
        from sa_engine.backtest import run_backtest

        report = run_backtest(
            doc, args.instrument, args.start, args.end, starting_capital=args.capital, data_dir=args.data_dir
        )
        print(report.model_dump_json(indent=2) if args.json else report.summary())
        return 0
    from sa_engine.paper import run_paper

    paper = run_paper(doc, args.instrument, args.capital, duration_s=args.duration, log_level="WARNING")
    print(json.dumps(paper.model_dump(exclude={"trades"}), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
