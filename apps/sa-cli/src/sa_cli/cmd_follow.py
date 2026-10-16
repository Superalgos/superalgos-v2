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
from typing import TYPE_CHECKING

from sa_cli.home import CliError, Home
from sa_cli.tsa_config import load_tsa_settings
from sa_core.models import ProviderRuleSet
from sa_engine.network.executor import NautilusPaperExecutor, PaperExecutor, SimulatedPaperExecutor
from sa_engine.network.follower import FollowerBot, FollowerError
from sa_engine.network.ledger import Ledger, LedgerRow
from sa_engine.network.replay import PriceFeed, ReplayError, ReplaySpec, load_replay_candles

if TYPE_CHECKING:
    from sa_engine.paper import FollowerExecutor

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _providers(values: list[str]) -> list[str]:
    out = [v.lower() for v in values]
    for v in out:
        if not _HEX64.match(v):
            raise CliError(f"--provider {v!r} must be a 64-character hex public key")
    return out


def _executor(args: argparse.Namespace) -> tuple[PaperExecutor, FollowerExecutor | None]:
    """(executor, the Nautilus FollowerExecutor behind it, if any)."""
    kind = args.executor or ("sim" if args.replay_spec else "nautilus")
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
        return SimulatedPaperExecutor(spec.instrument, args.capital, PriceFeed(candles)), None
    from sa_engine.paper import FollowerExecutor  # noqa: PLC0415 - Nautilus loads only for live paper trading

    inner = FollowerExecutor(args.instrument, args.capital, log_level="WARNING")
    return NautilusPaperExecutor(inner), inner


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
    providers = _providers(args.provider or [])
    if not providers and not args.signal_file:
        raise CliError(
            "name at least one provider with --provider PUBKEY, or pass --signal-file FILE"
        )
    if not args.signal_file and not args.relay:
        raise CliError("--relay URL is required to follow providers")
    ledger = Ledger(Path(args.ledger) if args.ledger else home.root / "follower" / "ledger.sqlite")
    executor, nautilus = _executor(args)
    allocation = None if args.signal_file or args.no_allocation else home.last_allocation()
    bot = FollowerBot(
        device=device, delegation=delegation, relay_url=args.relay or "ws://unused", providers=providers, executor=executor,
        ledger=ledger, verifiers=load_tsa_settings(args.tsa_config).verifiers(), allocation=allocation, allow_gaps=args.allow_gaps,
        max_age_s=args.max_age, reorder_wait_s=args.reorder_wait,
    )  # fmt: skip
    try:
        if args.signal_file:
            return _follow_files(args, bot, nautilus)
        return _follow_relay(args, bot, nautilus)
    except FollowerError as exc:
        raise CliError(str(exc)) from exc
    finally:
        ledger.close()


def _follow_files(
    args: argparse.Namespace, bot: FollowerBot, nautilus: FollowerExecutor | None
) -> int:
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
        print(json.dumps({"paper": report.model_dump(exclude={"trades"})}, sort_keys=True))
    return 1 if any(r.status != "accepted" for r in rows) else 0


def _follow_relay(
    args: argparse.Namespace, bot: FollowerBot, nautilus: FollowerExecutor | None
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
        summary["paper"] = paper.model_dump(exclude={"trades"})
    summary["seconds"] = round(time.time() - started, 1)
    print(json.dumps(summary, sort_keys=True))
    return 0
