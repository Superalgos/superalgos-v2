"""TSA settings, the Nautilus-backed follower path, home-directory errors, argument parsing."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

import sa_engine.paper as paper_module
from cli_support import Cli, ThreadedRelay
from sa_cli.home import CliError
from sa_cli.main import build_parser, main
from sa_cli.tsa_config import load_tsa_settings
from sa_core.models import Signal
from sa_core.power import DEFAULT_TIERS, ruleset_event, sign_ruleset
from sa_core.signals import build_signal_event
from sa_core.tsa import DEFAULT_TRUST_DIR, TsaAnchor

# ---------------------------------------------------------------- TSA settings


def test_default_tsa_settings_use_the_bundled_roots_and_three_public_endpoints() -> None:
    settings = load_tsa_settings(None)
    assert len(settings.endpoints) == 3 and settings.k == 2
    assert isinstance(settings.verifiers()["tsa"], TsaAnchor)
    assert settings.stamper().k == 2


def test_tsa_config_file_overrides_endpoints_and_trust(tmp_path: Path) -> None:
    cfg = tmp_path / "tsa.json"
    cfg.write_text(json.dumps({"k": 2, "trust_dir": str(DEFAULT_TRUST_DIR), "endpoints": [
        {"name": "a", "url": "http://127.0.0.1:1/a", "operator": "alpha"},
        {"name": "b", "url": "http://127.0.0.1:1/b", "operator": "beta"},
    ]}))  # fmt: skip
    settings = load_tsa_settings(cfg)
    assert [e.name for e in settings.endpoints] == ["a", "b"]
    one_operator = tmp_path / "one.json"
    one_operator.write_text(
        json.dumps({"endpoints": [{"name": "a", "url": "http://x/a", "operator": "alpha"}]})
    )
    with pytest.raises(CliError, match="cannot set up TSA stamping"):
        load_tsa_settings(one_operator).stamper()


def test_tsa_config_errors_are_short(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="not found"):
        load_tsa_settings(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"unknown": 1}')
    with pytest.raises(CliError, match="invalid"):
        load_tsa_settings(bad)
    bad.write_text(json.dumps({"trust_dir": str(tmp_path)}))
    with pytest.raises(CliError, match="trust bundle"):
        load_tsa_settings(bad)


# ---------------------------------------------------------------- Nautilus-backed follower


class FakePaperReport:
    def __init__(self, filled: int) -> None:
        self.filled = filled

    def model_dump(self, exclude: set[str] | None = None) -> dict[str, Any]:
        return {"orders_filled": self.filled}


class FakeFollowerExecutor:
    """Stands in for sa_engine.paper.FollowerExecutor: no Nautilus node, no network."""

    instances: ClassVar[list[FakeFollowerExecutor]] = []

    def __init__(self, instrument: str, capital: float, **kw: Any) -> None:
        self.instrument, self.capital = instrument, capital
        self.submitted: list[Any] = []
        self.results: list[Any] = []
        self.ran_for: float | None = None
        self.thread = ""
        FakeFollowerExecutor.instances.append(self)

    def submit(self, intent: Any) -> None:
        self.submitted.append(intent)

    def run(self, duration_s: float | None = None) -> FakePaperReport:
        self.ran_for = duration_s
        self.thread = threading.current_thread().name
        time.sleep(min(duration_s or 0.5, 0.5))
        return FakePaperReport(len(self.submitted))


@pytest.fixture
def fake_nautilus(monkeypatch: pytest.MonkeyPatch) -> type[FakeFollowerExecutor]:
    FakeFollowerExecutor.instances = []
    monkeypatch.setattr(paper_module, "FollowerExecutor", FakeFollowerExecutor)
    return FakeFollowerExecutor


def signed(demo: Cli, **kw: Any) -> tuple[str, str]:
    h = demo.home()
    device, delegation = h.load_device(), h.load_delegation(h.load_device())
    base: dict[str, Any] = {
        "strategy_id": "s", "seq": 1, "prev": None, "instrument": "BINANCE:BTCUSDT", "side": "buy", "action": "open",
        "size_fraction": 0.5, "order_type": "market", "timeframe": 3600, "created_ms": int(time.time() * 1000),
    }  # fmt: skip
    return build_signal_event(
        device, Signal.model_validate({**base, **kw}), delegation
    ).model_dump_json(), device.pubkey_hex


def test_signal_file_mode_hands_accepted_signals_to_the_nautilus_paper_node(
    demo: Cli, tmp_path: Path, fake_nautilus: type[FakeFollowerExecutor]
) -> None:
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text)
    out = demo.ok(
        "follow",
        "--signal-file",
        file,
        "--provider",
        provider,
        "--ledger",
        tmp_path / "l.sqlite",
        "--duration",
        "0.2",
    )
    assert "seq 1: accepted" in out and '{"paper": {"orders_filled": 1}}' in out
    node = fake_nautilus.instances[0]
    assert (
        len(node.submitted) == 1 and node.submitted[0].size_fraction == 0.5 and node.ran_for == 0.2
    )


def test_a_rejected_file_does_not_start_a_paper_node(
    demo: Cli, tmp_path: Path, fake_nautilus: type[FakeFollowerExecutor]
) -> None:
    text, provider = signed(demo)
    file = tmp_path / "s.json"
    file.write_text(text.replace("0.5", "0.9"))
    code, out, _ = demo.run(
        "follow", "--signal-file", file, "--provider", provider, "--ledger", tmp_path / "l.sqlite"
    )
    assert code == 1 and "rejected" in out and fake_nautilus.instances[0].ran_for is None


def test_relay_mode_runs_the_bot_in_a_helper_thread_and_nautilus_in_the_main_thread(
    demo: Cli, relay: ThreadedRelay, tmp_path: Path, fake_nautilus: type[FakeFollowerExecutor]
) -> None:
    h = demo.home()
    device, delegation, sa = h.load_device(), h.load_delegation(h.load_device()), h.load_sa()
    relay.inject(
        ruleset_event(
            device, sign_ruleset(sa, device.pubkey_hex, 3600, tiers=DEFAULT_TIERS), delegation
        )
    )
    out = demo.ok(
        "follow",
        "--relay",
        relay.url,
        "--provider",
        device.pubkey_hex,
        "--ledger",
        tmp_path / "l.sqlite",
        "--duration",
        "0.4",
    )
    assert "ready: following 1 provider(s)" in out and '"received": 0' in out
    assert fake_nautilus.instances[0].thread == threading.main_thread().name


def test_signals_reach_a_bot_whose_loop_runs_off_the_main_thread(
    demo: Cli, relay: ThreadedRelay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger is opened on the main thread but written by the bot's helper thread (Nautilus holds the main one):
    every relay signal used to fail with sqlite3.ProgrammingError and `sa follow` reported received 0."""

    class WaitingExecutor(FakeFollowerExecutor):
        def run(self, duration_s: float | None = None) -> FakePaperReport:
            self.thread = threading.current_thread().name
            deadline = time.time() + 30
            while not self.submitted and time.time() < deadline:
                time.sleep(0.02)
            return FakePaperReport(len(self.submitted))

    FakeFollowerExecutor.instances = []
    monkeypatch.setattr(paper_module, "FollowerExecutor", WaitingExecutor)
    h = demo.home()
    device, delegation, sa = h.load_device(), h.load_delegation(h.load_device()), h.load_sa()
    relay.inject(
        ruleset_event(
            device, sign_ruleset(sa, device.pubkey_hex, 3600, tiers=DEFAULT_TIERS), delegation
        )
    )
    ready = tmp_path / "ready"

    def publish_after_ready() -> None:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.02)
        text, _ = signed(demo)
        relay.inject(
            build_signal_event(
                device, Signal.model_validate_json(json.loads(text)["content"]), delegation
            )
        )

    publisher = threading.Thread(target=publish_after_ready)
    publisher.start()
    out = demo.ok("follow", "--relay", relay.url, "--provider", device.pubkey_hex, "--ledger", tmp_path / "l.sqlite",
                  "--ready-file", ready, "--duration", "30", "--reorder-wait", "0")  # fmt: skip
    publisher.join()
    assert (
        '"received": 1' in out and '"accepted": 1' in out and '"paper": {"orders_filled": 1}' in out
    )
    assert len(FakeFollowerExecutor.instances[0].submitted) == 1


