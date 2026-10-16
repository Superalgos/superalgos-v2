"""One complete network run across real processes: sarelay, a provider replaying real history, followers.

Everything goes through the `sa` CLI and the sarelay binary; the tests only read what the processes left
behind (ledgers, bundle, relay storage) and query the relay like any other client.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from e2e_net import TIMINGS, Proc, free_port, sa, spawn, spawn_sa, wait_for
from sa_core.keys import DeviceKey
from sa_engine.network.ledger import Ledger
from sa_engine.network.replay import ReplaySpec

STRATEGY = Path(__file__).resolve().parent / "strategies" / "ema_fast_1h.json"
TIMEFRAME_S = 3600


@dataclass
class Relay:
    proc: Proc
    url: str
    admin_url: str
    storage: Path
    balances: Path
    time_scale: float

    def stop(self) -> None:
        self.proc.stop()


@dataclass
class Actor:
    name: str
    home: Path
    address: str
    pubkey: str
    tokens: int


@dataclass
class FollowerSpec:
    name: str
    tokens: int
    allocate: bool = True


@dataclass
class Run:
    workdir: Path
    relay: Relay
    provider: Actor
    state: Path
    spec_path: Path
    bundle: Path
    followers: list[Actor]
    ledgers: dict[str, Path]
    provider_summary: dict[str, Any]
    follower_summaries: dict[str, dict[str, Any]]
    follower_logs: dict[str, Path]
    speed: float
    window: tuple[str, str]
    extra: dict[str, object] = field(default_factory=dict)

    def ledger(self, name: str) -> Ledger:
        return Ledger(self.ledgers[name])

    @property
    def spec(self) -> ReplaySpec:
        return ReplaySpec.load(self.spec_path)


def start_relay(binary: Path, workdir: Path, *, time_scale: float, label: str = "") -> Relay:
    workdir.mkdir(parents=True, exist_ok=True)
    port, admin = free_port(), free_port()
    balances = workdir / "balances.json"
    balances.write_text("{}")
    storage = workdir / "relay-data"
    cfg = workdir / "sarelay.toml"
    cfg.write_text(
        f"""listen = "127.0.0.1:{port}"
storage_path = "{storage}"
allocation_delay = "0s"
core_delay = "0s"
max_anchor_latency = "0s"
time_scale = {time_scale!r}
[admin]
listen = "127.0.0.1:{admin}"
[log]
format = "json"
level = "info"
[oracle]
cache_ttl = "1s"
max_lookups_per_sec = 100000
lookup_burst = 100000
[limits]
max_conns_per_ip = 100000
max_pending_per_provider = 100000
max_pending_total = 1000000
[policy]
min_provider_power = 0
min_proposer_power = 0
provider_idle_expiry = "0s"
[rate]
zero_power_per_sec = 100000
zero_power_burst = 100000
powered_per_sec = 100000
powered_burst = 100000
[[chains]]
id = "fixture"
kind = "fixture"
file = "{balances}"
"""
    )
    t0 = time.time()
    proc = spawn(
        "sarelay", [str(binary), "-config", str(cfg)], workdir / "sarelay.log", env=dict(os.environ)
    )
    import socket

    def listening() -> bool:
        with socket.socket() as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) == 0

    wait_for(listening, 15, "sarelay to listen")
    TIMINGS[f"{label}relay start"] = time.time() - t0
    return Relay(
        proc, f"ws://127.0.0.1:{port}", f"http://127.0.0.1:{admin}", storage, balances, time_scale
    )


def make_actor(workdir: Path, name: str, balances: Path, tokens: int) -> Actor:
    home = workdir / name
    sa("keys", "init", home=home)
    args = ["identity", "--demo", "--balances", balances]
    if tokens:
        args += ["--tokens", str(tokens)]
    sa(*args, home=home)
    info = json.loads((home / "identity.json").read_text())
    return Actor(name, home, info["address"], info["device_pubkey"], tokens)


def device_key(actor: Actor) -> DeviceKey:
    """The actor's device key, unlocked with the e2e passphrase (throwaway)."""
    from e2e_net import PASSPHRASE
    from sa_core.keys import load_device_key

    return load_device_key(actor.home / "device.key.json", PASSPHRASE)


def last_json_line(text: str) -> dict[str, Any]:
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if line.startswith("{") and line.endswith("}"):
            parsed: dict[str, Any] = json.loads(line)
            return parsed
    raise AssertionError(f"no JSON summary in output:\n{text[-2000:]}")


