"""``sa``: argument parsing and error handling. Every command is a function ``(args, home) -> exit code``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from sa_cli import (
    cmd_demo,
    cmd_engine,
    cmd_exchange,
    cmd_follow,
    cmd_gov,
    cmd_identity,
    cmd_profile,
    cmd_provider,
    cmd_record,
)
from sa_cli.home import CliError, Home
from sa_core.governance import DEFAULT_MIN_PROPOSER_POWER

Handler = Callable[[argparse.Namespace, Home], int]

DEFAULT_RELAY = "ws://127.0.0.1:7781"


def _add_tsa(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tsa-config",
        type=Path,
        help="JSON with trust_dir/endpoints/k; default: public TSAs and the bundled roots",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sa", description="Superalgos v2: verifiable trading signals.")
    p.add_argument("--home", type=Path, help="key directory (default: $SA_HOME or ~/.superalgos)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # keys
    keys = sub.add_parser("keys", help="manage the encrypted device key").add_subparsers(
        dest="sub", required=True
    )
    k_init = keys.add_parser(
        "init", help="create the encrypted device key (passphrase from $SA_PASSPHRASE or a prompt)"
    )
    k_init.set_defaults(func=cmd_identity.keys_init)

    # identity
    ident = sub.add_parser(
        "identity",
        help="demo identity, read-only address, or import a delegation; shows the current one",
    )
    ident.add_argument(
        "--demo",
        action="store_true",
        help="create a throwaway SA key and delegate the device key (demos only)",
    )
    ident.add_argument(
        "--renew",
        action="store_true",
        help="with --demo: sign a fresh delegation with the existing demo key",
    )
    ident.add_argument(
        "--days",
        type=float,
        default=30.0,
        help="delegation lifetime in days (default 30, at most 90)",
    )
    ident.add_argument(
        "--tokens",
        type=str,
        help="with --demo: SA tokens to give this identity in the --balances fixture",
    )
    ident.add_argument(
        "--import-address", metavar="0xADDR", help="read-only: show its token power, cannot sign"
    )
    ident.add_argument(
        "--import-delegation", metavar="FILE", help="use a delegation signed by your own wallet"
    )
    ident.add_argument("--power", action="store_true", help="print token power")
    ident.add_argument(
        "--balances", type=Path, help="fixture balance file (shared with the relay's fixture mode)"
    )
    ident.set_defaults(func=cmd_identity.identity)

    # allocate
    al = sub.add_parser(
        "allocate", help="split token power between providers and governance (basis points)"
    )
    al.add_argument("--provider", action="append", metavar="PUBKEY=BPS", help="repeatable")
    al.add_argument("--governance", type=int, default=0, metavar="BPS")
    al.add_argument("--nonce", type=int, help="default: last nonce + 1")
    al.add_argument("--relay", help="publish the allocation (kind 9332) to this relay")
    al.set_defaults(func=cmd_identity.allocate)

    # revoke
    rv = sub.add_parser(
        "revoke",
        help="withdraw a device key's delegation (kind 9334) after loss or theft; signed with the SA key",
    )
    rv.add_argument(
        "--device",
        metavar="PUBKEY",
        help="the device key to revoke (64 hex; default: this home's own device key)",
    )
    rv.add_argument(
        "--effective-at",
        type=int,
        metavar="UNIX",
        help="revoke from this unix time (default: now); earlier times void what that key signed since",
    )
    rv.add_argument("--relay", default=DEFAULT_RELAY)
    rv.set_defaults(func=cmd_identity.revoke)

    # provider
    prov = sub.add_parser("provider", help="provider bot: register, run, export").add_subparsers(
        dest="sub", required=True
    )
    reg = prov.add_parser("register", help="sign and publish the provider's rule set (kind 37318)")
    reg.add_argument("strategy")
    reg.add_argument("--relay", default=DEFAULT_RELAY)
    reg.add_argument(
        "--state", help="provider state directory (default: <home>/provider/<strategy name>)"
    )
    reg.add_argument(
        "--tier",
        action="append",
        metavar="MIN@DELAY",
        help="repeatable; default: 500000@0, 100000@0.1, 10000@0.5, 0@2",
    )
    reg.add_argument(
        "--private",
        type=int,
        metavar="MIN_POWER",
        help="private provider: only followers with this much allocated power",
    )
    reg.set_defaults(func=cmd_provider.register)
    run = prov.add_parser(
        "run", help="run the strategy and publish a signed, anchored signal for every order"
    )
    run.add_argument("strategy")
    run.add_argument("--relay", default=DEFAULT_RELAY)
    run.add_argument("--state")
    run.add_argument(
        "--replay",
        nargs=2,
        metavar=("START", "END"),
        help="replay real history YYYY-MM-DD YYYY-MM-DD (end exclusive)",
    )
    run.add_argument("--paper", action="store_true", help="live Binance data, simulated fills")
    run.add_argument(
        "--speed",
        type=float,
        default=3600.0,
        help="replay speed: market seconds per wall second (default 3600)",
    )
    run.add_argument(
        "--lead",
        type=float,
        default=3.0,
        help="seconds before the first replayed signal (default 3)",
    )
    run.add_argument(
        "--start-at",
        type=float,
        metavar="UNIX_SECONDS",
        help="replay: start the clock at this wall time instead of now + --lead (several providers, one clock)",
    )
    run.add_argument("--instrument", default="BINANCE:BTCUSDT")
    run.add_argument("--capital", type=float, default=10_000.0)
    run.add_argument("--duration", type=float, help="paper mode: stop after this many seconds")
    run.add_argument("--data-dir", type=Path)
    run.add_argument(
        "--balances",
        type=Path,
        help="private providers: fixture balances for entitlement (default: real chains)",
    )
    run.add_argument(
        "--delay-d",
        type=int,
        default=0,
        help="allocation delay D in seconds, as configured on the relay (default 0)",
    )
    run.add_argument(
        "--no-anchor",
        action="store_true",
        help="skip TSA anchoring (signals then never count in a record)",
    )
    run.add_argument(
        "--anchor-timeout",
        type=float,
        default=90.0,
        help="seconds to wait for the last TSA batch at the end",
    )
    run.add_argument(
        "--export",
        type=Path,
        metavar="BUNDLE",
        help="write the provider's bundle when the run ends",
    )
    run.add_argument(
        "--reveal",
        action="store_true",
        help="with --export: include the plaintext of committed (private) signals",
    )
    _add_tsa(run)
    run.set_defaults(func=cmd_provider.run)
    exp = prov.add_parser("export", help="write the provider's bundle from its state directory")
    exp.add_argument("--state", required=True)
    exp.add_argument("-o", "--output", required=True)
    exp.add_argument("--reveal", action="store_true")
    exp.set_defaults(func=cmd_provider.export)

    # exchange
    exch = sub.add_parser(
        "exchange", help="store your own exchange API credentials, encrypted"
    ).add_subparsers(dest="exchange_cmd", required=True)
    ex_add = exch.add_parser(
        "add",
        help=f"encrypt an API key and secret (prompted, or {cmd_exchange.KEY_ENV} and {cmd_exchange.SECRET_ENV})",
    )
    ex_add.add_argument("exchange", choices=[cmd_exchange.BINANCE_TESTNET])
    ex_add.set_defaults(func=cmd_exchange.add)

    # profile
    prof = sub.add_parser(
        "profile", help="your follower settings (<home>/profile.json)"
    ).add_subparsers(dest="profile_cmd", required=True)
    prof.add_parser("show", help="print the settings in force").set_defaults(func=cmd_profile.show)
    pset = prof.add_parser(
        "set",
        help="change one setting, e.g. `sa profile set disaster-stop-pct 10` (0 turns the disaster stop off)",
    )
    pset.add_argument("setting", help=f"one of: {', '.join(cmd_profile.SETTINGS)}")
    pset.add_argument("value")
    pset.set_defaults(func=cmd_profile.set_value)

    # follow
    fol = sub.add_parser(
        "follow", help="follower bot: verify signals and trade them (paper or testnet)"
    )
    fol.add_argument("--relay", help=f"relay URL, e.g. {DEFAULT_RELAY}")
    fol.add_argument("--provider", action="append", metavar="PUBKEY", help="repeatable")
    fol.add_argument(
        "--signal-file",
        action="append",
        metavar="FILE",
        help="off-grid: execute a signal event file (same checks)",
    )
    fol.add_argument(
        "--ruleset",
        metavar="FILE",
        help="with --signal-file: the provider's rule set (timeframe check)",
    )
    fol.add_argument("--ledger", help="SQLite ledger (default: <home>/follower/ledger.sqlite)")
    fol.add_argument(
        "--executor",
        choices=["sim", "nautilus", "testnet"],
        help="default: sim with --replay-spec, else nautilus (live data, simulated fills); "
        "testnet: real orders on your Binance spot testnet account (see `sa exchange add binance-testnet`)",
    )
    fol.add_argument(
        "--replay-spec", help="the provider's replay.json: prices for the simulated account"
    )
    fol.add_argument("--instrument", default="BINANCE:BTCUSDT")
    fol.add_argument(
        "--capital",
        type=float,
        default=10_000.0,
        help="paper account size (ignored by --executor testnet)",
    )
    fol.add_argument("--duration", type=float)
    fol.add_argument("--data-dir", type=Path)
    fol.add_argument(
        "--no-allocation",
        action="store_true",
        help="do not publish <home>/allocation.json at start",
    )
    fol.add_argument(
        "--allow-gaps",
        action="store_true",
        help="execute signals after a chain gap instead of rejecting them",
    )
    fol.add_argument(
        "--reorder-wait",
        type=float,
        default=1.0,
        help="seconds a signal waits for earlier ones that a relay delivered late (default 1)",
    )
    fol.add_argument(
        "--max-age",
        type=float,
        help="reject signals older than this many seconds (default 3 x timeframe + 30)",
    )
    fol.add_argument(
        "--bar-grace",
        type=float,
        default=10.0,
        help="seconds of slack on top of one timeframe before a signal's bar counts as stale (default 10)",
    )
    fol.add_argument("--ready-file", help="written once subscribed (for scripts)")
    _add_tsa(fol)
    fol.set_defaults(func=cmd_follow.follow)

    # backtest / paper
    for name, fn, help_ in (("backtest", cmd_engine.backtest, "backtest a strategy on Binance 1m history"),
                            ("paper", cmd_engine.paper, "paper trade a strategy on live data")):  # fmt: skip
        e = sub.add_parser(name, help=help_)
        e.add_argument("strategy")
        e.add_argument("--instrument", default="BINANCE:BTCUSDT")
        e.add_argument("--capital", type=float, default=10_000.0)
        if name == "backtest":
            e.add_argument("--start", required=True, metavar="YYYY-MM-DD")
            e.add_argument("--end", required=True, metavar="YYYY-MM-DD")
            e.add_argument("--data-dir")
            e.add_argument("--json", action="store_true")
            e.add_argument(
                "-o", "--output", type=Path, help="also write the full report JSON to this file"
            )
        else:
            e.add_argument("--duration", type=float)
        e.set_defaults(func=fn)

    # record / verify
    rec = sub.add_parser("record", help="deterministic track record of a bundle")
    rec.add_argument("bundle")
    rec.add_argument(
        "--replay-spec",
        help="score a replayed bundle with the replay's candles (default: Binance REST klines)",
    )
    rec.add_argument("--data-dir", type=Path)
    rec.add_argument(
        "--tolerance",
        type=int,
        default=60,
        help="seconds a signal's anchor may lag its created time (default 60)",
    )
    rec.add_argument("-o", "--output")
    _add_tsa(rec)
    rec.set_defaults(func=cmd_record.record)
    ver = sub.add_parser(
        "verify", help="check a bundle (completeness, anchors) or a signal event file"
    )
    ver.add_argument("path")
    ver.add_argument("--sa-address", help="signal file: require this delegating SA address")
    _add_tsa(ver)
    ver.set_defaults(func=cmd_record.verify)

    # demo
    dm = sub.add_parser(
        "demo", help="the whole network and dashboard on this machine, one command, no Docker"
    )
    cmd_demo.add_arguments(dm)
    dm.set_defaults(func=cmd_demo.demo)

    # governance
    gov = sub.add_parser("gov", help="governance: propose, vote, tally").add_subparsers(
        dest="sub", required=True
    )
    gp = gov.add_parser("propose")
    gp.add_argument("--title", required=True)
    gp.add_argument("--option", action="append", required=True, help="repeat at least twice")
    gp.add_argument("--deadline-in", default="3d", help="e.g. 90s, 12h, 3d (default 3d)")
    gp.add_argument(
        "--relay",
        action="append",
        required=True,
        help="official relay(s); the first one is published to",
    )
    gp.set_defaults(func=cmd_gov.propose)
    gv = gov.add_parser("vote")
    gv.add_argument("proposal", help="proposal event id")
    gv.add_argument("--option", type=int, required=True, help="option index, from 0")
    gv.add_argument("--relay", default=DEFAULT_RELAY)
    gv.add_argument(
        "--stamp-ots",
        action="store_true",
        help="also submit an OpenTimestamps proof (public calendars)",
    )
    gv.set_defaults(func=cmd_gov.vote)
    gt = gov.add_parser("tally")
    gt.add_argument("proposal")
    gt.add_argument("--relay", action="append", required=True, help="official relay(s)")
    gt.add_argument("--balances", type=Path, help="fixture balances (default: real chains)")
    gt.add_argument("--delay-d", type=int, default=7 * 86400)
    gt.add_argument(
        "--min-proposer-power",
        type=int,
        default=DEFAULT_MIN_PROPOSER_POWER,
        help=f"whole SA tokens a proposer must hold at the snapshot (default {DEFAULT_MIN_PROPOSER_POWER})",
    )
    gt.add_argument(
        "--demo-trust-created-at",
        action="store_true",
        help="DEMO ONLY: trust created_at instead of anchors",
    )
    gt.add_argument("-o", "--output")
    gt.set_defaults(func=cmd_gov.tally_cmd)
    return p


def main(argv: Sequence[str] | None = None, *, home: Home | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if home is None:
        home = Home(
            Path(args.home) if args.home else Path(os.environ.get("SA_HOME", "~/.superalgos"))
        )
    try:
        func: Handler = args.func
        return func(args, home)
    except CliError as exc:
        print(f"sa: error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def run() -> None:  # pragma: no cover - console entry point
    sys.exit(main())
