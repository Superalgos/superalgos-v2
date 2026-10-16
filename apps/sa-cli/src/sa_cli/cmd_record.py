"""``sa record`` (deterministic track record of a bundle) and ``sa verify`` (bundle or signal file)."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from pydantic import ValidationError

from sa_cli.home import CliError, Home
from sa_cli.tsa_config import load_tsa_settings
from sa_core.anchors import anchored_time
from sa_core.bundle import BundleError, bundle_from_json, check_bundle
from sa_core.models import Bundle, NostrEvent
from sa_core.signals import SignalError, signal_from_text
from sa_core.trackrecord import Candle, TrackRecordError, compute
from sa_engine.network.replay import (
    ReplayError,
    ReplaySpec,
    fetch_rest_candles,
    load_replay_candles,
)

DAY_MS = 86_400_000


def _read_bundle(path: str) -> Bundle:
    try:
        return bundle_from_json(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError(f"cannot read bundle {path}: {exc.strerror}") from exc
    except BundleError as exc:
        raise CliError(str(exc)) from exc


def candles_for(
    args: argparse.Namespace, instruments: list[str], first_ms: int, last_ms: int
) -> dict[str, list[Candle]]:
    try:
        if args.replay_spec:
            spec = ReplaySpec.load(Path(args.replay_spec))
            extra = [i for i in instruments if i != spec.instrument]
            if extra:
                raise CliError(f"the bundle trades {extra} but the replay was of {spec.instrument}")
            return {
                spec.instrument: load_replay_candles(
                    spec, data_dir=args.data_dir, tolerance_s=args.tolerance
                )
            }
        end = min(last_ms + 7 * DAY_MS, int(time.time() * 1000))
        return {
            i: fetch_rest_candles(i.partition(":")[2], first_ms - 60_000, end) for i in instruments
        }
    except ReplayError as exc:
        raise CliError(str(exc)) from exc


def record(args: argparse.Namespace, home: Home) -> int:
    bundle = _read_bundle(args.bundle)
    check = check_bundle(bundle)
    sigs = [i.signal for i in check.items if i.signal is not None]
    if not sigs:
        raise CliError(
            f"{args.bundle} holds no signal that can be scored (private signals need their reveals: export with --reveal)"
        )
    instruments = sorted({s.instrument for s in sigs})
    candles = candles_for(
        args, instruments, min(s.created_ms for s in sigs), max(s.created_ms for s in sigs)
    )
    try:
        result = compute(
            bundle,
            candles,
            verifiers=load_tsa_settings(args.tsa_config).verifiers(),
            tolerance_s=args.tolerance,
        )
    except TrackRecordError as exc:
        raise CliError(str(exc)) from exc
    text = result.to_json()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"track record written: {args.output}")
    else:
        print(text, end="")
    return 0


def verify(args: argparse.Namespace, home: Home) -> int:
    try:
        text = Path(args.path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read {args.path}: {exc.strerror}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise CliError(
            f"{args.path} is not JSON ({exc}); pass a bundle or a signal event file"
        ) from exc
    if isinstance(doc, dict) and "events" in doc and "provider" in doc:
        return _verify_bundle(args, text)
    if isinstance(doc, dict) and {"id", "pubkey", "sig", "kind"} <= set(doc):
        return _verify_signal(args, text)
    raise CliError(
        f"{args.path} is neither a bundle (events + provider) nor a Nostr event (id, pubkey, sig, kind)"
    )


def _verify_bundle(args: argparse.Namespace, text: str) -> int:
    try:
        bundle = bundle_from_json(text)
    except BundleError as exc:
        raise CliError(str(exc)) from exc
    check = check_bundle(bundle)
    verifiers = load_tsa_settings(args.tsa_config).verifiers()
    anchored = sum(
        anchored_time(bytes.fromhex(i.event.id), bundle.proofs, verifiers) is not None
        for i in check.items
    )
    print(
        f"provider {bundle.provider}: {len(check.items)} chain items, {anchored} anchored (tsa), {len(bundle.proofs)} proofs"
    )
    for problem in check.problems:
        print(f"  problem: {problem}")
    for gap in check.gaps:
        print(f"  gap: {gap}")
    if check.unrevealed:
        print(f"  unrevealed commitments: {len(check.unrevealed)} (count as misses)")
    if check.complete:
        print(
            "bundle is complete"
            + (
                ""
                if check.head_checked
                else " (tail truncation cannot be seen without an external head)"
            )
        )
        return 0
    print("bundle is NOT complete: do not trust its record")
    return 1


def _verify_signal(args: argparse.Namespace, text: str) -> int:
    try:
        event = NostrEvent.model_validate_json(text)
    except ValidationError as exc:
        raise CliError(f"{args.path} is not a valid event: {exc.errors()[0]['msg']}") from exc
    if event.tag_values("p"):
        raise CliError(
            "this is a private signal (NIP-44 ciphertext): verify it with `sa follow --signal-file`, which holds the follower key"
        )
    try:
        signed = signal_from_text(text, int(time.time()), args.sa_address)
    except SignalError as exc:
        print(f"signal is INVALID: {exc}")
        return 1
    s = signed.signal
    print(
        f"signal is valid: {s.strategy_id} seq {s.seq} {s.action} {s.side} {s.size_fraction} {s.instrument}"
    )
    print(
        f"  provider {signed.event.pubkey}, delegated by {signed.delegation.evm_address} until {signed.delegation.expires_at}"
    )
    return 0