def test_a_bot_failure_in_the_helper_thread_is_reported(
    demo: Cli, relay: ThreadedRelay, tmp_path: Path, fake_nautilus: type[FakeFollowerExecutor]
) -> None:
    err = demo.fail(
        "follow",
        "--relay",
        relay.url,
        "--provider",
        "ab" * 32,
        "--ledger",
        tmp_path / "l.sqlite",
        "--duration",
        "0.3",
    )
    assert "has no rule set" in err


# ---------------------------------------------------------------- home and parser


def test_home_files_that_are_damaged_are_explained(cli: Cli) -> None:
    home = cli.home()
    with pytest.raises(CliError, match="no identity"):
        home.load_sa()
    cli.ok("keys", "init")
    home.identity_path.write_text("nope")
    with pytest.raises(CliError, match="damaged"):
        home.identity()
    home.allocation_path.write_text("nope")
    with pytest.raises(CliError, match="not an allocation"):
        home.last_allocation()
    sa_file = home.sa_path
    sa_file.write_text("{}")
    with pytest.raises(CliError, match="not a Superalgos key file"):
        home.load_sa()
    home.device_path.write_text("{}")
    with pytest.raises(CliError, match="not a Superalgos key file"):
        home.load_device()


def test_the_default_home_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SA_HOME", str(tmp_path / "envhome"))
    monkeypatch.setenv("SA_PASSPHRASE", "env-passphrase-ok")
    monkeypatch.setenv("SA_SCRYPT_N", "1024")
    assert main(["keys", "init"]) == 0
    assert (tmp_path / "envhome" / "device.key.json").exists()
    assert main(["--home", str(tmp_path / "explicit"), "keys", "init"]) == 0
    assert (tmp_path / "explicit" / "device.key.json").exists()
    capsys.readouterr()


