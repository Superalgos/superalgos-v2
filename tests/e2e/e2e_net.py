"""Process plumbing for the e2e tests: the real sarelay binary, `sa` subprocesses, a small amount of polling."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASSPHRASE = "e2e-throwaway-passphrase"
TIMINGS: dict[str, float] = {}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_relay(dest: Path) -> Path:
    """`go build` the real relay (the same binary `make -C relay build` makes)."""
    t0 = time.time()
    out = dest / "sarelay"
    go = shutil.which("go")
    assert go, "the Go toolchain is needed to build the relay: install Go 1.27 or newer"
    subprocess.run([go, "build", "-o", str(out), "./cmd/sarelay"], cwd=ROOT / "relay", check=True)
    TIMINGS["go build sarelay"] = time.time() - t0
    return out


def sa_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "SA_PASSPHRASE": PASSPHRASE,
        "SA_SCRYPT_N": "1024",
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("SA_HOME", None)
    env.update(extra or {})
    return env


def sa(*args: object, home: Path | None = None, check: bool = True, env: dict[str, str] | None = None,
       timeout: float = 600) -> subprocess.CompletedProcess[str]:  # fmt: skip
    """Run `sa` to completion."""
    cmd = [
        sys.executable,
        "-m",
        "sa_cli",
        *(["--home", str(home)] if home else []),
        *map(str, args),
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True, env=sa_env(env), timeout=timeout, cwd=ROOT, check=False
    )
    if check and res.returncode != 0:
        raise AssertionError(
            f"`sa {' '.join(map(str, args))}` exited {res.returncode}\nstdout: {res.stdout}\nstderr: {res.stderr}"
        )
    return res


@dataclass
class Proc:
    """A long-running subprocess with its output captured in a file."""

    name: str
    popen: subprocess.Popen[bytes]
    log: Path
    started: float = field(default_factory=time.time)

    def output(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def wait(self, timeout: float) -> int:
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired as exc:
            self.stop()
            raise AssertionError(
                f"{self.name} did not finish in {timeout:.0f} s\n{self.output()[-3000:]}"
            ) from exc

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 15) -> int:
        if self.popen.poll() is None:
            self.popen.send_signal(sig)
            try:
                self.popen.wait(timeout)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait(5)
        return int(self.popen.returncode)


def spawn(
    name: str, cmd: list[str], log: Path, env: dict[str, str] | None = None, cwd: Path | None = None
) -> Proc:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as fh:
        popen = subprocess.Popen(
            cmd, stdout=fh, stderr=subprocess.STDOUT, env=env or sa_env(), cwd=cwd or ROOT
        )
    return Proc(name, popen, log)


def spawn_sa(
    name: str, *args: object, home: Path, log: Path, env: dict[str, str] | None = None
) -> Proc:
    return spawn(
        name,
        [sys.executable, "-m", "sa_cli", "--home", str(home), *map(str, args)],
        log,
        sa_env(env),
    )


def wait_for(cond: Callable[[], bool], timeout: float, what: str, every: float = 0.1) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
