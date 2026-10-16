"""`sa demo`: the plan (dates, files, command lines), the process set, and the orchestration with the heavy parts stubbed.

The real thing, across real processes, is `make demo-smoke`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import sa_cli.demo_plan as plan
from cli_support import ThreadedRelay
from sa_cli import cmd_demo
from sa_cli.home import CliError, Home
from sa_cli.main import build_parser, main
from sa_core.models import ALLOCATION_KIND, PROPOSAL_KIND, RULESET_KIND, VOTE_KIND
from sa_engine.network.replay import ReplaySpec

# ---------------------------------------------------------------- plan


def test_default_window_is_the_last_days_of_the_latest_complete_month() -> None:
    assert plan.latest_complete_month(date(2026, 10, 13)) == date(2026, 9, 1)
    assert plan.latest_complete_month(date(2026, 1, 2)) == date(2025, 12, 1)
    w = plan.pick_window(date(2026, 10, 13), 2)
    assert (w.replay_start, w.replay_end) == ("2026-09-29", "2026-10-01")
    assert (w.backtest_start, w.backtest_end) == (
        "2026-09-01",
        "2026-09-15",
    )  # two weeks, before the replay


def test_window_with_an_explicit_month_and_a_long_replay() -> None:
    w = plan.pick_window(date(2026, 10, 13), 20, "2026-02-17")  # any day of the month names it
    assert (w.replay_start, w.replay_end) == ("2026-02-09", "2026-03-01")
    assert (w.backtest_start, w.backtest_end) == (
        "2026-02-01",
        "2026-02-09",
    )  # never overlaps the replay


def test_window_errors_say_what_to_change() -> None:
    with pytest.raises(CliError, match="--days 0 must be at least 1"):
        plan.pick_window(date(2026, 10, 13), 0)
    with pytest.raises(CliError, match="--month 'soon'"):
        plan.pick_window(date(2026, 10, 13), 2, "soon")
    with pytest.raises(CliError, match="use at most 25"):
        plan.pick_window(date(2026, 10, 13), 29, "2026-02-01")


def test_the_repo_is_found_from_the_package_and_not_from_nowhere(tmp_path: Path) -> None:
    root = plan.find_repo_root()
    assert (root / "relay" / "go.mod").is_file() and (root / "web" / "package.json").is_file()
    with pytest.raises(CliError, match="superalgos-v2 checkout"):
        plan.find_repo_root(tmp_path / "a" / "b")


def test_demo_strategies_exist_and_validate() -> None:
    from sa_engine.schema import validate_strategy

    for name in (plan.ATLAS_STRATEGY, plan.FOX_STRATEGY):
        assert validate_strategy(plan.strategy_path(name).read_text()).name == name


def test_missing_demo_strategy_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(plan, "__file__", str(tmp_path / "demo_plan.py"))
    with pytest.raises(CliError, match="demo strategy nope is missing"):
        plan.strategy_path("nope")


def test_relay_config_is_unthrottled_with_fixture_balances() -> None:
    toml = plan.relay_config(
        port=1, admin_port=2, storage=Path("/s"), balances=Path("/b.json"), time_scale=0.5
    )
    for needle in (
        'listen = "127.0.0.1:1"',
        'allocation_delay = "0s"',
        "time_scale = 0.5",
        'kind = "fixture"',
        'file = "/b.json"',
    ):
        assert needle in toml


def test_api_config_lists_every_source_and_changes_with_the_mode() -> None:
    pubkeys = {plan.ATLAS: "a" * 64, plan.FOX: "f" * 64}
    kw: dict[str, Any] = {
        "relay_url": "ws://r", "work": Path("/w"), "web_dist": Path("/d"), "tsa_config": Path("/t.json"), "speed": 100.0,
        "pubkeys": pubkeys, "live": False, "data_dir": Path("/data"),
    }  # fmt: skip
    cfg = plan.api_config(**kw)
    assert cfg["time_scale"] == 0.01 and cfg["governance_anchor"] == "created-at"
    assert set(cfg["replay_specs"]) == set(pubkeys.values())  # type: ignore[call-overload]
    assert cfg["tsa_config"] == "/t.json" and cfg["web_dist"] == "/d" and cfg["me_home"] == "/w/mid"
    assert [f["home"] for f in cfg["followers"]] == ["/w/whale", "/w/mid", "/w/zero"]  # type: ignore[attr-defined]
    assert cfg["dev_views"] is True  # the demo turns the dev views on
    live = plan.api_config(**{**kw, "live": True, "tsa_config": None, "web_dist": None})
    assert live["time_scale"] == 1.0 and live["replay_specs"] == {}
    assert "tsa_config" not in live and "web_dist" not in live


def test_provider_and_follower_command_lines() -> None:
    w = plan.Window("2026-09-01", "2026-09-15", "2026-09-29", "2026-10-01")
    kw: dict[str, Any] = {
        "home": Path("/h"), "strategy": Path("/s.json"), "relay_url": "ws://r", "state": Path("/st"), "window": w, "speed": 1440.0,
        "wall0_s": 1.5, "tsa_config": Path("/t.json"), "data_dir": Path("/d"), "bundle": Path("/b.json"), "balances": Path("/bal.json"),
        "reveal": True, "live": False,
    }  # fmt: skip
    replay = plan.provider_command(**kw)
    assert replay[replay.index("--replay") + 1 :][:2] == ["2026-09-29", "2026-10-01"]
    assert "--reveal" in replay and "--start-at" in replay and "--paper" not in replay
    assert replay[replay.index("--tsa-config") + 1] == "/t.json"
    live = plan.provider_command(**{**kw, "live": True, "reveal": False, "tsa_config": None})
    assert (
        "--paper" in live
        and "--replay" not in live
        and "--reveal" not in live
        and "--tsa-config" not in live
    )
    f = plan.follower_command(home=Path("/h"), relay_url="ws://r", providers=["a", "b"], spec=Path("/spec"), tsa_config=None,
                              data_dir=Path("/d"), ready_file=Path("/ready"))  # fmt: skip
    assert (
        f.count("--provider") == 2
        and f[f.index("--replay-spec") + 1] == "/spec"
        and "--executor" not in f
    )
    n = plan.follower_command(home=Path("/h"), relay_url="ws://r", providers=["a"], spec=None, tsa_config=Path("/t"),
                              data_dir=Path("/d"), ready_file=Path("/ready"))  # fmt: skip
    assert n[n.index("--executor") + 1] == "nautilus" and "--tsa-config" in n


# ---------------------------------------------------------------- process set


def py(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def test_process_set_tracks_failures_and_stops_everything(tmp_path: Path) -> None:
    procs = cmd_demo.ProcessSet(tmp_path / "logs")
    ok = procs.start("ok", py("print('hello')"))
    bad = procs.start("bad", py("import sys; print('boom'); sys.exit(3)"))
    sleeper = procs.start("sleeper", py("import time; time.sleep(60)"))
    stubborn = procs.start(
        "stubborn",
        py(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        ),
    )
    deadline = time.time() + 10
    while time.time() < deadline and "ready" not in stubborn.tail():
        time.sleep(0.05)  # the handler must be installed before SIGTERM arrives
    ok.popen.wait(10)
    bad.popen.wait(10)
    assert procs.failed() == [bad] and procs.failed() == []  # reported once
    assert "boom" in bad.tail() and procs.alive("sleeper") and not procs.alive("ok")
    procs.stop_all(timeout=1.0)
    assert all(c.popen.poll() is not None for c in procs.children)
    assert stubborn.popen.returncode == -signal.SIGKILL  # ignored SIGTERM, so it was killed
    assert sleeper.popen.returncode == -signal.SIGTERM
    assert (tmp_path / "logs" / "ok.log").read_text().strip() == "hello"
    assert cmd_demo.Child("x", ok.popen, tmp_path / "missing.log").tail() == "(no log)"


def test_wait_until_and_port_helpers() -> None:
    cmd_demo.wait_until(lambda: True, 1, "nothing")
    with pytest.raises(CliError, match="timed out after 0 s waiting for a miracle"):
        cmd_demo.wait_until(lambda: False, 0.1, "a miracle")
    port = cmd_demo.free_port()
    assert not cmd_demo.port_open(port)
    assert not cmd_demo.http_ok(f"http://127.0.0.1:{port}/x")


# ---------------------------------------------------------------- orchestration


def make_args(tmp_path: Path, **kw: Any) -> argparse.Namespace:
    ns = build_parser().parse_args(
        [
            "demo",
            "--workdir",
            str(tmp_path / "demo"),
            "--data-dir",
            str(tmp_path / "data"),
            "--lead",
            "1",
        ]
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def make_demo(tmp_path: Path, **kw: Any) -> cmd_demo.Demo:
    return cmd_demo.Demo(make_args(tmp_path, **kw), plan.find_repo_root())


def test_demo_arguments_are_checked(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="--speed 0 must be within"):
        make_demo(tmp_path, speed=0.0)
    with pytest.raises(CliError, match="--speed 99999 must be within"):
        make_demo(tmp_path, speed=99999.0)
    args = build_parser().parse_args(["demo"])
    assert (args.port, args.speed, args.days, args.live, args.public_tsa) == (
        8787,
        1440.0,
        2,
        False,
        False,
    )


def test_the_work_directory_is_wiped_only_if_it_is_a_demo_directory(tmp_path: Path) -> None:
    d = make_demo(tmp_path)
    d.work.mkdir()
    (d.work / "precious.txt").write_text("mine")
    with pytest.raises(CliError, match="not a demo directory"):
        d.prepare_directory()
    assert (d.work / "precious.txt").exists()
    (d.work / "precious.txt").unlink()
    d.work.rmdir()
    d.prepare_directory()
    (d.work / "old.txt").write_text("stale")
    d.prepare_directory()  # a marked directory is reset
    assert not (d.work / "old.txt").exists() and (d.work / plan.DEMO_MARKER).is_file()
    assert json.loads((d.work / "balances.json").read_text()) == {}
    assert (d.work / "bundles").is_dir() and d.data_dir.is_dir()


def test_identities_allocations_and_governance_reach_the_relay(
    tmp_path: Path, relay: ThreadedRelay
) -> None:
    d = make_demo(tmp_path)
    d.prepare_directory()
    d.relay_url = relay.url
    d.create_identities()
    d.governance()
    assert set(d.pubkeys) == {"atlas", "fox", "whale", "mid", "zero"}
    assert len(set(d.pubkeys.values())) == 5
    assert len(relay.events(RULESET_KIND)) == 2 and len(relay.events(ALLOCATION_KIND)) == 2
    assert len(relay.events(PROPOSAL_KIND)) == 1 and len(relay.events(VOTE_KIND)) == 2
    assert d.proposal_id == relay.events(PROPOSAL_KIND)[0].id
    balances = json.loads((d.work / "balances.json").read_text())
    assert sorted(float(v) for v in balances.values()) == [
        20_000.0,
        1_000_000.0,
    ]  # zero-power zero is simply absent
    rules = [json.loads(e.content) for e in relay.events(RULESET_KIND)]
    private = [r for r in rules if r.get("private")]
    assert len(private) == 1 and private[0]["private"]["min_power"] == plan.PRIVATE_MIN_POWER
    assert (
        len([r for r in rules if r.get("tiers")]) == 1
    )  # the public provider's default four tiers
    # the identity files hold no private key in the clear
    for home in ("atlas", "fox", "whale", "mid", "zero"):
        text = "".join(p.read_text() for p in (d.work / home).glob("*.json"))
        assert "BEGIN" not in text and plan.PASSPHRASE not in text


def test_a_failing_sa_command_names_the_command_and_the_reason(tmp_path: Path) -> None:
    d = make_demo(tmp_path)
    d.prepare_directory()
    with pytest.raises(
        CliError, match=r"`sa identity --demo` failed in demo home atlas: .*no device key"
    ):
        d.sa("atlas", "identity", "--demo")
    out = {"atlas": 1}
    (d.work / "atlas").mkdir()
    (d.work / "atlas" / "identity.json").write_text(json.dumps({"device_pubkey": "ab" * 32}))
    assert d.pubkey("atlas") == "ab" * 32 and out


def test_build_relay_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = make_demo(tmp_path, relay_bin=tmp_path / "nope")
    d.prepare_directory()
    with pytest.raises(CliError, match="--relay-bin .* does not exist"):
        d.build_relay()
    binary = tmp_path / "sarelay"
    binary.write_text("x")
    d.args.relay_bin = binary
    assert d.build_relay() == binary
    d.args.relay_bin = None
    monkeypatch.setattr("sa_cli.cmd_demo.shutil.which", lambda _: None)
    with pytest.raises(CliError, match="Go toolchain"):
        d.build_relay()
    monkeypatch.setattr("sa_cli.cmd_demo.shutil.which", lambda _: "/usr/bin/go")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "undefined: foo")

    monkeypatch.setattr("sa_cli.cmd_demo.subprocess.run", fake_run)
    with pytest.raises(CliError, match="go build of the relay failed:\nundefined: foo"):
        d.build_relay()
    monkeypatch.setattr(
        "sa_cli.cmd_demo.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    assert d.build_relay() == d.work / "sarelay"
    assert calls[0][:2] == ["/usr/bin/go", "build"]


def test_build_web_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "web" / "dist").mkdir(parents=True)
    d = cmd_demo.Demo(make_args(tmp_path), repo)
    d.args.no_web_build = True
    assert d.build_web() is None  # no dist yet and no build wanted: API only
    d.args.no_web_build = False
    (repo / "web" / "dist" / "index.html").write_text("<html>")
    assert d.build_web() == repo / "web" / "dist"
    d.args.rebuild_web = True
    d.args.no_web_build = True
    assert d.build_web() is None
    d.args.no_web_build = False
    monkeypatch.setattr("sa_cli.cmd_demo.shutil.which", lambda _: None)
    with pytest.raises(CliError, match="bun is needed"):
        d.build_web()
    monkeypatch.setattr("sa_cli.cmd_demo.shutil.which", lambda _: "/usr/bin/bun")
    ran: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("sa_cli.cmd_demo.subprocess.run", fake_run)
    assert d.build_web() == repo / "web" / "dist"
    assert [c[1:] for c in ran] == [["install", "--frozen-lockfile"], ["run", "build"]]
    monkeypatch.setattr(
        "sa_cli.cmd_demo.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "out", "tsc: error"),
    )
    with pytest.raises(CliError, match="failed in web/"):
        d.build_web()


def _stale_setup(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    web = repo / "web"
    (web / "dist").mkdir(parents=True)
    (web / "src" / "lib").mkdir(parents=True)
    for f in (
        web / "dist" / "index.html",
        web / "index.html",
        web / "bun.lock",
        web / "src" / "lib" / "a.ts",
    ):
        f.write_text("x")
        os.utime(f, (1_000, 1_000))
    return repo, web


@pytest.mark.parametrize("touched", ["src/lib/a.ts", "index.html", "bun.lock"])
def test_build_web_rebuilds_when_sources_are_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, touched: str
) -> None:
    repo, web = _stale_setup(tmp_path)
    d = cmd_demo.Demo(make_args(tmp_path), repo)
    assert d.build_web() == web / "dist"  # fresh: no build
    os.utime(web / touched, (2_000, 2_000))
    monkeypatch.setattr("sa_cli.cmd_demo.shutil.which", lambda _: "/usr/bin/bun")
    ran: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("sa_cli.cmd_demo.subprocess.run", fake_run)
    assert d.build_web() == web / "dist"
    assert [c[1:] for c in ran] == [["install", "--frozen-lockfile"], ["run", "build"]]


def test_build_web_stale_with_no_web_build_serves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, web = _stale_setup(tmp_path)
    os.utime(web / "src" / "lib" / "a.ts", (2_000, 2_000))
    d = cmd_demo.Demo(make_args(tmp_path), repo)
    d.args.no_web_build = True

    def boom(*a: Any, **kw: Any) -> None:
        raise AssertionError("must not build")

    monkeypatch.setattr("sa_cli.cmd_demo.subprocess.run", boom)
    assert d.build_web() == web / "dist"


FAKE_RELAY = """\
#!{python}
import re, socket, sys, time
cfg = open(sys.argv[2]).read()
port = int(re.search(r'listen = "127.0.0.1:(\\d+)"', cfg).group(1))
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen()
time.sleep(60)
"""


def test_start_relay_waits_for_the_port_and_reports_an_early_exit(tmp_path: Path) -> None:
    d = make_demo(tmp_path)
    d.prepare_directory()
    fake = tmp_path / "fake-sarelay"
    fake.write_text(FAKE_RELAY.format(python=sys.executable))
    fake.chmod(0o755)
    try:
        d.start_relay(fake)
        assert d.relay_url.startswith("ws://127.0.0.1:") and cmd_demo.port_open(
            int(d.relay_url.rsplit(":", 1)[1])
        )
        assert "time_scale = 0.000694" in (d.work / "sarelay.toml").read_text()
    finally:
        d.procs.stop_all()
    quitter = tmp_path / "quit-sarelay"
    quitter.write_text(f"#!{sys.executable}\nprint('bad config')\nraise SystemExit(2)\n")
    quitter.chmod(0o755)
    d2 = make_demo(tmp_path / "second", live=True)
    d2.prepare_directory()
    with pytest.raises(CliError, match="the relay exited at start-up:\nbad config"):
        d2.start_relay(quitter)


def stub_start(monkeypatch: pytest.MonkeyPatch, d: cmd_demo.Demo) -> list[tuple[str, list[str]]]:
    """Replace process start by a recorder; followers 'become ready' by writing their ready file."""
    seen: list[tuple[str, list[str]]] = []

    def start(name: str, cmd: list[str], **kw: Any) -> cmd_demo.Child:
        seen.append((name, cmd))
        if "--ready-file" in cmd:
            Path(cmd[cmd.index("--ready-file") + 1]).write_text("ready\n")
        popen = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        child = cmd_demo.Child(
            name, popen, d.work / "logs" / f"{name}.log", kw.get("essential", False)
        )
        d.procs.children.append(child)
        return child

    monkeypatch.setattr(d.procs, "start", start)
    return seen


def test_providers_and_followers_get_one_clock_and_the_right_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_demo(tmp_path)
    d.prepare_directory()
    d.pubkeys = {n: n[0] * 64 for n in ("atlas", "fox", "whale", "mid", "zero")}
    d.relay_url = "ws://r"
    seen = stub_start(monkeypatch, d)
    try:
        wall0 = d.start_providers()
        spec = json.loads((d.work / "replay.json").read_text())
        assert spec["wall0_ms"] == int(wall0 * 1000) and spec["speed"] == 1440.0
        names = [n for n, _ in seen]
        assert names == ["provider-atlas", "provider-fox"]
        atlas, fox = (c for _, c in seen)
        assert (
            "--reveal" not in atlas and "--reveal" in fox
        )  # only the private provider reveals in its bundle
        assert (
            atlas[atlas.index("--start-at") + 1] == fox[fox.index("--start-at") + 1]
        )  # one clock for both
        seen.clear()
        d.start_followers()
        subs = {n: [c[i + 1] for i, x in enumerate(c) if x == "--provider"] for n, c in seen}
        assert subs["follower-whale"] == [
            "a" * 64,
            "f" * 64,
        ]  # whale is entitled to the private provider
        assert subs["follower-mid"] == subs["follower-zero"] == ["a" * 64]
        d.procs.stop_all()
        seen.clear()
        d.live = True
        d.start_providers()
        assert [n for n, _ in seen] == ["provider-atlas"] and "--paper" in seen[0][
            1
        ]  # the private one is idle in live mode
        seen.clear()
        d.start_followers()
        assert all("--executor" in c and "--replay-spec" not in c for _, c in seen)
    finally:
        d.procs.stop_all()


def test_a_follower_that_dies_before_it_is_ready_is_reported_with_its_log(tmp_path: Path) -> None:
    d = make_demo(tmp_path)
    d.prepare_directory()
    d.pubkeys = {n: n[0] * 64 for n in ("atlas", "fox", "whale", "mid", "zero")}
    d.relay_url = "ws://r"
    d.procs.start("follower-whale", py("print('cannot subscribe'); raise SystemExit(1)"))
    d.procs.start("follower-mid", py("import time; time.sleep(30)"))
    d.procs.start("follower-zero", py("import time; time.sleep(30)"))
    time.sleep(0.5)
    ready = d.work / "whale.ready"
    assert (
        d.follower_settled("whale", ready)() is True
    )  # dead counts as settled, ready is checked after
    assert not ready.exists() and "cannot subscribe" in d.child("follower-whale").tail()
    d.procs.stop_all()


def test_backtests_are_started_for_both_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_demo(tmp_path)
    d.prepare_directory()
    seen = stub_start(monkeypatch, d)
    try:
        d.start_backtests()
    finally:
        d.procs.stop_all()
    assert [n for n, _ in seen] == ["backtest-atlas-ema-1h", "backtest-fox-ema-1h"]
    cmd = seen[0][1]
    assert (
        cmd[cmd.index("--start") + 1] == d.window.backtest_start and "-o" in cmd and "--json" in cmd
    )


def test_the_api_is_started_with_a_config_that_names_every_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_demo(tmp_path, port=cmd_demo.free_port())
    d.prepare_directory()
    d.pubkeys = {n: n[0] * 64 for n in ("atlas", "fox", "whale", "mid", "zero")}
    d.relay_url = "ws://r"
    seen = stub_start(monkeypatch, d)
    monkeypatch.setattr(cmd_demo, "http_ok", lambda url: True)
    try:
        url = d.start_api(tmp_path / "dist")
    finally:
        d.procs.stop_all()
    assert url == f"http://127.0.0.1:{d.args.port}"
    cfg = json.loads((d.work / "api-config.json").read_text())
    assert cfg["relays"] == ["ws://r"] and cfg["web_dist"] == str(tmp_path / "dist")
    assert seen[0][0] == "api" and seen[0][1][-2:] == ["--port", str(d.args.port)]


def test_the_api_port_must_be_free(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        d = make_demo(tmp_path, port=s.getsockname()[1])
        d.prepare_directory()
        d.pubkeys = {n: n[0] * 64 for n in ("atlas", "fox", "whale", "mid", "zero")}
        with pytest.raises(CliError, match="already in use; pick another with --port"):
            d.start_api(None)


def test_an_api_that_exits_at_start_is_reported_with_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_demo(tmp_path, port=cmd_demo.free_port())
    d.prepare_directory()
    d.pubkeys = {n: n[0] * 64 for n in ("atlas", "fox", "whale", "mid", "zero")}
    d.relay_url = "ws://r"
    real_start = d.procs.start
    monkeypatch.setattr(
        d.procs,
        "start",
        lambda name, cmd, **kw: real_start(
            name, py("print('bad config'); raise SystemExit(2)"), **kw
        ),
    )
    with pytest.raises(CliError, match="the API exited at start-up:\nbad config"):
        d.start_api(None)


def stub_everything(monkeypatch: pytest.MonkeyPatch, d: cmd_demo.Demo, relay_url: str) -> list[str]:
    order: list[str] = []

    def step(name: str, result: Any = None) -> Any:
        def run(*_: object) -> Any:
            order.append(name)
            return result() if callable(result) else result

        return run

    def start_relay(_: Path) -> None:
        order.append("start_relay")
        d.relay_url = relay_url

    monkeypatch.setattr(d, "build_relay", step("build_relay", Path("/x")))
    monkeypatch.setattr(d, "build_web", step("build_web"))
    monkeypatch.setattr(d, "start_relay", start_relay)
    monkeypatch.setattr(d, "create_identities", step("identities"))
    monkeypatch.setattr(d, "governance", step("governance"))
    monkeypatch.setattr(d, "warm_data", step("warm_data"))
    monkeypatch.setattr(d, "start_backtests", step("backtests"))
    monkeypatch.setattr(d, "start_providers", step("providers", lambda: time.time() + 5))
    monkeypatch.setattr(d, "start_followers", step("followers"))
    monkeypatch.setattr(d, "start_api", step("api", "http://127.0.0.1:1"))
    return order


def test_run_does_the_steps_in_order_prints_the_url_and_shuts_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = make_demo(tmp_path, duration=0.6)
    order = stub_everything(monkeypatch, d, "ws://r")
    old = signal.getsignal(signal.SIGINT)
    assert d.run() == 0
    assert order == [
        "build_relay",
        "build_web",
        "start_relay",
        "identities",
        "governance",
        "warm_data",
        "backtests",
        "providers",
        "followers",
        "api",
    ]
    out = capsys.readouterr().out
    assert (
        "READY http://127.0.0.1:1" in out
        and "dashboard   http://127.0.0.1:1" in out
        and "local throwaway TSAs" in out
    )
    assert "stopped; everything the demo started is gone" in out
    assert signal.getsignal(signal.SIGINT) is old  # handlers are put back
    assert d.tsa_config is not None and d.tsa_config.name == "tsa-config.json"


def test_ctrl_c_stops_cleanly_and_a_dead_essential_child_ends_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = make_demo(tmp_path, public_tsa=True, live=True)
    stub_everything(monkeypatch, d, "ws://r")
    monkeypatch.setattr(d, "wait", lambda: (_ for _ in ()).throw(KeyboardInterrupt("SIGINT")))
    assert d.run() == 0
    out = capsys.readouterr().out
    assert "stopping ..." in out and "public time-stamp authorities" in out and d.tsa_config is None
    d2 = make_demo(tmp_path / "two", duration=5)
    stub_everything(monkeypatch, d2, "ws://r")
    monkeypatch.setattr(
        d2,
        "announce",
        lambda *a: d2.procs.start(
            "relay", py("print('crashed'); raise SystemExit(7)"), essential=True
        ),
    )
    assert d2.run() == 1
    assert "relay exited with code 7:\ncrashed" in capsys.readouterr().out


def test_sigterm_is_handled_like_ctrl_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = make_demo(tmp_path)
    stub_everything(monkeypatch, d, "ws://r")
    monkeypatch.setattr(d, "announce", lambda *a: os.kill(os.getpid(), signal.SIGTERM))

    def wait() -> int:
        time.sleep(5)
        return 0

    monkeypatch.setattr(d, "wait", wait)
    assert d.run() == 0
    assert "stopping ..." in capsys.readouterr().out


def test_setup_errors_still_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = make_demo(tmp_path)
    stub_everything(monkeypatch, d, "ws://r")
    monkeypatch.setattr(d, "governance", lambda: (_ for _ in ()).throw(CliError("no proposal")))
    with pytest.raises(CliError, match="no proposal"):
        d.run()
    assert "stopped; everything the demo started is gone" in capsys.readouterr().out


def test_the_demo_command_is_wired_into_sa(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ran: list[Any] = []

    def run(self: cmd_demo.Demo) -> int:
        ran.append(self)
        return 0

    monkeypatch.setattr(cmd_demo.Demo, "run", run)
    assert (
        main(
            ["demo", "--workdir", str(tmp_path / "w"), "--days", "1"], home=Home(tmp_path / "home")
        )
        == 0
    )
    assert ran and ran[0].window.replay_end.endswith("-01")


def test_announce_mentions_live_and_replay_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = make_demo(tmp_path)
    d.relay_url = "ws://r"
    d.announce("http://x", time.time() + 10, 3.0)
    assert "replay of" in capsys.readouterr().out
    d.live = True
    d.announce("http://x", time.time() - 5, 3.0)
    out = capsys.readouterr().out
    assert "LIVE paper trading" in out and "fox is idle in live mode" in out


# ---------------------------------------------------------------- container roles


def role_args(tmp_path: Path, role: str, **kw: Any) -> argparse.Namespace:
    argv = [
        "demo",
        "--role",
        role,
        "--workdir",
        str(tmp_path / "work"),
        "--data-dir",
        str(tmp_path / "data"),
        "--lead",
        "30",
    ]
    ns = build_parser().parse_args(argv)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def exec_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "sa_cli.cmd_demo.os.execvpe", lambda file, argv, env: calls.append(list(argv))
    )
    return calls


def setup_role(
    tmp_path: Path, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> cmd_demo.Demo:
    """Run the setup role against the fake relay; backtests are stubbed (they need market data)."""

    def fake_run(cmd: list[str], **k: Any) -> subprocess.CompletedProcess[str]:
        Path(cmd[cmd.index("-o") + 1]).write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("sa_cli.cmd_demo.subprocess.run", fake_run)
    d = cmd_demo.Demo(
        role_args(
            tmp_path,
            "setup",
            relay_url=relay.url,
            public_tsa=True,
            web_dist=tmp_path / "dist",
            **kw,
        ),
        None,
    )
    assert d.role_setup() == 0
    return d


def test_the_setup_role_prepares_everything_the_other_containers_read(
    tmp_path: Path,
    relay: ThreadedRelay,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    d = setup_role(tmp_path, relay, monkeypatch)
    work = d.work
    assert (
        (work / "replay.json").is_file()
        and (work / "api-config.json").is_file()
        and (work / plan.DEMO_MARKER).is_file()
    )
    assert len(relay.events(RULESET_KIND)) == 2 and len(relay.events(VOTE_KIND)) == 2
    spec = ReplaySpec.load(work / "replay.json")
    assert (
        spec.wall0_ms / 1000 - time.time() > 20
    )  # the lead gives the other containers time to start
    api = json.loads((work / "api-config.json").read_text())
    assert api["web_dist"] == str(tmp_path / "dist") and api["relays"] == [relay.url]
    assert "setup done" in capsys.readouterr().out
    d.relay_url = relay.url


def test_a_failed_backtest_stops_the_setup(
    tmp_path: Path, relay: ThreadedRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sa_cli.cmd_demo.subprocess.run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "no data"),
    )
    d = cmd_demo.Demo(role_args(tmp_path, "setup", relay_url=relay.url, public_tsa=True), None)
    with pytest.raises(CliError, match="backtest of atlas-ema-1h failed:\nno data"):
        d.role_setup()


def test_provider_and_follower_roles_exec_the_same_commands_as_the_local_demo(
    tmp_path: Path,
    relay: ThreadedRelay,
    monkeypatch: pytest.MonkeyPatch,
    exec_calls: list[list[str]],
) -> None:
    setup_role(tmp_path, relay, monkeypatch)
    spec = ReplaySpec.load(tmp_path / "work" / "replay.json")
    a = cmd_demo.Demo(role_args(tmp_path, "provider", name="fox", relay_url=relay.url), None)
    a.role_provider()
    fox = exec_calls[-1]
    assert fox[fox.index("--start-at") + 1] == str(spec.wall0_ms / 1000) and "--reveal" in fox
    assert fox[fox.index("--tsa-config") + 1] == str(tmp_path / "work" / "tsa" / "tsa-config.json")
    assert fox[fox.index("--replay") + 1 :][:2] == [spec.start, spec.end]
    f = cmd_demo.Demo(role_args(tmp_path, "follower", name="whale", relay_url=relay.url), None)
    f.role_follower()
    whale = exec_calls[-1]
    assert whale.count("--provider") == 2 and "--replay-spec" in whale
    with pytest.raises(CliError, match="--name 'whale' is not a provider"):
        cmd_demo.Demo(
            role_args(tmp_path, "provider", name="whale", relay_url=relay.url), None
        ).role_provider()
    with pytest.raises(CliError, match="--name 'atlas' is not a follower"):
        cmd_demo.Demo(
            role_args(tmp_path, "follower", name="atlas", relay_url=relay.url), None
        ).role_follower()
    with pytest.raises(CliError, match="--relay-url ws://HOST:PORT is required"):
        cmd_demo.Demo(role_args(tmp_path, "follower", name="zero"), None).role_follower()


def test_the_api_role_binds_the_host_it_is_given_and_needs_the_setup_first(
    tmp_path: Path,
    relay: ThreadedRelay,
    monkeypatch: pytest.MonkeyPatch,
    exec_calls: list[list[str]],
) -> None:
    d = cmd_demo.Demo(role_args(tmp_path, "api", api_host="0.0.0.0"), None)  # noqa: S104
    with pytest.raises(CliError, match="api-config.json is missing"):
        d.role_api()
    setup_role(tmp_path, relay, monkeypatch)
    d.role_api()
    cmd = exec_calls[-1]
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0" and cmd[cmd.index("--port") + 1] == "8787"  # noqa: S104
    assert (
        build_parser().parse_args(["demo"]).api_host == "127.0.0.1"
    )  # local only unless a container says otherwise


def test_the_tsa_role_serves_a_lab_farm_other_containers_can_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = cmd_demo.Demo(role_args(tmp_path, "tsa", tsa_port=cmd_demo.free_port()), None)
    seen: dict[str, Any] = {}

    def block() -> None:
        cfg = json.loads((d.work / "tsa" / "tsa-config.json").read_text())
        seen.update(cfg)

    monkeypatch.setattr(d, "block", block)
    assert d.role_tsa() == 0
    assert seen["lab"] is True and seen["min_gap_s"] == 1.0
    assert all(
        e["url"].startswith("http://tsa:") for e in seen["endpoints"]
    )  # the name the compose network resolves


def test_block_returns_on_sigterm(tmp_path: Path) -> None:
    import threading

    d = cmd_demo.Demo(role_args(tmp_path, "tsa"), None)
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        d.block()
    finally:
        timer.cancel()
        for s, h in old.items():
            signal.signal(s, h)


def test_roles_need_a_workdir_and_reject_live(tmp_path: Path) -> None:
    ns = build_parser().parse_args(["demo", "--role", "api"])
    with pytest.raises(CliError, match="--workdir DIR is required"):
        cmd_demo.Demo(ns, None)
    live = role_args(tmp_path, "provider", live=True)
    with pytest.raises(CliError, match="--live runs on one machine only"):
        cmd_demo.demo(live, Home(tmp_path / "h"))


def test_role_dispatch_calls_the_right_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def recorder(name: str) -> Any:
        def role(self: cmd_demo.Demo) -> int:
            called.append(name)
            return 0

        return role

    for name in ("role_tsa", "role_setup", "role_provider", "role_follower", "role_api"):
        monkeypatch.setattr(cmd_demo.Demo, name, recorder(name))
    for role in ("tsa", "setup", "provider", "follower", "api"):
        assert cmd_demo.demo(role_args(tmp_path, role), Home(tmp_path / "h")) == 0
    assert called == ["role_tsa", "role_setup", "role_provider", "role_follower", "role_api"]


def test_warm_data_prepares_the_month_once_and_explains_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sa_engine import catalog
    from sa_engine.data import DataError

    d = make_demo(tmp_path, month="2026-09-01")
    asked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(catalog, "ensure_catalog", lambda *a, **k: asked.append(a))
    d.warm_data()
    symbol, first, end, data_dir = asked[0]
    assert (symbol, str(first), str(end), data_dir) == (
        "BTCUSDT",
        "2026-09-01",
        "2026-10-01",
        d.data_dir,
    )
    assert "preparing BTCUSDT candles 2026-09" in capsys.readouterr().out

    def broken(*a: Any, **k: Any) -> None:
        raise DataError("GET failed: no network")

    monkeypatch.setattr(catalog, "ensure_catalog", broken)
    with pytest.raises(CliError, match="cannot prepare the candles: GET failed: no network"):
        d.warm_data()


def test_the_live_stack_leaves_dev_views_off() -> None:
    """deploy/live/init.sh writes the live API config: it must not turn the per-tier dev views on."""
    init = Path(__file__).resolve().parents[3] / "deploy" / "live" / "init.sh"
    if not init.exists():
        pytest.skip("no deploy/live stack in this tree")
    assert '"dev_views"' not in init.read_text(encoding="utf-8")  # the JSON key
