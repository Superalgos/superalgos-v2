"""``sa demo``: the plan as data. Dates, configuration files and command lines, with no process started here.

The cast (all throwaway keys, fixture balances; the relay's balance fixture is one JSON file):

=========  ===========  ============================================================================
who        SA tokens    role
=========  ===========  ============================================================================
atlas      0            public provider, strategy ``atlas-ema-1h``, the default four-tier table
fox        0            private provider (threshold 10,000 SA allocated), strategy ``fox-ema-1h``
whale      1,000,000    60% to atlas (T0), 30% to fox (entitled), 10% governance
mid        20,000       70% to atlas (T2), 30% governance; the dashboard's "My power"
zero       0            follows atlas with no allocation: the zero-power tier
=========  ===========  ============================================================================
"""

from __future__ import annotations

import json
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sa_cli.home import CliError

PASSPHRASE = "demo-throwaway-passphrase"  # noqa: S105 - keys are throwaway and live in the demo directory only
SCRYPT_N = "1024"  # cheap KDF: demo keys protect nothing
INSTRUMENT = "BINANCE:BTCUSDT"
DEMO_MARKER = ".sa-demo"

ATLAS, FOX = "atlas", "fox"
FOLLOWERS: tuple[tuple[str, int], ...] = (("whale", 1_000_000), ("mid", 20_000), ("zero", 0))
ATLAS_STRATEGY, FOX_STRATEGY = "atlas-ema-1h", "fox-ema-1h"
PRIVATE_MIN_POWER = 10_000
LABELS = {ATLAS: "Atlas EMA (public)", FOX: "Fox EMA (private)"}


@dataclass(frozen=True)
class Window:
    """Replay and backtest windows, ``YYYY-MM-DD`` (end exclusive). Both lie in one monthly archive."""

    backtest_start: str
    backtest_end: str
    replay_start: str
    replay_end: str


def latest_complete_month(today: date) -> date:
    """First day of the most recent calendar month that has ended."""
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def pick_window(today: date, days: int, month_start: str | None = None) -> Window:
    """The last ``days`` days of a month are replayed; the days before them (up to 14) are backtested.

    Default month: the latest complete one (Binance publishes monthly archives after the month ends).
    """
    if days < 1:
        raise CliError(f"--days {days} must be at least 1; a replay needs a day of candles")
    try:
        first = date.fromisoformat(month_start) if month_start else latest_complete_month(today)
    except ValueError as exc:
        raise CliError(f"--month {month_start!r} must look like 2026-09-01") from exc
    first = first.replace(day=1)
    n = monthrange(first.year, first.month)[1]
    if days > n - 3:
        raise CliError(
            f"--days {days} leaves no room to backtest in a {n}-day month; use at most {n - 3}"
        )
    end = first + timedelta(days=n)
    replay_start = end - timedelta(days=days)
    bt_end = min(first + timedelta(days=14), replay_start)
    return Window(first.isoformat(), bt_end.isoformat(), replay_start.isoformat(), end.isoformat())


def find_repo_root(start: Path | None = None) -> Path:
    """The superalgos-v2 checkout: the demo builds the relay (Go) and the dashboard (bun) from source."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "relay" / "go.mod").is_file() and (parent / "web" / "package.json").is_file():
            return parent
    raise CliError(
        "sa demo builds the relay and the dashboard from a superalgos-v2 checkout, and none was found above "
        f"{here}; run it from a clone: git clone <repo> && cd superalgos-v2 && uv sync --all-packages && uv run sa demo"
    )


def strategy_path(name: str) -> Path:
    path = Path(__file__).resolve().parent / "demo_data" / f"{name}.json"
    if not path.is_file():
        raise CliError(f"demo strategy {name} is missing at {path}; reinstall sa-cli")
    return path


def relay_config(
    *, port: int, admin_port: int, storage: Path, balances: Path, time_scale: float
) -> str:
    """TOML for sarelay: no allocation delay, no throttling, the shared fixture balances."""
    return f"""listen = "127.0.0.1:{port}"
