"""`make demo-smoke`: start `sa demo` headless, hit every API endpoint, assert real data, shut down.

Exit 0 only if every check passes and the demo stops cleanly on SIGINT. Takes about a minute after the first build.
Nothing here uses a real wallet, a real chain or a real exchange: the demo's keys are throwaway.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
READY_TIMEOUT_S = 300  # includes the first go build and web build
DATA_TIMEOUT_S = 120  # after READY: lead 15 s + a 30 s replay + the last anchors


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def fetch(base: str, path: str, method: str = "GET") -> tuple[int, Any]:
    req = urllib.request.Request(  # noqa: S310 - a loopback http URL we built
        base + path, method=method, data=b"{}" if method != "GET" else None
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - loopback
            body = res.read()
            return res.status, json.loads(
                body
            ) if res.headers.get_content_type() == "application/json" else body.decode()
    except urllib.error.HTTPError as exc:
        return exc.code, None


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str) -> None:
        self.rows.append((ok, name, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)

    @property
    def ok(self) -> bool:
        return all(r[0] for r in self.rows)


def eventually(check: Callable[[], str | None], timeout: float) -> str | None:
    """Run ``check`` until it returns None (success) or ``timeout`` passes; returns the last complaint."""
    deadline = time.monotonic() + timeout
    why: str | None = "never ran"
    while time.monotonic() < deadline:
        try:
            why = check()
        except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            why = f"{type(exc).__name__}: {exc}"
        if why is None:
            return None
        time.sleep(1.0)
    return why


def run_checks(base: str, report: Report) -> None:
    state: dict[str, Any] = {}

    def providers() -> str | None:
        code, body = fetch(base, "/api/providers")
        pubs = [p for p in body if p["mode"] == "public"]
        privs = [p for p in body if p["mode"] == "private"]
        if code != 200 or not pubs or not privs:
            return f"status {code}, public {len(pubs)}, private {len(privs)}"
        if not all(p["followers"] > 0 and p["power_backing"] > 0 and p["tiers"] for p in body):
            return "a provider has no followers, power or tiers yet"
        state["public"], state["private"] = pubs[0], privs[0]
        return None

    def signals() -> str | None:
        pk = state["public"]["pubkey"]
        code, sigs = fetch(base, f"/api/providers/{pk}/signals?limit=50")
        if code != 200 or len(sigs) < 2:
            return f"{len(sigs or [])} signals so far"
        if any("release" in s for s in sigs):
            return "the provider's signals carry per-tier release data (a dev view)"
        if not all(
            s["status"] == ("out" if s["anchored_ms"] else "waiting_for_time_stamp") for s in sigs
        ):
            return "a signal whose status does not follow its time-stamp"
        if not any(s["anchored_ms"] for s in sigs):
            return "no anchored signal yet"
        state["signals"] = sigs
        return None

    def dev_releases() -> str | None:
        """`sa demo` turns the dev views on: the per-tier timeline data is served."""
        pk = state["public"]["pubkey"]
        code, sigs = fetch(base, f"/api/dev/providers/{pk}/releases?limit=50")
        if code != 200 or not sigs:
            return f"status {code}, {len(sigs or [])} signals so far"
        if not all(len(s["release"]) == len(state["public"]["tiers"]) for s in sigs):
            return "a signal without one release per tier"
        state["releases"] = len(sigs[0]["release"])
        return None

    def my_signals() -> str | None:
        """The viewer's own follower ledger: what it received, its tier's delay, what it did with each."""
        code, rows = fetch(base, "/api/me/signals")
        if code != 200 or not rows:
            return f"status {code}, {len(rows or [])} received so far"
        if not all(
            r["outcome"] in ("executed", "rejected", "pending") and r["received_ms"] > 0
            for r in rows
        ):
            return f"a row without a result or receive time: {rows[0]}"
        state["my_signals"] = rows
        return None

    def record() -> str | None:
        pk = state["public"]["pubkey"]
        code, rec = fetch(base, f"/api/providers/{pk}/record")
        v = rec["verification"]
        if code != 200 or not rec["trades"] or not rec["equity"]:
            return f"{len(rec['trades'])} trades, {len(rec['equity'])} equity points so far"
        if not (v["signatures_ok"] and v["chain_complete"] and v["anchored_pct"] > 0):
            return f"verification {v}"
        state["record"] = rec
        return None

    def private_revealed() -> str | None:
        pk = state["private"]["pubkey"]
        code, rec = fetch(base, f"/api/providers/{pk}/record")
        code2, sigs = fetch(base, f"/api/providers/{pk}/signals?limit=50")
        if code != 200 or code2 != 200 or not sigs or not all(s["private"] for s in sigs):
            return f"{len(sigs or [])} revealed private signals so far (revealed in the bundle when its replay ends)"
        state["private_record"] = rec
        return None

    def backtests() -> str | None:
        code, bts = fetch(base, "/api/backtests")
        if code != 200 or not bts or not bts[0]["equity"] or bts[0]["trades"] < 1:
            return f"status {code}, {len(bts or [])} backtests"
        code, one = fetch(base, f"/api/backtests/{bts[0]['id']}")
        state["backtests"] = bts
        return None if code == 200 and one["id"] == bts[0]["id"] else "backtest detail differs"

    def backtest_fills() -> str | None:
        """F7: the stored report's fills reach the API (chart markers), inside the backtest's own dates."""
        bt = state["backtests"][0]
        code, one = fetch(base, f"/api/backtests/{bt['id']}")
        if (
            code != 200
            or not one["fills"]
            or one["timeframe"] not in ("1m", "5m", "15m", "1h", "4h", "1d")
        ):
            return f"status {code}, {len(one['fills'] if one else [])} fills, timeframe {one and one['timeframe']}"
        lo = datetime.fromisoformat(bt["start"]).replace(tzinfo=UTC).timestamp() * 1000
        hi = datetime.fromisoformat(bt["end"]).replace(tzinfo=UTC).timestamp() * 1000 + 86_400_000
        if not all(
            lo <= f["t"] <= hi and f["qty"] > 0 and f["kind"] in ("entry", "exit")
            for f in one["fills"]
        ):
            return f"a fill lies outside {bt['start']}..{bt['end']} or is malformed: {one['fills'][:2]}"
        state["fills"] = one
        return None

    def market_symbols() -> str | None:
        code, syms = fetch(base, "/api/market/symbols?q=btcusdt")
        if code in (502, 503):
            state["market_offline"] = (
                True  # Binance unreachable from here: the live-market checks cannot run
            )
            return None
        if code != 200 or not syms or syms[0]["instrument"] != "BTCUSDT":
            return f"status {code}: {syms}"
        state["symbols"] = syms
        return None

    def market_candles() -> str | None:
        """Candles of the backtest's last day (local catalog or Binance) and indicators computed from them."""
        bt = state["backtests"][0]
        end_ms = int(datetime.fromisoformat(bt["end"]).replace(tzinfo=UTC).timestamp() * 1000) - 1
        q = f"instrument=BINANCE:BTCUSDT&tf=1h&end_ms={end_ms}&limit=24"
        code, cs = fetch(base, f"/api/market/candles?{q}")
        if code != 200 or len(cs) != 24:
            return f"status {code}, {len(cs or [])} candles"
        if not all(a["t"] < b["t"] and a["h"] >= a["l"] for a, b in itertools.pairwise(cs)):
            return "candles not ascending or high < low"
        code, ind = fetch(base, f"/api/market/indicators?{q}&spec=ema(5),rsi(5),bbands(5,2)")
        if (
            code != 200
            or set(ind) != {"ema(5)", "rsi(5)", "bbands(5,2)"}
            or len(ind["ema(5)"]) != 24
        ):
            return f"indicators status {code}: { {k: len(v) for k, v in (ind or {}).items()} }"
        state["candles"], state["indicators"] = cs, ind
        return None

    def market_errors() -> str | None:
        bad = {
            "/api/market/candles?instrument=BTCUSDT&tf=7m": 400,
            "/api/market/candles?instrument=BTCUSDT&tf=1h&limit=1001": 400,
            "/api/market/indicators?instrument=BTCUSDT&tf=1h&spec=nope(3)": 400,
        }
        got = {path: fetch(base, path)[0] for path in bad}
        return None if got == bad else f"expected {bad}, got {got}"

    def export() -> str | None:
        pk = state["public"]["pubkey"]
        code, text = fetch(base, f"/api/providers/{pk}/export?format=csv")
        rows = text.splitlines() if isinstance(text, str) else []
        if code != 200 or not rows or not rows[0].startswith("seq,id,created_ms,") or len(rows) < 2:
            return f"csv status {code}, {len(rows)} lines"
        code, bundle = fetch(base, f"/api/providers/{pk}/export?format=bundle")
        if code != 200 or bundle.get("provider") != pk or not bundle.get("events"):
            return f"bundle status {code}"
        path = Path(tempfile.mkdtemp(prefix="sa-export-")) / "bundle.json"
        path.write_text(json.dumps(bundle))
        done = subprocess.run(  # noqa: S603 - our own argv
            [sys.executable, "-m", "sa_cli", "verify", str(path)], capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        if done.returncode != 0 or "bundle is complete" not in done.stdout:
            return f"sa verify said (exit {done.returncode}): {done.stdout[-300:]} {done.stderr[-300:]}"
        state["export"] = (len(rows) - 1, len(bundle["events"]))
        return None

    def network() -> str | None:
        code, net = fetch(base, "/api/network")
        if (
            code != 200
            or not net["relays"]
            or not all(r["ok"] for r in net["relays"])
            or not net["chains"]
            or net["dev_views"] is not True
        ):
            return str(net)
        state["network"] = net
        return None

    def me() -> str | None:
        code, m = fetch(base, "/api/me")
        if code != 200 or m["token_power"] <= 0 or not m["allocations"]["providers"]:
            return str(m)
        state["me"] = m
        return None

    def governance() -> str | None:
        code, props = fetch(base, "/api/governance/proposals")
        if code != 200 or not props or not any(v > 0 for v in props[0]["tally"].values()):
            return str(props)
        state["proposal"] = props[0]
        return None

    def web() -> str | None:
        code, html = fetch(base, "/")
        return None if code == 200 and "Superalgos" in html else f"status {code}"

    def read_only() -> str | None:
        codes = {m: fetch(base, "/api/providers", m)[0] for m in ("POST", "PUT", "PATCH", "DELETE")}
        codes |= {
            f"{m} market": fetch(base, "/api/market/candles", m)[0] for m in ("POST", "DELETE")
        }
        if not all(c in (404, 405) for c in codes.values()):
            return f"write verbs answered {codes}"
        # the only POST routes are sign-in and the strategy designer's; an empty untyped request must never answer 2xx (SEP-1.8)
        auth = {
            p: fetch(base, p, "POST")[0]
            for p in (
                "/api/auth/logout",
                "/api/auth/verify",
                # the strategy designer's routes refuse a body without a JSON content type, whatever the bind
                "/api/strategy/validate",
                "/api/backtests/run",
            )
        }
        return (
            None if all(400 <= c < 500 for c in auth.values()) else f"auth routes answered {auth}"
        )

    # each check waits for the data it needs; the order is the order data appears
    plan: list[tuple[str, Callable[[], str | None], float, Callable[[], str]]] = [
        (
            "providers (public + private)",
            providers,
            DATA_TIMEOUT_S,
            lambda: (
                f"{state['public']['name']} / {state['private']['name']}, {state['public']['followers']} followers"
            ),
        ),
        (
            "provider signals: one status each, anchors",
            signals,
            DATA_TIMEOUT_S,
            lambda: (
                f"{len(state['signals'])} signals, statuses {sorted({s['status'] for s in state['signals']})}"
            ),
        ),
        (
            "dev views on: tier releases",
            dev_releases,
            DATA_TIMEOUT_S,
            lambda: f"{state['releases']} releases per signal",
        ),
        (
            "my signals (follower ledger)",
            my_signals,
            DATA_TIMEOUT_S,
            lambda: (
                f"{len(state['my_signals'])} received, {sorted({r['outcome'] for r in state['my_signals']})}"
            ),
        ),
        (
            "verified track record",
            record,
            DATA_TIMEOUT_S,
            lambda: (
                f"{len(state['record']['trades'])} trades, {len(state['record']['equity'])} equity points, {state['record']['verification']}"
            ),
        ),
        (
            "private provider revealed record",
            private_revealed,
            DATA_TIMEOUT_S,
            lambda: f"{len(state['private_record']['trades'])} trades",
        ),
        (
            "backtest",
            backtests,
            30,
            lambda: (
                f"{len(state['backtests'])} backtests, {state['backtests'][0]['trades']} trades"
            ),
        ),
        (
            "backtest fills for the chart",
            backtest_fills,
            30,
            lambda: (
                f"{len(state['fills']['fills'])} fills on {state['fills']['timeframe']} candles"
            ),
        ),
        (
            "market symbols (Binance)",
            market_symbols,
            30,
            lambda: (
                "SKIPPED: Binance is unreachable from here"
                if state.get("market_offline")
                else f"{len(state['symbols'])} match(es) for btcusdt"
            ),
        ),
        (
            "market candles and indicators",
            market_candles,
            60,
            lambda: f"{len(state['candles'])} candles, series {sorted(state['indicators'])}",
        ),
        ("market parameter errors", market_errors, 30, lambda: "bad tf, limit and spec are 400"),
        (
            "signal export (sa verify)",
            export,
            DATA_TIMEOUT_S,
            lambda: (
                f"{state['export'][0]} csv rows; bundle of {state['export'][1]} events verified complete"
            ),
        ),
        (
            "network status",
            network,
            30,
            lambda: (
                f"{len(state['network']['relays'])} relay(s) ok, chains {[c['name'] for c in state['network']['chains']]}"
            ),
        ),
        ("my power", me, 30, lambda: f"{state['me']['token_power']:g} SA"),
        (
            "governance tally",
            governance,
            30,
            lambda: f"{state['proposal']['title']!r}: {state['proposal']['tally']}",
        ),
        ("dashboard at /", web, 30, lambda: "index.html served"),
        (
            "no write endpoints",
            read_only,
            30,
            lambda: (
                "POST/PUT/PATCH/DELETE refused; sign-in and designer routes refuse unsigned/untyped requests"
            ),
        ),
    ]
    for name, check, timeout, detail in plan:
        why = eventually(check, timeout)
        report.add(why is None, name, detail() if why is None else why)


def main() -> int:
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="sa-demo-smoke-"))
    log = work / "demo.log"
    cmd = [
        sys.executable, "-m", "sa_cli", "demo", "--workdir", str(work / "demo"), "--port", str(port),
        "--days", "1", "--speed", "2880", "--lead", "15", "--duration", "400",
    ]  # fmt: skip
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    print(f"sa demo --days 1 --speed 2880 on 127.0.0.1:{port} (log {log})", flush=True)
    with log.open("wb") as fh:
        demo = subprocess.Popen(  # noqa: S603 - our own argv, no shell
            cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
    report = Report()
    base = f"http://127.0.0.1:{port}"
    try:
        started = time.monotonic()
        while "READY" not in log.read_text(errors="replace"):
            if demo.poll() is not None or time.monotonic() - started > READY_TIMEOUT_S:
                print(log.read_text(errors="replace")[-3000:])
                report.add(
                    False,
                    "demo start",
                    f"not ready after {time.monotonic() - started:.0f} s (exit {demo.poll()})",
                )
                return 1
            time.sleep(0.5)
        report.add(True, "demo start", f"READY after {time.monotonic() - started:.0f} s")
        run_checks(base, report)
    finally:
        if demo.poll() is None:
            demo.send_signal(signal.SIGINT)
        try:
            code = demo.wait(45)
        except subprocess.TimeoutExpired:
            demo.kill()
            code = demo.wait(10)
            report.add(False, "clean shutdown", "did not stop within 45 s of SIGINT")
        else:
            report.add(code == 0, "clean shutdown", f"exit code {code}")
    with socket.socket() as s:
        s.settimeout(0.3)
        report.add(s.connect_ex(("127.0.0.1", port)) != 0, "port released", f"{port} is closed")
    stopped = "everything the demo started is gone" in log.read_text(errors="replace")
    report.add(stopped, "children stopped", "demo reported all processes stopped")
    print("\nsmoke test " + ("PASSED" if report.ok else "FAILED"), flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
