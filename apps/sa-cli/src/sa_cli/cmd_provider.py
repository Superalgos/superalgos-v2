"""``sa provider register | run | export``."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from sa_cli.cmd_identity import oracle_for
from sa_cli.home import CliError, Home
from sa_cli.relay_util import publish_sync
from sa_cli.tsa_config import load_tsa_settings
from sa_core.bundle import write_bundle
from sa_core.keys import DeviceKey
from sa_core.models import Delegation, PrivateRule, Tier
from sa_core.power import DEFAULT_TIERS, ruleset_event, sign_ruleset
from sa_engine.network.entitlement import RelayEntitlements, UrlQuerier
from sa_engine.network.journal import JournalError, ProviderStore
from sa_engine.network.provider import ProviderBot, ProviderError, export_store, replay_run_id
from sa_engine.network.replay import ReplayError, ReplaySpec, collect_intents
from sa_engine.schema import StrategyDoc, StrategyValidationError, validate_strategy


def load_strategy(path: str) -> StrategyDoc:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(
            f"cannot read strategy {path!r}: {exc.strerror}; pass the path of a strategy JSON file"
        ) from exc
    try:
        return validate_strategy(text)
    except StrategyValidationError as exc:
        raise CliError(
            f"{path} is not a valid strategy:\n  " + "\n  ".join(str(i) for i in exc.issues)
        ) from exc


def state_dir(args: argparse.Namespace, home: Home, doc: StrategyDoc) -> Path:
    return Path(args.state) if args.state else home.root / "provider" / doc.name


def parse_tier(text: str) -> Tier:
    power, sep, delay = text.partition("@")
    try:
        if not sep:
            raise ValueError("missing @")
        return Tier(min_power=int(power), delay_fraction=float(delay))
    except ValueError as exc:
        raise CliError(
            f"--tier {text!r} must look like MIN_POWER@DELAY_FRACTION, e.g. 100000@0.1 ({exc})"
        ) from exc


# ---------------------------------------------------------------- register


def register(args: argparse.Namespace, home: Home) -> int:
    doc = load_strategy(args.strategy)
    device = home.load_device()
    delegation = home.load_delegation(device)
    sa = home.load_sa()
    try:
        if args.private is not None:
            rules = sign_ruleset(
                sa,
                device.pubkey_hex,
                doc.timeframe_seconds,
                private=PrivateRule(min_power=args.private),
            )
        else:
            tiers = [parse_tier(t) for t in args.tier] if args.tier else list(DEFAULT_TIERS)
            rules = sign_ruleset(sa, device.pubkey_hex, doc.timeframe_seconds, tiers=tiers)
    except ValueError as exc:
        raise CliError(f"invalid rule set: {exc}") from exc
    store = ProviderStore(state_dir(args, home, doc))
    store.save_ruleset(rules)
    publish_sync(args.relay, device, delegation, ruleset_event(device, rules, delegation))
    kind = (
        f"private (threshold {args.private} SA)"
        if args.private is not None
        else f"{len(rules.tiers or ())} public tiers"
    )
    print(
        f"registered provider {device.pubkey_hex} on {args.relay}: timeframe {doc.timeframe} ({doc.timeframe_seconds}s), {kind}"
    )
    print(f"state directory: {store.dir}")
    return 0


# ---------------------------------------------------------------- run


def _build_bot(
    args: argparse.Namespace,
    doc: StrategyDoc,
    device: DeviceKey,
    delegation: Delegation,
    store: ProviderStore,
) -> ProviderBot:
    try:
        rules = store.load_ruleset()
    except JournalError as exc:
        raise CliError(str(exc)) from exc
    if rules.provider != device.pubkey_hex:
        raise CliError(
            f"{store.ruleset_path} is for provider {rules.provider[:12]}.., but this home's device key is {device.pubkey_hex[:12]}.."
        )
    if rules.timeframe != doc.timeframe_seconds:
        raise CliError(
            f"strategy {doc.name!r} runs on {doc.timeframe} ({doc.timeframe_seconds}s) but the registered rule set binds {rules.timeframe}s; "
            "run `sa provider register` again"
        )
    entitlements = None
    if rules.private is not None:
        entitlements = RelayEntitlements(
            UrlQuerier(args.relay),
            device.pubkey_hex,
            rules,
            oracle_for(args.balances),
            delay_d=args.delay_d,
        )
    stamper = None if args.no_anchor else load_tsa_settings(args.tsa_config).stamper()
    return ProviderBot(
        device=device, delegation=delegation, rules=rules, strategy_id=doc.name, store=store, relay_url=args.relay,
        entitlements=entitlements, stamper=stamper,
    )  # fmt: skip


def run(args: argparse.Namespace, home: Home) -> int:
    if (args.replay is None) == (not args.paper):
        raise CliError(
            "choose one: --replay START END (replayed history) or --paper (live data, simulated fills)"
        )
    doc = load_strategy(args.strategy)
    device = home.load_device()
    delegation = home.load_delegation(device)
    store = ProviderStore(state_dir(args, home, doc))
    bot = _build_bot(args, doc, device, delegation, store)
    if args.replay is not None:
        return _run_replay(args, doc, bot)
    return _run_paper(args, doc, bot)


def _finish(args: argparse.Namespace, bot: ProviderBot, started: float) -> int:
    summary = {
        "provider": bot.device.pubkey_hex,
        "signals": bot.stats.signals,
        "sent": bot.stats.sent,
        "anchored": bot.stats.anchored,
        "rejected": [{"event": e[:12], "reason": r} for e, r in bot.stats.rejected],
        "anchor_errors": bot.stats.anchor_errors[-3:],
        "seconds": round(time.time() - started, 1),
        "journal": bot.store.counts(),
    }
    if args.export:
        write_bundle(Path(args.export), bot.export(reveal=args.reveal))
        summary["bundle"] = str(args.export)
    print(json.dumps(summary, sort_keys=True))
    return 1 if bot.stats.rejected else 0


def _run_replay(args: argparse.Namespace, doc: StrategyDoc, bot: ProviderBot) -> int:
    start, end = args.replay
    try:
        intents = collect_intents(
            doc, args.instrument, start, end, capital=args.capital, data_dir=args.data_dir
        )
        spec = ReplaySpec(
            instrument=args.instrument,
            start=start,
            end=end,
            speed=args.speed,
            wall0_ms=int(
                (args.start_at if args.start_at is not None else time.time() + args.lead) * 1000
            ),
        )
    except (ReplayError, ValueError) as exc:
        raise CliError(f"cannot prepare the replay: {exc}") from exc
    spec.save(bot.store.replay_path)
    run_id = replay_run_id(doc.model_dump_json(), spec)
    print(
        f"replay {start}..{end} of {args.instrument} at {args.speed:g}x: {len(intents)} signals over {spec.wall_duration_s():.0f} s",
        flush=True,
    )
    print(f"replay spec: {bot.store.replay_path}", flush=True)
    started = time.time()

    async def main() -> None:
        async with bot:
            effective = await bot.run_replay(intents, spec, run_id)
            if effective != spec:
                effective.save(bot.store.replay_path)
            await bot.drain(anchor_timeout_s=args.anchor_timeout)

    _guard(main())
    return _finish(args, bot, started)


def _guard(coro: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(coro)
    except ProviderError as exc:
        raise CliError(str(exc)) from exc
    except TimeoutError as exc:
        raise CliError(
            "timed out waiting for the TSA anchors; the signals are published, re-run to anchor them"
        ) from exc


def _run_paper(args: argparse.Namespace, doc: StrategyDoc, bot: ProviderBot) -> int:
    from sa_engine.paper import run_paper  # noqa: PLC0415 - Nautilus loads only when it is needed

    ready = threading.Event()
    failure: list[BaseException] = []
    loop_box: list[asyncio.AbstractEventLoop] = []
    stop_box: list[asyncio.Event] = []

    async def bot_main() -> None:
        stop = asyncio.Event()
        stop_box.append(stop)
        loop_box.append(asyncio.get_running_loop())
        async with bot:
            ready.set()
            await stop.wait()
            await bot.drain(anchor_timeout_s=args.anchor_timeout)

    def thread_main() -> None:
        try:
            asyncio.run(bot_main())
        except BaseException as exc:
            failure.append(exc)
            ready.set()

    worker = threading.Thread(target=thread_main, name="provider-bot", daemon=True)
    worker.start()
    if not ready.wait(30) or failure:
        raise CliError(
            f"the provider bot did not start: {failure[0] if failure else 'timed out connecting to the relay'}"
        )
    started = time.time()
    print(
        f"paper trading {doc.name} on {args.instrument}: every order becomes a signal", flush=True
    )
    try:
        run_paper(
            doc,
            args.instrument,
            args.capital,
            duration_s=args.duration,
            intent_hook=bot.live_hook(),
            log_level="WARNING",
        )
    finally:
        loop_box[0].call_soon_threadsafe(stop_box[0].set)
        worker.join(args.anchor_timeout + 30)
    if failure:
        raise CliError(f"the provider bot failed: {failure[0]}")
    return _finish(args, bot, started)


# ---------------------------------------------------------------- export


def export(args: argparse.Namespace, home: Home) -> int:
    store = ProviderStore(Path(args.state))
    try:
        rules = store.load_ruleset()
    except JournalError as exc:
        raise CliError(str(exc)) from exc
    bundle = export_store(store, rules.provider, reveal=args.reveal, exported_at=int(time.time()))
    write_bundle(Path(args.output), bundle)
    print(
        f"bundle written: {args.output} ({len(bundle.events)} events, {len(bundle.proofs)} proofs, {len(bundle.reveals)} reveals)"
    )
    return 0