storage_path = "{storage}"
allocation_delay = "0s"
core_delay = "0s"
max_anchor_latency = "0s"
time_scale = {time_scale!r}
[admin]
listen = "127.0.0.1:{admin_port}"
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


def api_config(
    *,
    relay_url: str,
    work: Path,
    web_dist: Path | None,
    tsa_config: Path | None,
    speed: float,
    pubkeys: dict[str, str],
    live: bool,
    data_dir: Path,
) -> dict[str, object]:
    """The JSON ``sa-api --config`` reads: every data source the demo produced."""
    followers = [{"home": str(work / name)} for name, _ in FOLLOWERS]
    replay_specs = (
        {}  # live mode: the public provider is scored against Binance's REST klines, the private one is idle
        if live
        else {pubkeys[ATLAS]: str(work / "replay.json"), pubkeys[FOX]: str(work / "replay.json")}
    )
    cfg: dict[str, object] = {
        "relays": [relay_url],
        "balances": str(work / "balances.json"),
        "time_scale": 1.0 if live else 1.0 / speed,
        "delay_d_s": 0,
        "governance_anchor": "created-at",  # DEMO ONLY: no OpenTimestamps wait in a demo
        "bundles_dir": str(work / "bundles"),
        "backtests_dir": str(work / "backtests"),
        "replay_specs": replay_specs,
        "data_dir": str(data_dir),
        "labels": {pubkeys[ATLAS]: LABELS[ATLAS], pubkeys[FOX]: LABELS[FOX]},
        "me_home": str(work / "mid"),
        "followers": followers,
        "dev_views": True,  # the demo shows the per-tier delivery timeline (a dev view); a production deployment leaves it off
    }
    if tsa_config is not None:
        cfg["tsa_config"] = str(tsa_config)
    if web_dist is not None:
        cfg["web_dist"] = str(web_dist)
    return cfg


def sa_command(home: Path, *args: object) -> list[str]:
    return [sys.executable, "-m", "sa_cli", "--home", str(home), *map(str, args)]


def provider_command(
    *,
    home: Path,
    strategy: Path,
    relay_url: str,
    state: Path,
    window: Window,
    speed: float,
    wall0_s: float,
    tsa_config: Path | None,
    data_dir: Path,
    bundle: Path,
    balances: Path,
    reveal: bool,
    live: bool,
) -> list[str]:
    cmd: list[object] = [
        *sa_command(home, "provider", "run", strategy, "--relay", relay_url, "--state", state)
    ]
    if live:
        cmd += ["--paper"]
    else:
        cmd += [
            "--replay",
            window.replay_start,
            window.replay_end,
            "--speed",
            speed,
            "--start-at",
            wall0_s,
        ]
    cmd += [
        "--data-dir",
        data_dir,
        "--export",
        bundle,
        "--balances",
        balances,
        "--anchor-timeout",
        60,
    ]
    if tsa_config is not None:
        cmd += ["--tsa-config", tsa_config]
    if reveal:
        cmd += ["--reveal"]
    return [str(c) for c in cmd]


def follower_command(
    *,
    home: Path,
    relay_url: str,
    providers: list[str],
    spec: Path | None,
    tsa_config: Path | None,
    data_dir: Path,
    ready_file: Path,
) -> list[str]:
    cmd: list[object] = [
        *sa_command(
            home, "follow", "--relay", relay_url, "--ready-file", ready_file, "--data-dir", data_dir
        )
    ]
    for p in providers:
        cmd += ["--provider", p]
    cmd += ["--replay-spec", spec] if spec is not None else ["--executor", "nautilus"]
    if tsa_config is not None:
        cmd += ["--tsa-config", tsa_config]
    return [str(c) for c in cmd]


def write_api_config(path: Path, cfg: dict[str, object]) -> None:
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
