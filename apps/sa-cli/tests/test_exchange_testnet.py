"""SEP-3.17: `sa exchange add binance-testnet`, `sa follow --executor testnet`, and no secret anywhere it should not be."""

from __future__ import annotations

import logging
import stat
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from pydantic import SecretStr

import sa_engine.testnet as testnet_module
from cli_support import PASSPHRASE, Cli, ThreadedRelay
from sa_cli.home import CliError, Home
from sa_cli.main import main
from sa_core.keys import ExchangeCredentials
from sa_core.models import Signal
from sa_core.power import DEFAULT_TIERS, ruleset_event, sign_ruleset
from sa_core.signals import build_signal_event
from sa_engine.intent import OrderIntent
from sa_engine.network.executor import Execution
from sa_engine.network.ledger import Ledger

KEY = "CANARYKEY-not-a-real-key-0123456789"


def _pem(key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey, protect: bool = False) -> str:
    enc = BestAvailableEncryption(b"pw") if protect else NoEncryption()
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, enc).decode()


SECRET = _pem(
    ed25519.Ed25519PrivateKey.generate()
)  # a throwaway Ed25519 key, the only kind Binance spot accepts
BODY = "".join(SECRET.strip().splitlines()[1:-1])
ENV = {"SA_BINANCE_TESTNET_API_KEY": KEY, "SA_BINANCE_TESTNET_API_SECRET": SECRET}
ADD = ("exchange", "add", "binance-testnet")