def run_network(
    workdir: Path,
    binary: Path,
    *,
    speed: float,
    window: tuple[str, str],
    followers: list[FollowerSpec],
    tsa_config: Path,
    data_dir: Path,
    private_min: int | None = None,
    reveal: bool = False,
    lead: float = 20.0,
    settle_s: float = 4.0,
    label: str = "",
) -> Run:
    """Relay + provider replaying `window` at `speed` + followers; returns when everything has stopped."""
    relay = start_relay(
        binary, workdir, time_scale=1.0 / speed, label=f"{label}: " if label else ""
    )
    t_setup = time.time()
    provider = make_actor(workdir, "provider", relay.balances, 0)
    actors = [make_actor(workdir, f.name, relay.balances, f.tokens) for f in followers]
    state = workdir / "provider-state"
    reg = ["provider", "register", STRATEGY, "--relay", relay.url, "--state", state]
    if private_min is not None:
        reg += ["--private", str(private_min)]
    sa(*reg, home=provider.home)
    for spec, actor in zip(followers, actors, strict=True):
        if spec.allocate:
            sa(
                "allocate",
                "--provider",
                f"{provider.pubkey}=10000",
                "--relay",
                relay.url,
                home=actor.home,
            )
    TIMINGS[f"{label}: setup ({len(actors)} followers, keys + identities)"] = time.time() - t_setup

    bundle = workdir / "bundle.json"
    spec_path = state / "replay.json"
    run_args = [
        "provider", "run", STRATEGY, "--relay", relay.url, "--state", state, "--replay", *window,
        "--speed", str(speed), "--lead", str(lead), "--tsa-config", tsa_config, "--data-dir", data_dir,
        "--export", bundle, "--balances", relay.balances,
    ]  # fmt: skip
    if reveal:
        run_args.append("--reveal")
    t_run = time.time()
    prov = spawn_sa("provider", *run_args, home=provider.home, log=workdir / "provider.log")
    try:
        wait_for(
            lambda: spec_path.exists() or prov.popen.poll() is not None,
            180,
            "the provider to write replay.json",
        )
        if prov.popen.poll() is not None:
            raise AssertionError(f"provider exited early:\n{prov.output()}")
        t_followers = time.time()
        procs: dict[str, Proc] = {}
        ledgers: dict[str, Path] = {}
        logs: dict[str, Path] = {}
        for actor in actors:
            ledgers[actor.name] = workdir / f"{actor.name}.sqlite"
            logs[actor.name] = workdir / f"{actor.name}.log"
            procs[actor.name] = spawn_sa(
                actor.name, "follow", "--relay", relay.url, "--provider", provider.pubkey, "--replay-spec", spec_path,
                "--ledger", ledgers[actor.name], "--ready-file", workdir / f"{actor.name}.ready", "--tsa-config", tsa_config,
                "--data-dir", data_dir, home=actor.home, log=logs[actor.name],
            )  # fmt: skip
        for actor in actors:
            ready_file, proc = workdir / f"{actor.name}.ready", procs[actor.name]
            wait_ready(actor.name, ready_file, proc)
            assert ready_file.exists(), (
                f"{actor.name} exited before it was ready:\n{proc.output()[-3000:]}"
            )
        TIMINGS[f"{label}: followers ready (process start to subscribed)"] = (
            time.time() - t_followers
        )
        first_signal_in = ReplaySpec.load(spec_path).wall0_ms / 1000 - time.time()
        assert first_signal_in > 0, (
            f"followers were ready {-first_signal_in:.1f} s after the first signal: raise lead"
        )
        code = prov.wait(900)
        TIMINGS[f"{label}: provider run (backtest, lead, replay, last TSA batch)"] = (
            time.time() - t_run
        )
        assert code == 0, f"provider exited {code}:\n{prov.output()[-3000:]}"
        time.sleep(
            settle_s
        )  # the slowest tier (2 x timeframe, scaled) and the last attestations arrive
        summaries: dict[str, dict[str, Any]] = {}
        for name, proc in procs.items():
            proc.stop()
            summaries[name] = last_json_line(proc.output())
    except BaseException:
        prov.stop()
        relay.stop()
        raise
    return Run(
        workdir, relay, provider, state, spec_path, bundle, actors, ledgers, last_json_line(prov.output()), summaries, logs,
        speed, window,
    )  # fmt: skip


def wait_ready(name: str, ready_file: Path, proc: Proc) -> None:
    wait_for(
        lambda: ready_file.exists() or proc.popen.poll() is not None, 60, f"{name} to subscribe"
    )