def test_the_parser_requires_a_command_and_lists_them(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([])
    assert exc.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


def test_a_loopback_tsa_config_may_set_a_short_gap_but_a_public_one_may_not(tmp_path: Path) -> None:
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"min_gap_s": 1.0, "trust_dir": str(DEFAULT_TRUST_DIR), "endpoints": [
        {"name": "a", "url": "http://127.0.0.1:1/a", "operator": "alpha", "min_gap_s": 1.0},
        {"name": "b", "url": "http://localhost:1/b", "operator": "beta", "min_gap_s": 1.0},
    ]}))  # fmt: skip
    assert load_tsa_settings(local).stamper().min_gap_s == 1.0
    public = tmp_path / "public.json"
    public.write_text(json.dumps({"min_gap_s": 1.0}))
    with pytest.raises(CliError, match="only loopback TSAs"):
        load_tsa_settings(public).stamper()


def test_lab_config_allows_the_short_gap_on_a_container_host(tmp_path: Path) -> None:
    cfg = tmp_path / "lab.json"
    cfg.write_text(json.dumps({"min_gap_s": 1.0, "lab": True, "trust_dir": str(DEFAULT_TRUST_DIR), "endpoints": [
        {"name": "a", "url": "http://tsa:8080/a", "operator": "alpha", "min_gap_s": 1.0},
        {"name": "b", "url": "http://tsa:8080/b", "operator": "beta", "min_gap_s": 1.0},
    ]}))  # fmt: skip
    assert load_tsa_settings(cfg).stamper().min_gap_s == 1.0
    cfg.write_text(cfg.read_text().replace('"lab": true', '"lab": false'))
    with pytest.raises(CliError, match="only loopback TSAs"):
        load_tsa_settings(cfg).stamper()
