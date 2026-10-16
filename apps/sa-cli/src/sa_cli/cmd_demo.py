"""``sa demo``: the whole network on one machine with one command, no Docker.

Builds ``sarelay`` (Go) and the dashboard (bun) once, starts a relay with fixture balances, creates throwaway identities
(a public provider, a private provider, followers at T0, T2 and zero power), runs a backtest for the dashboard, starts
the providers replaying real BTCUSDT candles at demo speed (``--live``: the public provider trades live paper on
Binance data), the followers, and the read-only API serving the dashboard. Ctrl-C stops everything.

Throwaway keys, fixture balances, no real wallet, no real chain transaction, no real exchange order. By default signals
are anchored by three local throwaway time-stamp authorities (``--public-tsa`` uses the real public ones).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sa_cli import demo_plan as plan
from sa_cli.home import CliError, Home
from sa_core.tsa_lab import tsa_farm
from sa_engine.network.replay import MAX_SPEED, ReplaySpec

ROLES = ("all", "tsa", "setup", "provider", "follower", "api")


def say(msg: str) -> None:
    print(msg, flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def wait_until(cond: Callable[[], bool], timeout: float, what: str, every: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(every)
    raise CliError(
        f"timed out after {timeout:.0f} s waiting for {what}; see the logs in the demo directory"
    )


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as res:  # noqa: S310 - a loopback URL we built
            return int(res.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------- processes


@dataclass
class Child:
    name: str
    popen: subprocess.Popen[bytes]
    log: Path
    essential: bool = False
    reported: bool = False

    def tail(self, lines: int = 12) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"


@dataclass
class ProcessSet:
    """Children in their own sessions, so a Ctrl-C reaches only us, and we stop them in order."""

    log_dir: Path
    children: list[Child] = field(default_factory=list)

    def start(
        self,
        name: str,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        essential: bool = False,
    ) -> Child:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"{name}.log"
        with log.open("wb") as fh:
            popen = subprocess.Popen(  # noqa: S603 - our own argv list, no shell
                cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, cwd=cwd, start_new_session=True
            )
        child = Child(name, popen, log, essential)
        self.children.append(child)
        return child

    def failed(self) -> list[Child]:
        """Children that exited non-zero since the last call."""
        out = []
        for c in self.children:
            code = c.popen.poll()
            if code not in (None, 0) and not c.reported:
                c.reported = True
                out.append(c)
        return out

    def alive(self, name: str) -> bool:
        return any(c.name == name and c.popen.poll() is None for c in self.children)

    def stop_all(self, timeout: float = 10.0) -> None:
        """SIGTERM newest first (followers before providers before the relay), then SIGKILL the stragglers."""
        running = [c for c in reversed(self.children) if c.popen.poll() is None]
        for c in running:
            with contextlib.suppress(ProcessLookupError):
                c.popen.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout
        for c in running:
            try:
                c.popen.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                c.popen.kill()
                c.popen.wait(5)


# ---------------------------------------------------------------- the demo


def web_sources_newer_than(web: Path, built: Path) -> Path | None:
    """First dashboard input (web/src/**, web/index.html, the bun lockfile) modified after ``built``, else None."""
    stamp = built.stat().st_mtime_ns
    inputs = [web / "index.html", web / "bun.lock", web / "bun.lockb"]
    src = web / "src"
    if src.is_dir():
        inputs += (p for p in src.rglob("*") if p.is_file())
    for p in inputs:
        if p.is_file() and p.stat().st_mtime_ns > stamp:
            return p.relative_to(web.parent)
    return None


class Demo:
    def __init__(self, args: argparse.Namespace, repo: Path | None) -> None:
        self.args = args
        self.repo = repo
        if repo is None and not args.workdir:
            raise CliError("--workdir DIR is required without a checkout (container roles)")
        self.work = Path(args.workdir) if args.workdir else (repo or Path()) / ".sa-demo"
        self.data_dir = Path(
            args.data_dir or os.environ.get("SA_DATA_DIR") or (repo or self.work) / ".sa-data"
        ).resolve()
        self.live: bool = args.live
        self.speed: float = args.speed
        self.window = plan.pick_window(date.today(), args.days, args.month)
        self.procs = ProcessSet(self.work / "logs")
        self.stack = contextlib.ExitStack()
        self.tsa_config: Path | None = None
        self.relay_url = ""
        self.pubkeys: dict[str, str] = {}
        self.proposal_id = ""
        self.env = {
            **os.environ,
            "SA_PASSPHRASE": plan.PASSPHRASE,
            "SA_SCRYPT_N": plan.SCRYPT_N,
            "PYTHONUNBUFFERED": "1",
        }
        self.env.pop("SA_HOME", None)
        if not 0 < self.speed <= MAX_SPEED:
            raise CliError(
                f"--speed {self.speed:g} must be within (0, {MAX_SPEED:g}] market seconds per wall second"
            )

    # ------------------------------------------------------------ helpers

    @property
    def checkout(self) -> Path:
        if self.repo is None:
            raise CliError("this step builds from a superalgos-v2 checkout, and none was given")
        return self.repo

    def sa(self, home: str, *argv: object) -> str:
        """Run one `sa` command in this process against the demo identity ``home``; returns its stdout."""
        from sa_cli.main import main as sa_main  # noqa: PLC0415 - main imports this module

        h = Home(self.work / home, environ=self.env, interactive=lambda: False)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sa_main([str(a) for a in argv], home=h)
        if code != 0:
            raise CliError(
                f"`sa {' '.join(map(str, argv))}` failed in demo home {home}: {err.getvalue().strip() or out.getvalue().strip()}"
            )
        return out.getvalue()

    def pubkey(self, name: str) -> str:
        info = json.loads((self.work / name / "identity.json").read_text(encoding="utf-8"))
        return str(info["device_pubkey"])

    # ------------------------------------------------------------ steps

    def prepare_directory(self) -> None:
        if self.work.exists():
            if not (self.work / plan.DEMO_MARKER).is_file():
                raise CliError(
                    f"{self.work} exists and is not a demo directory (no {plan.DEMO_MARKER} marker); "
                    "pick another --workdir, or delete it yourself"
                )
            shutil.rmtree(self.work)
        for sub in ("logs", "bundles", "backtests", "relay-data"):
            (self.work / sub).mkdir(parents=True)
        (self.work / plan.DEMO_MARKER).write_text(
            "created by `sa demo`; safe to delete\n", encoding="utf-8"
        )
        (self.work / "balances.json").write_text("{}", encoding="utf-8")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def build_relay(self) -> Path:
        if self.args.relay_bin:
            binary = Path(self.args.relay_bin)
            if not binary.is_file():
                raise CliError(
                    f"--relay-bin {binary} does not exist; build it with `make -C relay build`"
                )
            return binary
        go = shutil.which("go")
        if go is None:
            raise CliError(
                "the Go toolchain (1.27 or newer) is needed to build the relay: install it, or pass --relay-bin PATH"
            )
        out = self.work / "sarelay"
        say("building the relay (go build) ...")
        res = subprocess.run(  # noqa: S603
            [go, "build", "-o", str(out), "./cmd/sarelay"],
            cwd=self.checkout / "relay",
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise CliError(
                f"go build of the relay failed:\n{res.stderr.strip()[-1500:]}\nfix the error or pass --relay-bin PATH"
            )
        return out

    def build_web(self) -> Path | None:
        web = self.checkout / "web"
        dist = web / "dist"
        built = (dist / "index.html").is_file()
        newer = web_sources_newer_than(web, dist / "index.html") if built else None
        if built and not self.args.rebuild_web and newer is None:
            return dist
        if self.args.no_web_build:
            if built and not self.args.rebuild_web:
                say(
                    f"note: web/dist is older than {newer} and --no-web-build is set: serving the stale build"
                )
                return dist
            say("note: web/dist is not built and --no-web-build is set: serving the API only")
            return None
        if newer is not None and not self.args.rebuild_web:
            say(f"web/dist is older than {newer}: rebuilding the dashboard")
        bun = shutil.which("bun")
        if bun is None:
            raise CliError(
                "bun is needed to build the dashboard once (https://bun.sh); or pass --no-web-build to run without it"
            )
        say("building the dashboard (bun) ...")
        for cmd in ([bun, "install", "--frozen-lockfile"], [bun, "run", "build"]):
            res = subprocess.run(  # noqa: S603
                cmd, cwd=self.checkout / "web", capture_output=True, text=True, check=False
            )
            if res.returncode != 0:
                raise CliError(
                    f"`{' '.join(cmd[1:])}` failed in web/:\n{(res.stdout + res.stderr).strip()[-1500:]}"
                )
        return dist

    def start_relay(self, binary: Path) -> None:
        port, admin = free_port(), free_port()
        scale = 1.0 if self.live else 1.0 / self.speed
        cfg = self.work / "sarelay.toml"
        cfg.write_text(
            plan.relay_config(
                port=port,
                admin_port=admin,
                storage=self.work / "relay-data",
                balances=self.work / "balances.json",
                time_scale=scale,
            ),
            encoding="utf-8",
        )
        self.procs.start(
            "relay", [str(binary), "-config", str(cfg)], env=dict(os.environ), essential=True
        )
        wait_until(
            lambda: port_open(port) or not self.procs.alive("relay"), 20, "the relay to listen"
        )
        if not self.procs.alive("relay"):
            raise CliError(f"the relay exited at start-up:\n{self.procs.children[-1].tail()}")
        self.relay_url = f"ws://127.0.0.1:{port}"

    def create_identities(self) -> None:
        bal = self.work / "balances.json"
        tiers = ()  # the protocol's default table: 500k@0, 100k@0.1, 10k@0.5, 0@2
        for name, tokens in ((plan.ATLAS, 0), (plan.FOX, 0), *plan.FOLLOWERS):
            self.sa(name, "keys", "init")
            args: list[object] = ["identity", "--demo", "--balances", bal, "--days", 30]
            if tokens:
                args += ["--tokens", tokens]
            self.sa(name, *args)
            self.pubkeys[name] = self.pubkey(name)
        self.sa(plan.ATLAS, "provider", "register", plan.strategy_path(plan.ATLAS_STRATEGY), "--relay", self.relay_url,
                "--state", self.work / "state-atlas", *tiers)  # fmt: skip
        self.sa(plan.FOX, "provider", "register", plan.strategy_path(plan.FOX_STRATEGY), "--relay", self.relay_url,
                "--state", self.work / "state-fox", "--private", plan.PRIVATE_MIN_POWER)  # fmt: skip
        a, f = self.pubkeys[plan.ATLAS], self.pubkeys[plan.FOX]
        self.sa(
            "whale",
            "allocate",
            "--provider",
            f"{a}=6000",
            "--provider",
            f"{f}=3000",
            "--governance",
            1000,
            "--relay",
            self.relay_url,
        )
        self.sa(
            "mid",
            "allocate",
            "--provider",
            f"{a}=7000",
            "--governance",
            3000,
            "--relay",
            self.relay_url,
        )

    def governance(self) -> None:
        out = self.sa("whale", "gov", "propose", "--title", "Raise the zero-power delay to 3x the timeframe?",
                      "--option", "Yes", "--option", "No", "--deadline-in", "1d", "--relay", self.relay_url)  # fmt: skip
        found = re.search(r"proposal ([0-9a-f]{64})", out)
        if found is None:
            raise CliError(f"could not read the proposal id from: {out.strip()}")
        self.proposal_id = found.group(1)
        self.sa("whale", "gov", "vote", self.proposal_id, "--option", 0, "--relay", self.relay_url)
        self.sa("mid", "gov", "vote", self.proposal_id, "--option", 1, "--relay", self.relay_url)

    def warm_data(self) -> None:
        """Download and convert the month of candles once, before the processes that all read it start together."""
        from sa_engine.catalog import ensure_catalog  # noqa: PLC0415 - Nautilus loads only when it is needed
        from sa_engine.data import DataError  # noqa: PLC0415

        first = date.fromisoformat(self.window.backtest_start)
        end = date.fromisoformat(self.window.replay_end)
        say(
            f"preparing BTCUSDT candles {first:%Y-%m} (the first run downloads about 40 MB from data.binance.vision) ..."
        )
        try:
            ensure_catalog(plan.INSTRUMENT.partition(":")[2], first, end, self.data_dir)
        except DataError as exc:
            raise CliError(f"cannot prepare the candles: {exc}") from exc

    def backtest_argv(self, strategy: str) -> list[str]:
        w = self.window
        return plan.sa_command(
            self.work / plan.ATLAS, "backtest", plan.strategy_path(strategy), "--start", w.backtest_start,
            "--end", w.backtest_end, "--data-dir", self.data_dir, "--json", "-o",
            self.work / "backtests" / f"{strategy}.json",
        )  # fmt: skip

    def start_backtests(self) -> None:
        for strategy in (plan.ATLAS_STRATEGY, plan.FOX_STRATEGY):
            self.procs.start(f"backtest-{strategy}", self.backtest_argv(strategy), env=self.env)

    def provider_jobs(self) -> list[tuple[str, str, bool, bool]]:
        """(name, strategy, reveal in bundle, live) of the providers that run in this mode."""
        jobs = [(plan.ATLAS, plan.ATLAS_STRATEGY, False, self.live)]
        if not self.live:  # live mode runs the public provider only: one relay has one time scale
            jobs.append((plan.FOX, plan.FOX_STRATEGY, True, False))
        return jobs

    def provider_argv(
        self, name: str, strategy: str, reveal: bool, live: bool, wall0: float
    ) -> list[str]:
        return plan.provider_command(
            home=self.work / name, strategy=plan.strategy_path(strategy), relay_url=self.relay_url, state=self.work / f"state-{name}",
            window=self.window, speed=self.speed, wall0_s=wall0, tsa_config=self.tsa_config, data_dir=self.data_dir,
            bundle=self.work / "bundles" / f"{name}.json", balances=self.work / "balances.json", reveal=reveal, live=live,
        )  # fmt: skip

    def write_replay_spec(self, wall0: float) -> None:
        if not self.live:
            ReplaySpec(instrument=plan.INSTRUMENT, start=self.window.replay_start, end=self.window.replay_end,
                       speed=self.speed, wall0_ms=int(wall0 * 1000)).save(self.work / "replay.json")  # fmt: skip

    def start_providers(self) -> float:
        wall0 = time.time() + float(self.args.lead)
        self.write_replay_spec(wall0)
        for name, strategy, reveal, live in self.provider_jobs():
            self.procs.start(
                f"provider-{name}",
                self.provider_argv(name, strategy, reveal, live, wall0),
                env=self.env,
            )
        return wall0

    def follower_argv(self, name: str) -> list[str]:
        a, f = self.pubkeys[plan.ATLAS], self.pubkeys[plan.FOX]
        follows = [a, f] if name == "whale" and not self.live else [a]
        return plan.follower_command(
            home=self.work / name, relay_url=self.relay_url, providers=follows,
            spec=None if self.live else self.work / "replay.json", tsa_config=self.tsa_config, data_dir=self.data_dir,
            ready_file=self.work / f"{name}.ready",
        )  # fmt: skip

    def start_followers(self) -> None:
        for name, _ in plan.FOLLOWERS:
            self.procs.start(f"follower-{name}", self.follower_argv(name), env=self.env)
        for name, _ in plan.FOLLOWERS:
            ready = self.work / f"{name}.ready"
            wait_until(self.follower_settled(name, ready), 120, f"follower {name} to subscribe")
            if not ready.exists():
                raise CliError(
                    f"follower {name} exited before it was ready:\n{self.child(f'follower-{name}').tail()}"
                )

    def follower_settled(self, name: str, ready: Path) -> Callable[[], bool]:
        return lambda: ready.exists() or not self.procs.alive(f"follower-{name}")

    def child(self, name: str) -> Child:
        return next(c for c in self.procs.children if c.name == name)

    def write_api_config(self, web_dist: Path | None) -> Path:
        cfg = plan.api_config(
            relay_url=self.relay_url, work=self.work, web_dist=web_dist, tsa_config=self.tsa_config, speed=self.speed,
            pubkeys=self.pubkeys, live=self.live, data_dir=self.data_dir,
        )  # fmt: skip
        path = self.work / "api-config.json"
        plan.write_api_config(path, cfg)
        return path

    def start_api(self, web_dist: Path | None) -> str:
        path = self.write_api_config(web_dist)
        port = self.args.port
        if port_open(port):
            raise CliError(f"port {port} is already in use; pick another with --port")
        self.procs.start(
            "api",
            [sys.executable, "-m", "sa_api", "--config", str(path), "--port", str(port)],
            env=self.env,
            essential=True,
        )
        url = f"http://127.0.0.1:{port}"
        wait_until(
            lambda: http_ok(f"{url}/api/network") or not self.procs.alive("api"),
            30,
            "the API to answer",
        )
        if not self.procs.alive("api"):
            raise CliError(f"the API exited at start-up:\n{self.child('api').tail()}")
        return url

    # ------------------------------------------------------------ container roles (deploy/docker-compose.yml)

    def block(self) -> None:
        """Serve until SIGTERM or SIGINT."""

        def stop(signum: int, _frame: object) -> None:
            raise KeyboardInterrupt(signal.Signals(signum).name)

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        with contextlib.suppress(KeyboardInterrupt):
            while True:
                time.sleep(1.0)

    def use_shared_files(self) -> None:
        """What a role needs from the others: the relay URL, the TSA config and the replay clock the setup wrote."""
        if not self.args.relay_url:
            raise CliError("--relay-url ws://HOST:PORT is required for this role")
        self.relay_url = self.args.relay_url
        if not self.args.public_tsa:
            self.tsa_config = self.work / "tsa" / "tsa-config.json"
        for name in (plan.ATLAS, plan.FOX, *(n for n, _ in plan.FOLLOWERS)):
            self.pubkeys[name] = self.pubkey(name)
        spec_path = self.work / "replay.json"
        if spec_path.is_file():
            spec = ReplaySpec.load(spec_path)
            self.speed = spec.speed
            self.window = plan.Window(
                self.window.backtest_start, self.window.backtest_end, spec.start, spec.end
            )

    def role_tsa(self) -> int:
        farm = tsa_farm(
            self.work / "tsa",
            host="0.0.0.0",  # noqa: S104 - a container binds its own interface; compose publishes nothing
            port=self.args.tsa_port,
            farm_gap_s=1.0,
            advertise=self.args.tsa_host,
        )
        with farm as cfg:
            say(f"throwaway TSA farm up; config {cfg}")
            self.block()
        return 0

    def role_setup(self) -> int:
        if not self.args.relay_url:
            raise CliError("--relay-url ws://HOST:PORT is required for this role")
        for sub in ("logs", "bundles", "backtests"):
            (self.work / sub).mkdir(parents=True, exist_ok=True)
        (self.work / plan.DEMO_MARKER).write_text(
            "created by `sa demo`; safe to delete\n", encoding="utf-8"
        )
        bal = self.work / "balances.json"
        if not bal.exists():
            bal.write_text("{}", encoding="utf-8")
        self.relay_url = self.args.relay_url
        host, _, port = self.relay_url.removeprefix("ws://").partition(":")
        wait_until(lambda: port_open(int(port), host), 120, f"the relay at {self.relay_url}")
        if not self.args.public_tsa:
            self.tsa_config = self.work / "tsa" / "tsa-config.json"
            wait_until(self.tsa_config.is_file, 120, "the TSA farm to write its config")
        say("creating throwaway identities and registering providers ...")
        self.create_identities()
        self.governance()
        self.warm_data()
        self.write_replay_spec(time.time() + float(self.args.lead))
        self.write_api_config(self.args.web_dist)
        for strategy in (plan.ATLAS_STRATEGY, plan.FOX_STRATEGY):
            say(f"backtesting {strategy} ...")
            res = subprocess.run(  # noqa: S603
                self.backtest_argv(strategy),
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raise CliError(
                    f"backtest of {strategy} failed:\n{(res.stdout + res.stderr).strip()[-1500:]}"
                )
        say("setup done")
        return 0

    def role_exec(self, argv: list[str]) -> int:
        """Become the real process, so the container's signals and exit code are its own."""
        say("exec " + " ".join(argv[:6]) + " ...")
        os.execvpe(argv[0], argv, self.env)  # noqa: S606 - our own argv list

    def role_provider(self) -> int:
        self.use_shared_files()
        name = self.args.name
        jobs = {j[0]: j for j in self.provider_jobs()}
        if name not in jobs:
            raise CliError(f"--name {name!r} is not a provider; use one of {sorted(jobs)}")
        _, strategy, reveal, live = jobs[name]
        wall0 = 0.0 if live else ReplaySpec.load(self.work / "replay.json").wall0_ms / 1000
        return self.role_exec(self.provider_argv(name, strategy, reveal, live, wall0))

    def role_follower(self) -> int:
        self.use_shared_files()
        if self.args.name not in {n for n, _ in plan.FOLLOWERS}:
            raise CliError(
                f"--name {self.args.name!r} is not a follower; use one of {[n for n, _ in plan.FOLLOWERS]}"
            )
        return self.role_exec(self.follower_argv(self.args.name))

    def role_api(self) -> int:
        path = self.work / "api-config.json"
        if not path.is_file():
            raise CliError(f"{path} is missing; the setup role writes it")
        cmd = [
            sys.executable,
            "-m",
            "sa_api",
            "--config",
            str(path),
            "--host",
            self.args.api_host,
            "--port",
            str(self.args.port),
        ]
        return self.role_exec(cmd)

    # ------------------------------------------------------------ run

    def run(self) -> int:
        def interrupt(signum: int, _frame: object) -> None:
            raise KeyboardInterrupt(signal.Signals(signum).name)

        old = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
        started = time.monotonic()
        try:
            try:
                self.prepare_directory()
                binary = self.build_relay()
                web_dist = self.build_web()
                if self.args.public_tsa:
                    say("anchoring with the public time-stamp authorities (network needed)")
                else:
                    self.tsa_config = self.stack.enter_context(
                        tsa_farm(self.work / "tsa", farm_gap_s=1.0)
                    )
                say(
                    f"starting the relay ({'live time' if self.live else f'demo time scale 1/{self.speed:g}'}) ..."
                )
                self.start_relay(binary)
                say(
                    "creating throwaway identities: atlas (public), fox (private), whale, mid, zero ..."
                )
                self.create_identities()
                self.governance()
                self.warm_data()
                self.start_backtests()
                wall0 = self.start_providers()
                self.start_followers()
                url = self.start_api(web_dist)
                self.announce(url, wall0, time.monotonic() - started)
                return self.wait()
            except KeyboardInterrupt:
                say("\nstopping ...")
                return 0
        finally:
            for s, h in old.items():
                signal.signal(s, h)
            self.procs.stop_all()
            self.stack.close()
            say("stopped; everything the demo started is gone")

    def announce(self, url: str, wall0: float, setup_s: float) -> None:
        first = max(0.0, wall0 - time.time())
        mode = "LIVE paper trading (atlas)" if self.live else (
            f"replay of {self.window.replay_start}..{self.window.replay_end} at {self.speed:g}x"
        )  # fmt: skip
        anchors = "public TSAs" if self.args.public_tsa else "local throwaway TSAs (demo)"
        say("")
        say(f"Superalgos v2 demo is up ({setup_s:.0f} s): {mode}; anchors: {anchors}")
        say(f"  dashboard   {url}")
        say(f"  relay       {self.relay_url}")
        say(f"  logs        {self.work / 'logs'}")
        say("  providers   atlas (public, 4 tiers), fox (private, 10,000 SA)")
        say("  followers   whale 1,000,000 SA, mid 20,000 SA, zero 0 SA")
        if self.live:
            say(
                "  atlas signals when its 1h EMA strategy trades on live Binance data; fox is idle in live mode"
            )
        else:
            say(f"  first signals in about {first:.0f} s; the pages fill as the replay runs")
        say(f"READY {url}")
        say("Press Ctrl-C to stop.")

    def wait(self) -> int:
        deadline = time.monotonic() + self.args.duration if self.args.duration else None
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.5)
            for c in self.procs.failed():
                say(f"{c.name} exited with code {c.popen.returncode}:\n{c.tail()}")
                if c.essential:
                    return 1
        return 0


def demo(args: argparse.Namespace, home: Home) -> int:
    if args.role == "all":
        return Demo(args, plan.find_repo_root()).run()
    if args.live:
        raise CliError(
            "--live runs on one machine only (--role all); the container network replays"
        )
    d = Demo(args, None)
    roles = {
        "tsa": d.role_tsa, "setup": d.role_setup, "provider": d.role_provider,
        "follower": d.role_follower, "api": d.role_api,
    }  # fmt: skip
    return roles[args.role]()


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--workdir", type=Path, help="demo directory, wiped at start (default: <repo>/.sa-demo)"
    )
    p.add_argument(
        "--port", type=int, default=8787, help="dashboard and API port on 127.0.0.1 (default 8787)"
    )
    p.add_argument(
        "--speed",
        type=float,
        default=1440.0,
        help="replay: market seconds per wall second (default 1440: a day in 60 s)",
    )
    p.add_argument("--days", type=int, default=2, help="days of real candles to replay (default 2)")
    p.add_argument(
        "--month",
        metavar="YYYY-MM-01",
        help="month of the Binance archive to use (default: the latest complete one)",
    )
    p.add_argument(
        "--lead",
        type=float,
        default=25.0,
        help="seconds before the first replayed signal (default 25)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="the public provider paper-trades live Binance data (needs the network)",
    )
    p.add_argument(
        "--public-tsa",
        action="store_true",
        help="anchor with the real public TSAs instead of local throwaway ones",
    )
    p.add_argument(
        "--duration",
        type=float,
        help="stop by itself after this many seconds (headless runs, smoke tests)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        help="Binance archive cache (default $SA_DATA_DIR or <repo>/.sa-data)",
    )
    p.add_argument(
        "--relay-bin", type=Path, help="use this sarelay binary instead of building relay/"
    )
    p.add_argument(
        "--no-web-build",
        action="store_true",
        help="do not build web/dist (API only if it is missing)",
    )
    p.add_argument("--rebuild-web", action="store_true", help="rebuild web/dist even if it exists")
    p.add_argument(
        "--role",
        choices=ROLES,
        default="all",
        help="one part of the network, for containers (deploy/docker-compose.yml); default: everything on this machine",
    )
    p.add_argument("--name", help="--role provider|follower: atlas, fox, whale, mid or zero")
    p.add_argument(
        "--relay-url", help="--role setup|provider|follower: ws://HOST:PORT of the relay"
    )
    p.add_argument(
        "--web-dist", type=Path, help="--role setup: built dashboard for the API to serve"
    )
    p.add_argument(
        "--api-host", default="127.0.0.1", help="--role api: bind address (default 127.0.0.1)"
    )
    p.add_argument(
        "--tsa-port", type=int, default=8080, help="--role tsa: port of the throwaway TSA farm"
    )
    p.add_argument(
        "--tsa-host", default="tsa", help="--role tsa: host name the other roles reach the farm by"
    )


__all__ = ["Demo", "ProcessSet", "add_arguments", "demo"]
