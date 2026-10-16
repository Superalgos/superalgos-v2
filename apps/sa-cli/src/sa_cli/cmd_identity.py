"""``sa keys init``, ``sa identity`` and ``sa allocate``."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from sa_cli.home import CliError, Home, IdentityInfo
from sa_cli.relay_util import publish_sync
from sa_core.chains import BalanceOracle, ChainRegistry, JsonRpcBackend, OracleError
from sa_core.delegation import (
    MAX_DELEGATION_DAYS,
    DelegationError,
    revocation_event,
    sign_delegation,
    sign_revocation,
    verify_delegation,
)
from sa_core.keys import DeviceKey, KeyStoreError, SAKey, save_device_key, save_sa_key
from sa_core.models import BPS, Delegation, norm_addr
from sa_core.power import allocation_event, sign_allocation
from sa_engine.network import fixture


def oracle_for(balances: Path | None) -> BalanceOracle:
    """The fixture file when given (demo mode, shared with the relay), else the real chain registry."""
    if balances is not None:
        return fixture.fixture_oracle(balances)
    return BalanceOracle(ChainRegistry.default(), JsonRpcBackend(), ttl_s=300)


# ---------------------------------------------------------------- sa keys init


def keys_init(args: argparse.Namespace, home: Home) -> int:
    if home.device_path.exists():
        raise CliError(
            f"{home.device_path} already exists; refusing to overwrite a key. Pick another --home to start fresh"
        )
    key = DeviceKey.generate()
    try:
        save_device_key(home.device_path, key, home.passphrase(confirm=True), n=home.scrypt_n)
    except KeyStoreError as exc:
        raise CliError(str(exc)) from exc
    home.root.chmod(0o700)
    print(f"device key created: {home.device_path}")
    print(f"device pubkey: {key.pubkey_hex}")
    print(
        "next: `sa identity --demo` for a throwaway demo identity, or `sa identity --import-address 0x...` for read-only power"
    )
    return 0


# ---------------------------------------------------------------- sa identity


def _show_power(info: IdentityInfo, balances: Path | None) -> None:
    if info.address is None:
        raise CliError(
            "this identity has no SA address; import one with `sa identity --import-address 0x...`"
        )
    try:
        power = oracle_for(balances).power(info.address)
    except OracleError as exc:
        raise CliError(f"cannot read the token power of {info.address}: {exc}") from exc
    print(
        f"token power of {info.address}: {power.tokens} SA ({'fixture' if balances else 'chains'})"
    )


def identity(args: argparse.Namespace, home: Home) -> int:
    chosen = [bool(args.demo), args.import_address is not None, args.import_delegation is not None]
    if sum(chosen) > 1:
        raise CliError("choose one of --demo, --import-address, --import-delegation")
    if args.demo:
        return _identity_demo(args, home)
    if args.import_address is not None:
        return _identity_readonly(args, home)
    if args.import_delegation is not None:
        return _identity_external(args, home)
    info = home.identity()
    if info is None:
        raise CliError(
            "no identity yet; run `sa identity --demo` or `sa identity --import-address 0x...`"
        )
    print(json.dumps(info.model_dump(), indent=2))
    if args.power:
        _show_power(info, args.balances)
    return 0


def check_days(days: float) -> None:
    """A delegation lives at most MAX_DELEGATION_DAYS: a stolen device key must not stay valid for a year."""
    if not 0 < days <= MAX_DELEGATION_DAYS:
        raise CliError(
            f"--days {days:g} is outside 0..{MAX_DELEGATION_DAYS}: a delegation lasts at most "
            f"{MAX_DELEGATION_DAYS} days. Pick a shorter lifetime and renew it (`sa identity --demo --renew`), "
            "or revoke a lost key with `sa revoke`"
        )


def _identity_demo(args: argparse.Namespace, home: Home) -> int:
    check_days(args.days)
    device = home.load_device()
    if home.sa_path.exists() and not args.renew:
        raise CliError(
            f"{home.sa_path} already exists; add --renew to sign a fresh delegation with the same demo key"
        )
    passphrase = home.passphrase()
    if args.renew:
        sa = home.load_sa()
    else:
        sa = SAKey.generate()
        try:
            save_sa_key(home.sa_path, sa, passphrase, n=home.scrypt_n)
        except KeyStoreError as exc:
            raise CliError(str(exc)) from exc
    expires = int(time.time()) + int(args.days * 86400)
    home.delegation_path.write_text(
        sign_delegation(sa, device.pubkey_hex, expires).model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    home.save_identity(
        IdentityInfo(mode="demo", address=sa.address, device_pubkey=device.pubkey_hex)
    )
    print(f"demo identity (throwaway key, never use a real wallet here): {sa.address}")
    print(f"delegation for {device.pubkey_hex[:12]}.. valid for {args.days:g} days")
    if args.tokens is not None:
        if args.balances is None:
            raise CliError("--tokens needs --balances FILE, the fixture the relay also reads")
        fixture.set_balance(args.balances, sa.address, args.tokens)
        print(f"fixture balance: {args.tokens} SA in {args.balances}")
    return 0


def _identity_readonly(args: argparse.Namespace, home: Home) -> int:
    existing = home.identity()
    if existing is not None and existing.mode != "readonly":
        raise CliError(
            f"this home already has a {existing.mode} identity ({existing.address}); "
            "import a read-only address into a separate --home"
        )
    try:
        address = norm_addr(args.import_address)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    info = IdentityInfo(mode="readonly", address=address)
    home.save_identity(info)
    print(f"read-only identity {address}: you can read its token power but cannot sign with it")
    _show_power(info, args.balances)
    return 0


def _identity_external(args: argparse.Namespace, home: Home) -> int:
    device = home.load_device()
    try:
        d = Delegation.model_validate_json(Path(args.import_delegation).read_text(encoding="utf-8"))
        verify_delegation(d, device.pubkey_hex, int(time.time()))
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read a delegation from {args.import_delegation}: {exc}") from exc
    except DelegationError as exc:
        raise CliError(f"the delegation is not valid for this device key: {exc}") from exc
    home.delegation_path.write_text(d.model_dump_json(indent=2) + "\n", encoding="utf-8")
    home.save_identity(
        IdentityInfo(mode="external", address=d.evm_address, device_pubkey=device.pubkey_hex)
    )
    print(
        f"delegation from {d.evm_address} imported (valid until {d.expires_at}); the SA key stays in your wallet"
    )
    return 0


# ---------------------------------------------------------------- sa allocate


def parse_shares(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        pk, sep, bps = item.partition("=")
        if not sep or len(pk) != 64 or not bps.isdigit():
            raise CliError(
                f"--provider {item!r} must look like <64-hex pubkey>=<basis points>, e.g. {'ab' * 32}=2500"
            )
        out[pk.lower()] = int(bps)
    return out


def allocate(args: argparse.Namespace, home: Home) -> int:
    providers = parse_shares(args.provider or [])
    total = args.governance + sum(providers.values())
    if total > BPS:
        raise CliError(
            f"shares sum to {total} bps, more than {BPS} (100%); lower them, power cannot be counted twice"
        )
    sa = home.load_sa()
    previous = home.last_allocation()
    nonce = (
        args.nonce if args.nonce is not None else (0 if previous is None else previous.nonce + 1)
    )
    if previous is not None and nonce <= previous.nonce:
        raise CliError(
            f"nonce {nonce} is not above the last allocation's {previous.nonce}; a newer allocation needs a higher nonce"
        )
    alloc = sign_allocation(sa, nonce, args.governance, providers)
    home.allocation_path.write_text(alloc.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        f"allocation nonce {nonce}: governance {args.governance} bps, "
        + (", ".join(f"{p[:12]}..={b}" for p, b in providers.items()) or "no providers")
    )
    if args.relay:
        device = home.load_device()
        delegation = home.load_delegation(device)
        publish_sync(args.relay, device, delegation, allocation_event(device, alloc, delegation))
        print(f"published to {args.relay}")
    return 0


# ---------------------------------------------------------------- sa revoke


def revoke(args: argparse.Namespace, home: Home) -> int:
    """Sign a revocation of a device key with the SA key and publish it (kind 9334) with this home's device key.

    Followers, the API and the relay stop accepting that device key's signals and allocations for this address
    from ``--effective-at`` (default: now)."""
    device = home.load_device()
    delegation = home.load_delegation(device)
    sa = home.load_sa()
    target = args.device or device.pubkey_hex
    if len(target) != 64 or any(c not in "0123456789abcdef" for c in target):
        raise CliError(f"--device {target!r} must be a 64-character lowercase hex Nostr public key")
    effective = int(time.time()) if args.effective_at is None else args.effective_at
    if effective < 0:
        raise CliError(f"--effective-at {effective} must be a unix time in seconds, 0 or later")
    rev = sign_revocation(sa, target, effective)
    event = revocation_event(device, rev)
    publish_sync(args.relay, device, delegation, event)
    print(
        f"revoked device key {target[:12]}.. for {sa.address} effective {effective} "
        f"(event {event.id}) on {args.relay}"
    )
    return 0
