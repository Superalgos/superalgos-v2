"""``sa follow``: subscribe to providers (or read signal files) and paper-trade what verifies."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import SecretStr

from sa_cli.cmd_exchange import BINANCE_TESTNET
from sa_cli.cmd_profile import FollowerProfile, load_profile
from sa_cli.home import CliError, Home
from sa_cli.tsa_config import load_tsa_settings
from sa_core.keys import ExchangeCredentials, NotEd25519Error, normalize_ed25519_secret
from sa_core.models import ProviderRuleSet
from sa_engine.network.executor import (
    Executor,
    NautilusPaperExecutor,
    NautilusTestnetExecutor,
    SimulatedPaperExecutor,
)
from sa_engine.network.follower import FollowerBot, FollowerError
from sa_engine.network.ledger import Ledger, LedgerRow
from sa_engine.network.replay import PriceFeed, ReplayError, ReplaySpec, load_replay_candles


class NodeRunner(Protocol):
    """The Nautilus-backed side of an executor: blocks the main thread while the node runs."""

    def run(self, duration_s: float | None = None) -> Any: ...


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _providers(values: list[str]) -> list[str]:
    out = [v.lower() for v in values]
    for v in out:
        if not _HEX64.match(v):
            raise CliError(f"--provider {v!r} must be a 64-character hex public key")
    return out


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _executor(
    args: argparse.Namespace, home: Home, profile: FollowerProfile
) -> tuple[Executor, NodeRunner | None, list[str]]:
    """(executor, the Nautilus runner behind it if any, secret strings to keep out of error messages)."""
    kind = args.executor or ("sim" if args.replay_spec else "nautilus")
    disaster_stop_pct = (
        profile.disaster_stop_pct
    )  # SEP-14.5; the sim executor has no exchange to rest at
    if kind == "sim":
        if not args.replay_spec:
            raise CliError(
                "--executor sim needs --replay-spec FILE (the provider's replay.json) to know prices"
            )
        try:
            spec = ReplaySpec.load(Path(args.replay_spec))
            candles = load_replay_candles(spec, data_dir=args.data_dir)
        except ReplayError as exc:
            raise CliError(str(exc)) from exc
        return SimulatedPaperExecutor(spec.instrument, args.capital, PriceFeed(candles)), None, []
    if kind == "testnet":
        from sa_engine.testnet import SpotTestnetFollower  # noqa: PLC0415 - Nautilus loads only for live trading

        creds = home.load_exchange(BINANCE_TESTNET)
        try:
            pem = normalize_ed25519_secret(creds.api_secret.get_secret_value())
        except NotEd25519Error as exc:
            raise CliError(f"stored {BINANCE_TESTNET} credentials: {exc}") from None
        creds = ExchangeCredentials(api_key=creds.api_key, api_secret=SecretStr(pem))
        body = "".join(
            pem.splitlines()[1:-1]
        )  # the key without its BEGIN/END lines, in case a message echoes it
        secrets = [creds.api_key.get_secret_value(), pem, body]
        try:
            follower = SpotTestnetFollower(
                args.instrument, creds, disaster_stop_pct=disaster_stop_pct
            )
        except ValueError as exc:
            raise CliError(_scrub(str(exc), secrets)) from None
        return NautilusTestnetExecutor(follower), follower, secrets
    from sa_engine.paper import FollowerExecutor  # noqa: PLC0415 - Nautilus loads only for live paper trading

    inner = FollowerExecutor(
        args.instrument, args.capital, disaster_stop_pct=disaster_stop_pct, log_level="WARNING"
    )
    return NautilusPaperExecutor(inner), inner, []


def _report_key(args: argparse.Namespace) -> str:
    return "testnet" if args.executor == "testnet" else "paper"


def _row_line(row: LedgerRow) -> str:
    base = f"{row.event_id[:12]} seq {row.seq}: {row.status}"
    if row.status == "rejected":
        return f"{base}: {row.reason}"
    fill = (
        f", {row.qty} @ {row.price}"
        if row.filled
        else (f", not placed: {row.exec_reason}" if not row.placed else "")
    )
    return f"{base}{fill}"


def follow(args: argparse.Namespace, home: Home) -> int:
    device = home.load_device()
    delegation = home.load_delegation(device)
    profile = load_profile(home)
    providers = _providers(args.provider or [])
    if not providers and not args.signal_file:
        raise CliError(
            "name at least one provider with --provider PUBKEY, or pass --signal-file FILE"
        )
    if not args.signal_file and not args.relay:
        raise CliError("--relay URL is required to follow providers")
    ledger = Ledger(Path(args.ledger) if args.ledger else home.root / "follower" / "ledger.sqlite")
    try:
        executor, nautilus, secrets = _executor(args, home, profile)
    except BaseException:
        ledger.close()
        raise
    allocation = None if args.signal_file or args.no_allocation else home.last_allocation()
    bot = FollowerBot(
        device=device, delegation=delegation, relay_url=args.relay or "ws://unused", providers=providers, executor=executor,
        ledger=ledger, verifiers=load_tsa_settings(args.tsa_config).verifiers(), allocation=allocation, allow_gaps=args.allow_gaps,
        max_age_s=args.max_age, bar_grace_s=args.bar_grace, reorder_wait_s=args.reorder_wait,
    )  # fmt: skip
    try:
        try:
            if args.signal_file:
                return _follow_files(args, bot, nautilus)
            return _follow_relay(args, bot, nautilus, profile)
        finally:
            _flush_exchange_events(executor, ledger)
    except FollowerError as exc:
        raise CliError(_scrub(str(exc), secrets)) from None
    except (RuntimeError, TimeoutError) as exc:  # the Nautilus node failed to start or run
        raise CliError(f"the trading node failed: {_scrub(str(exc), secrets)}") from None
    finally:
        ledger.close()


def _flush_exchange_events(executor: Executor, ledger: Ledger) -> None:
    """Order ids and fills that arrived after the bot stopped still belong in the ledger."""
    if isinstance(executor, NautilusTestnetExecutor):
        for execution in asyncio.run(executor.poll(int(time.time() * 1000))):
            ledger.set_execution(execution)


def _follow_files(args: argparse.Namespace, bot: FollowerBot, nautilus: NodeRunner | None) -> int:
    rules = None
    if args.ruleset:
        try:
            rules = ProviderRuleSet.model_validate_json(
                Path(args.ruleset).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise CliError(f"cannot read the rule set {args.ruleset}: {exc}") from exc
    pinned = bot.providers[0] if bot.providers else None
    texts: list[str] = []
    for name in args.signal_file:
        try:
            texts.append(Path(name).read_text(encoding="utf-8"))
        except OSError as exc:
            raise CliError(f"cannot read signal file {name}: {exc.strerror}") from exc

    async def ingest_all() -> list[LedgerRow]:
        return [await bot.ingest_text(text, provider=pinned, rules=rules) for text in texts]

    rows = asyncio.run(ingest_all())
    for row in rows:
        print(_row_line(row))
    if nautilus is not None and any(r.status == "accepted" for r in rows):
        report = nautilus.run(args.duration or 10.0)
        print(
            json.dumps({_report_key(args): report.model_dump(exclude={"trades"})}, sort_keys=True)
        )
    return 1 if any(r.status != "accepted" for r in rows) else 0


def _follow_relay(
    args: argparse.Namespace,
    bot: FollowerBot,
    nautilus: NodeRunner | None,
    profile: FollowerProfile,
) -> int:
    started = time.time()
    stop_box: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
    ready = threading.Event()
    errors: list[BaseException] = []

    async def announce() -> None:
        await bot.ready.wait()
        if args.ready_file:
            await asyncio.to_thread(Path(args.ready_file).write_text, "ready\n", encoding="utf-8")
        auth = (
            " (authenticated)" if bot.authenticated else " (not authenticated: zero-power service)"
        )
        print(
            f"ready: following {len(bot.providers)} provider(s) on {args.relay}{auth}", flush=True
        )
        ready.set()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        stop_box.append((loop, stop))
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
        note = asyncio.create_task(announce())
        try:
            await bot.run(stop, args.duration)
        finally:
            note.cancel()

    if nautilus is None:
        with contextlib.suppress(KeyboardInterrupt):  # the signal handler normally handles it
            asyncio.run(main())
    else:

        def worker() -> None:
            try:
                asyncio.run(main())
            except BaseException as exc:
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=worker, name="follower-bot", daemon=True)
        thread.start()
        ready.wait(60)
        try:
            paper = nautilus.run(args.duration)
        finally:
            if stop_box:
                with contextlib.suppress(
                    RuntimeError
                ):  # the bot already finished (--duration) and closed its loop
                    stop_box[0][0].call_soon_threadsafe(stop_box[0][1].set)
            thread.join(10)
        if errors:
            raise errors[0]
    summary = bot.summary()
    if nautilus is not None:
        summary[_report_key(args)] = paper.model_dump(exclude={"trades"})
        summary["disaster_stop_pct"] = profile.disaster_stop_pct
    summary["seconds"] = round(time.time() - started, 1)
    print(json.dumps(summary, sort_keys=True))
    return 0
