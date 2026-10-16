"""sa keys init, sa identity, sa allocate."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from cli_support import PASSPHRASE, Cli, ThreadedRelay
from sa_cli.home import CliError, Home
from sa_cli.main import main
from sa_core.delegation import sign_delegation
from sa_core.keys import DeviceKey, SAKey, load_device_key
from sa_core.models import ALLOCATION_KIND
from sa_core.power import validate_allocation_event


def test_keys_init_writes_an_encrypted_key_and_refuses_to_overwrite(cli: Cli) -> None:
    out = cli.ok("keys", "init")
    path = cli.root / "home" / "device.key.json"
    assert "device pubkey:" in out and stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    secret = load_device_key(path, PASSPHRASE).secret_bytes().hex()
    assert secret not in text  # nothing readable on disk
    assert "already exists" in cli.fail("keys", "init")


def test_passphrase_comes_from_env_or_a_prompt_and_is_confirmed(tmp_path: Path) -> None:
    answers = iter(["a-long-passphrase", "a-long-passphrase", "a-long-passphrase"])
    home = Home(
        tmp_path / "h",
        environ={"SA_SCRYPT_N": "1024"},
        prompt=lambda _: next(answers),
        interactive=lambda: True,
    )
    assert main(["keys", "init"], home=home) == 0
    assert home.load_device().pubkey_hex  # prompted once and cached: no further prompt consumed
    differing = iter(["first-passphrase", "second-passphrase"])
    other = Home(
        tmp_path / "h2",
        environ={"SA_SCRYPT_N": "1024"},
        prompt=lambda _: next(differing),
        interactive=lambda: True,
    )
    with pytest.raises(CliError, match="differ"):
        other.passphrase(confirm=True)
    quiet = Home(tmp_path / "h3", environ={}, interactive=lambda: False)
    with pytest.raises(CliError, match="SA_PASSPHRASE"):
        quiet.passphrase()


def test_short_passphrases_and_bad_kdf_settings_are_explained(cli: Cli) -> None:
    code, _, err = cli.run("keys", "init", env={"SA_PASSPHRASE": "short"})
    assert code == 2 and "at least 8" in err
    code, _, err = cli.run("keys", "init", home="other", env={"SA_SCRYPT_N": "lots"})
    assert code == 2 and "SA_SCRYPT_N" in err


def test_wrong_passphrase_and_missing_key_are_explained(cli: Cli) -> None:
    assert "sa keys init" in cli.fail("identity", "--demo")
    cli.ok("keys", "init")
    code, _, err = cli.run("identity", "--demo", env={"SA_PASSPHRASE": "not-the-passphrase"})
    assert code == 2 and "wrong passphrase" in err


def test_demo_identity_gets_a_delegation_and_fixture_balance(demo: Cli) -> None:
    home = demo.root / "home"
    info = json.loads((home / "identity.json").read_text())
    assert info["mode"] == "demo" and info["address"].startswith("0x")
    assert json.loads((demo.root / "balances.json").read_text()) == {info["address"]: "1000"}
    assert (home / "sa.key.json").exists() and (home / "delegation.json").exists()
    assert str(stat.S_IMODE((home / "sa.key.json").stat().st_mode)) == str(0o600)
    shown = demo.ok("identity", "--power", "--balances", demo.root / "balances.json")
    assert info["address"] in shown and "1000 SA (fixture)" in shown
    assert "--renew" in demo.fail("identity", "--demo")
    out = demo.ok("identity", "--demo", "--renew", "--days", "1")
    assert "valid for 1 days" in out
    assert (
        json.loads((home / "identity.json").read_text())["address"] == info["address"]
    )  # same key, fresh delegation


def test_demo_tokens_need_a_fixture_file(cli: Cli) -> None:
    cli.ok("keys", "init")
    assert "--balances" in cli.fail("identity", "--demo", "--tokens", "5")


def test_read_only_identity_shows_power_but_cannot_sign(cli: Cli) -> None:
    balances = cli.root / "balances.json"
    address = SAKey.generate().address
    balances.write_text(json.dumps({address: "42"}))
    out = cli.ok("identity", "--import-address", address, "--balances", balances)
    assert "read-only" in out and "42 SA" in out
    assert '"mode": "readonly"' in cli.ok("identity")
    err = cli.fail("allocate", "--governance", "1000")
    assert "readonly identity" in err and "sa identity --demo" in err


def test_readonly_import_does_not_replace_a_demo_identity(demo: Cli) -> None:
    assert "separate --home" in demo.fail("identity", "--import-address", "0x" + "ab" * 20)


def test_identity_flags_are_exclusive_and_the_empty_home_is_explained(cli: Cli) -> None:
    assert "choose one" in cli.fail("identity", "--demo", "--import-address", "0x" + "ab" * 20)
    assert "no identity yet" in cli.fail("identity")
    cli.ok("keys", "init")
    assert "not an EVM address" in cli.fail("identity", "--import-address", "0x123")


def test_a_delegation_signed_elsewhere_can_be_imported(cli: Cli, tmp_path: Path) -> None:
    cli.ok("keys", "init")
    device = cli.home().load_device()
    wallet = SAKey.generate()  # stands in for the user's own wallet; its key never reaches the CLI
    good = sign_delegation(wallet, device.pubkey_hex, int(time.time()) + 3600)
    file = tmp_path / "delegation.json"
    file.write_text(good.model_dump_json())
    out = cli.ok("identity", "--import-delegation", file)
    assert wallet.address in out and "stays in your wallet" in out
    assert json.loads((cli.root / "home" / "identity.json").read_text())["mode"] == "external"
    assert not (cli.root / "home" / "sa.key.json").exists()
    foreign = sign_delegation(wallet, DeviceKey.generate().pubkey_hex, int(time.time()) + 3600)
    file.write_text(foreign.model_dump_json())
    assert "not valid for this device key" in cli.fail("identity", "--import-delegation", file)
    assert "cannot read a delegation" in cli.fail(
        "identity", "--import-delegation", tmp_path / "nope.json"
    )


def test_expired_or_foreign_delegation_files_are_refused_at_use(demo: Cli) -> None:
    path = demo.root / "home" / "delegation.json"
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({**doc, "expires_at": 1}))
    home = demo.home()
    with pytest.raises(CliError, match="expired"):
        home.load_delegation(home.load_device())
    path.write_text(json.dumps({**doc, "nostr_pubkey": "ab" * 32}))
    with pytest.raises(CliError, match="create a new delegation"):
        home.load_delegation(home.load_device())
    path.write_text("nope")
    with pytest.raises(CliError, match="not a delegation"):
        home.load_delegation(home.load_device())
    path.unlink()
    with pytest.raises(CliError, match="no delegation"):
        home.load_delegation(home.load_device())


def test_allocate_signs_increments_the_nonce_and_publishes(demo: Cli, relay: ThreadedRelay) -> None:
    provider = "ab" * 32
    out = demo.ok(
        "allocate", "--provider", f"{provider}=2500", "--governance", "5000", "--relay", relay.url
    )
    assert "nonce 0" in out and "published" in out
    demo.ok("allocate", "--provider", f"{provider}=100")
    events = relay.events(ALLOCATION_KIND)
    assert len(events) == 1  # the second allocation was not sent (no --relay)
    alloc = validate_allocation_event(events[0])
    assert alloc.providers == {provider: 2500} and alloc.governance == 5000 and alloc.nonce == 0
    assert demo.home().last_allocation() is not None and demo.home().last_allocation().nonce == 1  # type: ignore[union-attr]
    assert "not above" in demo.fail("allocate", "--nonce", "1")


def test_allocate_rejects_bad_shares_before_asking_for_the_key(demo: Cli) -> None:
    assert "more than 10000" in demo.fail(
        "allocate", "--provider", f"{'ab' * 32}=9000", "--governance", "2000"
    )
    assert "must look like" in demo.fail("allocate", "--provider", "nope")
    assert "must look like" in demo.fail("allocate", "--provider", f"{'ab' * 32}=lots")


def test_allocate_publish_failure_is_reported(demo: Cli) -> None:
    err = demo.fail("allocate", "--governance", "100", "--relay", "ws://127.0.0.1:1")
    assert "cannot connect" in err
    with ThreadedRelay(reject=lambda ev: "invalid: nonce too low") as r:
        assert "refused the event" in demo.fail("allocate", "--governance", "100", "--relay", r.url)


def test_main_prints_errors_without_tracebacks(cli: Cli) -> None:
    err = cli.fail("verify", "missing.json")
    assert "cannot read missing.json" in err


# ---------------------------------------------------------------- security review M3: lifetime cap and sa revoke


def test_m3_delegation_lifetime_is_capped_at_ninety_days(cli: Cli) -> None:
    cli.ok("keys", "init")
    for days in ("91", "365", "1e9", "0", "-5"):
        err = cli.fail("identity", "--demo", "--days", days)
        assert "a delegation lasts at most 90 days" in err and "sa revoke" in err, days
    assert not (cli.root / "home" / "delegation.json").exists()  # nothing was signed
    assert "valid for 90 days" in cli.ok("identity", "--demo", "--days", "90")
    assert "valid for 90 days" in cli.ok("identity", "--demo", "--renew", "--days", "90")
    assert "at most 90 days" in cli.fail("identity", "--demo", "--renew", "--days", "90.5")
    doc = json.loads((cli.root / "home" / "delegation.json").read_text())
    assert doc["expires_at"] <= int(time.time()) + 90 * 86400 + 5


def test_m3_revoke_publishes_a_valid_kind_9334_for_this_device_key(
    demo: Cli, relay: ThreadedRelay
) -> None:
    from sa_core.delegation import validate_revocation_event, verify_delegation
    from sa_core.models import REVOCATION_KIND

    home = demo.home()
    device = home.load_device()
    before = int(time.time())
    out = demo.ok("revoke", "--relay", relay.url)
    assert f"revoked device key {device.pubkey_hex[:12]}" in out
    (ev,) = relay.events(REVOCATION_KIND)
    rev = validate_revocation_event(ev)
    assert rev.nostr_pubkey == device.pubkey_hex and rev.evm_address == home.load_sa().address
    assert before <= rev.effective_at <= int(time.time())
    assert ev.pubkey == device.pubkey_hex  # published by this home's key; any key would do
    with pytest.raises(Exception, match="revoked"):
        verify_delegation(
            home.load_delegation(device), device.pubkey_hex, rev.effective_at + 1, [rev]
        )


def test_m3_revoke_another_device_key_from_a_chosen_time(demo: Cli, relay: ThreadedRelay) -> None:
    from sa_core.delegation import validate_revocation_event
    from sa_core.models import REVOCATION_KIND

    lost = DeviceKey.generate().pubkey_hex
    demo.ok("revoke", "--device", lost, "--effective-at", "1700000000", "--relay", relay.url)
    rev = validate_revocation_event(relay.events(REVOCATION_KIND)[0])
    assert (rev.nostr_pubkey, rev.effective_at) == (lost, 1_700_000_000)


def test_m3_revoke_errors_say_what_to_fix(demo: Cli, relay: ThreadedRelay) -> None:
    assert "64-character lowercase hex" in demo.fail(
        "revoke", "--device", "nothex", "--relay", relay.url
    )
    assert "unix time" in demo.fail("revoke", "--effective-at", "-1", "--relay", relay.url)
    assert "cannot connect" in demo.fail("revoke", "--relay", "ws://127.0.0.1:1")
    balances = demo.root / "b.json"
    balances.write_text("{}")
    demo.ok("keys", "init", home="ro")
    demo.ok(
        "identity", "--import-address", SAKey.generate().address, "--balances", balances, home="ro"
    )
    assert "no delegation" in demo.fail(
        "revoke", "--relay", relay.url, home="ro"
    )  # no SA key to sign with