def everything_under(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


class FakeTestnetFollower:
    """Stands in for sa_engine.testnet.SpotTestnetFollower: no node, no network."""

    instances: ClassVar[list[FakeTestnetFollower]] = []
    fail_with: ClassVar[Exception | None] = None
    events: ClassVar[list[Execution]] = []

    def __init__(self, instrument: str, credentials: ExchangeCredentials, **kw: Any) -> None:
        self.instrument, self.credentials, self.kw = instrument, credentials, kw
        self.sent: list[tuple[OrderIntent, str]] = []
        self.pending: list[Execution] = []
        self.results: list[Any] = []
        self.thread = ""
        FakeTestnetFollower.instances.append(self)

    def submit(self, intent: OrderIntent, ref: str) -> None:
        self.sent.append((intent, ref))

    def drain(self) -> list[Execution]:
        out, self.pending = self.pending, []
        return out

    def run(self, duration_s: float | None = None) -> Any:
        self.thread = threading.current_thread().name
        if FakeTestnetFollower.fail_with is not None:
            raise FakeTestnetFollower.fail_with
        time.sleep(min(duration_s or 0.3, 0.3))
        self.pending = [
            Execution(ref, True, reason="accepted by the Binance testnet", venue_order_id="98765")
            for _, ref in self.sent
        ] + [
            Execution(
                ref,
                True,
                True,
                reason="filled",
                qty="0.001",
                price=50_000.0,
                fee=0.05,
                venue_order_id="98765",
            )
            for _, ref in self.sent
        ]

        class Report:
            def model_dump(self, exclude: set[str] | None = None) -> dict[str, Any]:
                return {"placed": 1}

        return Report()


@pytest.fixture
def fake_testnet(monkeypatch: pytest.MonkeyPatch) -> type[FakeTestnetFollower]:
    FakeTestnetFollower.instances = []
    FakeTestnetFollower.fail_with = None
    monkeypatch.setattr(testnet_module, "SpotTestnetFollower", FakeTestnetFollower)
    return FakeTestnetFollower


def signed(demo: Cli) -> tuple[str, str]:
    h = demo.home()
    device, delegation = h.load_device(), h.load_delegation(h.load_device())
    sig = Signal.model_validate(
        {
            "strategy_id": "s",
            "seq": 1,
            "prev": None,
            "instrument": "BINANCE:BTCUSDT",
            "side": "buy",
            "action": "open",
            "size_fraction": 0.001,
            "order_type": "market",
            "timeframe": 3600,
            "created_ms": int(time.time() * 1000),
        }
    )
    return build_signal_event(device, sig, delegation).model_dump_json(), device.pubkey_hex


# ---------------------------------------------------------------- sa exchange add


def test_add_from_the_environment_stores_one_encrypted_0600_file(cli: Cli) -> None:
    code, out, err = cli.run(*ADD, env=ENV)
    assert code == 0 and "binance-testnet" in out
    path = cli.home().exchange_path("binance-testnet")
    assert (
        stat.S_IMODE(path.stat().st_mode) == 0o600 and path.parent == cli.home().root / "exchange"
    )
    data = path.read_bytes()
    assert KEY.encode() not in data and SECRET.encode() not in data and b"CANARY" not in data
    for text in (out, err):
        assert KEY not in text and SECRET not in text
    creds = cli.home().load_exchange("binance-testnet")
    assert (creds.api_key.get_secret_value(), creds.api_secret.get_secret_value()) == (KEY, SECRET)


def test_add_prompts_when_the_environment_has_no_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[str] = []
    answers = iter(
        [f" {KEY} ", SECRET, PASSPHRASE, PASSPHRASE]
    )  # key, secret, then the passphrase twice

    def prompt(label: str) -> str:
        asked.append(label)
        return next(answers)

    home = Home(
        tmp_path / "h", environ={"SA_SCRYPT_N": "1024"}, prompt=prompt, interactive=lambda: True
    )
    assert main(list(ADD), home=home) == 0
    out = capsys.readouterr()
    assert [a.split(" ")[0] for a in asked] == ["Binance", "Binance", "Key", "Repeat"]
    assert (
        "from prompt" in out.out
        and KEY not in out.out + out.err
        and SECRET not in out.out + out.err
    )
    assert (
        home.load_exchange("binance-testnet").api_key.get_secret_value() == KEY
    )  # whitespace stripped


def test_add_errors_say_what_to_do(cli: Cli, tmp_path: Path) -> None:
    code, _, err = cli.run(*ADD, home="h2", env={"SA_BINANCE_TESTNET_API_KEY": KEY})
    assert code == 2 and "set both" in err and KEY not in err
    code, _, err = cli.run(*ADD, home="h3")  # no env, no terminal
    assert code == 2 and "no Binance testnet API key" in err
    code, _, err = cli.run(*ADD, home="h4", env={**ENV, "SA_BINANCE_TESTNET_API_SECRET": "  "})
    assert code == 2 and "must not be empty" in err
    assert cli.run(*ADD, home="h6", env=ENV)[0] == 0
    code, _, err = cli.run(*ADD, home="h6", env=ENV)  # second time
    assert code == 2 and "already exists; delete it first" in err and SECRET not in err
    code, _, err = cli.run(*ADD, home="h7", env={**ENV, "SA_PASSPHRASE": "short"})
    assert code == 2 and "passphrase has 5 characters" in err and SECRET not in err


def test_add_accepts_the_pem_without_its_begin_and_end_lines(cli: Cli) -> None:
    code, _, _ = cli.run(*ADD, env={**ENV, "SA_BINANCE_TESTNET_API_SECRET": BODY})
    assert code == 0
    assert cli.home().load_exchange("binance-testnet").api_secret.get_secret_value() == SECRET


@pytest.mark.parametrize(
    ("secret", "what"),
    [
        ("CANARYHMACSECRET" + "a1" * 24, "not a PKCS#8 PEM"),
        (_pem(rsa.generate_private_key(65537, 2048)), "RSA key"),
        (_pem(ed25519.Ed25519PrivateKey.generate(), protect=True), "encrypted PEM"),
    ],
    ids=["hmac", "rsa", "encrypted"],
)
def test_add_refuses_a_secret_that_is_not_an_ed25519_pem_before_writing(
    cli: Cli, secret: str, what: str
) -> None:
    code, out, err = cli.run(*ADD, env={**ENV, "SA_BINANCE_TESTNET_API_SECRET": secret})
    assert code == 2 and out == ""
    assert (
        err.count("\n") == 1
        and what in err
        and "Ed25519" in err
        and "testnet.binance.vision" in err
    )
    assert secret not in err and "".join(secret.splitlines()[1:-1] or [secret]) not in err
    assert not cli.home().exchange_path("binance-testnet").exists()


def test_follow_refuses_a_stored_secret_that_is_not_ed25519_with_one_line(
    demo: Cli, tmp_path: Path, fake_testnet: type[FakeTestnetFollower]
) -> None:
    bad = "CANARYHMACSECRET" + "b2" * 24
    demo.home().save_exchange(
        "binance-testnet", ExchangeCredentials(api_key=SecretStr(KEY), api_secret=SecretStr(bad))
    )
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    code, out, err = demo.run(*follow_args(tmp_path, file, provider))
    assert code == 2 and out == "" and err.count("\n") == 1
    assert err.startswith("sa: error: stored binance-testnet credentials: ") and "Ed25519" in err
    assert bad not in err and KEY not in err and not fake_testnet.instances


def test_loading_with_the_wrong_passphrase_or_a_missing_file_is_a_clear_error(cli: Cli) -> None:
    with pytest.raises(CliError, match=r"run `sa exchange add binance-testnet`"):
        cli.home().load_exchange("binance-testnet")
    cli.run(*ADD, env=ENV)
    with pytest.raises(CliError, match="wrong passphrase") as info:
        Home(cli.root / "home", environ={"SA_PASSPHRASE": "another passphrase"}).load_exchange(
            "binance-testnet"
        )
    assert SECRET not in str(info.value)


# ---------------------------------------------------------------- sa follow --executor testnet


def follow_args(tmp_path: Path, file: Path, provider: str) -> list[object]:
    return [
        "follow", "--executor", "testnet", "--instrument", "BINANCE:BTCUSDT", "--signal-file", file,
        "--provider", provider, "--ledger", tmp_path / "l.sqlite", "--duration", "0.2",
    ]  # fmt: skip


def test_missing_credentials_is_a_one_line_error_naming_the_command(
    demo: Cli, tmp_path: Path
) -> None:
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    code, out, err = demo.run(*follow_args(tmp_path, file, provider))
    assert code == 2 and out == ""
    assert (
        err == "sa: error: no binance-testnet credentials; run `sa exchange add binance-testnet`\n"
    )


def test_follow_uses_the_stored_credentials_and_records_the_exchange_order_id(
    demo: Cli, tmp_path: Path, fake_testnet: type[FakeTestnetFollower]
) -> None:
    assert demo.run(*ADD, env=ENV)[0] == 0
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    code, out, err = demo.run(*follow_args(tmp_path, file, provider))
    assert code == 0, err
    follower = fake_testnet.instances[0]
    assert follower.instrument == "BINANCE:BTCUSDT"
    assert (
        follower.credentials.api_key.get_secret_value() == KEY
    )  # the stored, decrypted credentials
    assert len(follower.sent) == 1 and follower.sent[0][0].size_fraction == 0.001
    assert follower.kw == {"disaster_stop_pct": 10.0}  # the profile's default (SEP-14.5)
    assert '"testnet": {"placed": 1}' in out and "paper" not in out
    ledger = Ledger(tmp_path / "l.sqlite")
    (row,) = ledger.rows()
    ledger.close()
    assert (
        row.venue_order_id == "98765"
        and row.filled
        and row.qty == "0.001"
        and row.price == 50_000.0
    )
    assert row.placed and row.event_id == follower.sent[0][1] and row.status == "accepted"


def test_relay_mode_runs_testnet_nautilus_on_the_main_thread(
    demo: Cli, relay: ThreadedRelay, tmp_path: Path, fake_testnet: type[FakeTestnetFollower]
) -> None:
    demo.run(*ADD, env=ENV)
    h = demo.home()
    device, delegation, sa = h.load_device(), h.load_delegation(h.load_device()), h.load_sa()
    relay.inject(
        ruleset_event(
            device, sign_ruleset(sa, device.pubkey_hex, 3600, tiers=DEFAULT_TIERS), delegation
        )
    )
    code, out, err = demo.run(
        "follow", "--executor", "testnet", "--relay", relay.url, "--provider", device.pubkey_hex,
        "--ledger", tmp_path / "l.sqlite", "--duration", "0.4",
    )  # fmt: skip
    assert code == 0, err
    assert fake_testnet.instances[0].thread == threading.main_thread().name and '"testnet":' in out
    assert '"disaster_stop_pct": 10.0' in out  # the summary says which profile setting was in force


# ---------------------------------------------------------------- Anti: secret leakage


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(f"node stopped before starting: auth failed for key {KEY} secret {SECRET}"),
        TimeoutError(f"node did not start; {SECRET}"),
    ],
)
def test_anti_a_node_failure_never_shows_the_secret_anywhere(
    demo: Cli,
    tmp_path: Path,
    fake_testnet: type[FakeTestnetFollower],
    failure: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    code, out, err = demo.run(*ADD, env=ENV)
    assert code == 0
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    fake_testnet.fail_with = failure
    code, fout, ferr = demo.run(*follow_args(tmp_path, file, provider))
    assert (
        code == 2
        and ferr.startswith("sa: error: the trading node failed: ")
        and "[REDACTED]" in ferr
    )
    haystack = out + err + fout + ferr + caplog.text + repr(caplog.records)
    assert KEY not in haystack and SECRET not in haystack
    stored = everything_under(tmp_path)  # ledger, home (encrypted key file) and signal file
    assert KEY.encode() not in stored and SECRET.encode() not in stored


def test_anti_the_secret_is_not_in_exceptions_from_constructing_the_follower(
    demo: Cli,
    tmp_path: Path,
    fake_testnet: type[FakeTestnetFollower],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo.run(*ADD, env=ENV)

    def boom(*a: Any, **kw: Any) -> None:
        raise ValueError(f"bad config for {SECRET}")

    monkeypatch.setattr(testnet_module, "SpotTestnetFollower", boom)
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    code, out, err = demo.run(*follow_args(tmp_path, file, provider))
    assert code == 2 and err == "sa: error: bad config for [REDACTED]\n" and SECRET not in out


def test_anti_credentials_never_print_or_repr(
    fake_testnet: type[FakeTestnetFollower], cli: Cli
) -> None:
    cli.run(*ADD, env=ENV)
    creds = cli.home().load_exchange("binance-testnet")
    for text in (repr(creds), str(creds), creds.model_dump_json(), str(creds.model_dump())):
        assert KEY not in text and SECRET not in text


def test_follow_gives_the_testnet_follower_the_profile_percent_and_zero_turns_it_off(
    demo: Cli, tmp_path: Path, fake_testnet: type[FakeTestnetFollower]
) -> None:
    assert demo.run(*ADD, env=ENV)[0] == 0
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    demo.ok("profile", "set", "disaster-stop-pct", "0")
    assert demo.run(*follow_args(tmp_path, file, provider))[0] == 0
    assert fake_testnet.instances[-1].kw == {"disaster_stop_pct": 0.0}
