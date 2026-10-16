"""Backtest jobs for the strategy designer: one at a time, bounded in time, run in a child process.

``POST /api/backtests/run`` validates the request, then ``JobManager`` runs ``python -m sa_engine backtest`` for the
strategy in a subprocess, so a runaway job can be killed at ``backtest_timeout_s`` and the API process never imports
Nautilus. A finished report is written atomically to ``backtests_dir/<job id>.json``: that is all it takes for it to
appear in ``GET /api/backtests`` (with its fills, for the chart overlay).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from sa_api.config import ApiConfig
from sa_engine.data import default_data_dir
from sa_engine.intent import BACKTEST_SYMBOLS, normalize_instrument
from sa_engine.report import BacktestReport
from sa_engine.schema import Issue, StrategyDoc, validate_issues

log = logging.getLogger(__name__)

JobState = Literal["running", "done", "failed", "timeout"]
MAX_JOBS_KEPT: Final = 50
ERROR_TAIL: Final = 600

Command = Callable[[Path, str, str, str, ApiConfig, Path], Sequence[str]]


class JobBusy(Exception):
    """A backtest is already running (one at a time); ``job_id`` is the running one."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"backtest job {job_id} is still running; poll GET /api/backtests/jobs/{job_id} and retry when it finishes"
        )
        self.job_id = job_id


class RequestInvalid(Exception):
    """The run request is wrong; ``issues`` say where (strategy issues keep the document's own JSON paths)."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues))


@dataclass(slots=True)
class Job:
    id: str
    state: JobState
    started_ms: int
    finished_ms: int | None = None
    backtest_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunRequest:
    doc: StrategyDoc
    instrument: str
    start: datetime
    end: datetime


def _parse_day(value: object, field: str) -> tuple[datetime | None, Issue | None]:
    if not isinstance(value, str):
        return None, Issue(
            path=f"request.{field}",
            message=f"{field} must be a date string like 2025-01-31, got {value!r}",
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, Issue(
            path=f"request.{field}",
            message=f"{field} {value[:30]!r} is not an ISO date like 2025-01-31",
        )
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), None


def parse_request(body: dict[str, Any], cfg: ApiConfig) -> RunRequest:
    """Check the body ``{strategy, instrument, timeframe?, start, end}``; raise :class:`RequestInvalid` listing every problem."""
    issues: list[Issue] = []
    unknown = sorted(set(body) - {"strategy", "instrument", "timeframe", "start", "end"})
    issues += [
        Issue(
            path=f"request.{k}",
            message="unknown field; send strategy, instrument, timeframe, start, end",
        )
        for k in unknown
    ]
    doc: StrategyDoc | None = None
    if "strategy" not in body:
        issues.append(
            Issue(
                path="request.strategy", message="required field is missing: the strategy document"
            )
        )
    else:
        doc, strategy_issues = validate_issues(
            body["strategy"] if isinstance(body["strategy"], dict) else json.dumps(body["strategy"])
        )
        issues += strategy_issues
    instrument = ""
    try:
        instrument = normalize_instrument(str(body.get("instrument", "")))
        venue, _, symbol = instrument.partition(":")
        if venue != "BINANCE" or symbol not in BACKTEST_SYMBOLS:
            issues.append(
                Issue(
                    path="request.instrument",
                    message=f"{instrument} cannot be backtested; supported: "
                    + ", ".join(f"BINANCE:{s}" for s in BACKTEST_SYMBOLS),
                )
            )
    except ValueError as exc:
        issues.append(Issue(path="request.instrument", message=f"{exc}; send e.g. BINANCE:BTCUSDT"))
    timeframe = body.get("timeframe")
    if timeframe is not None and doc is not None and timeframe != doc.timeframe:
        issues.append(
            Issue(
                path="request.timeframe",
                message=f"timeframe {timeframe!r} differs from the strategy's {doc.timeframe!r}; the strategy document decides, omit it or change the document",
            )
        )
    start, e1 = _parse_day(body.get("start"), "start")
    end, e2 = _parse_day(body.get("end"), "end")
    issues += [e for e in (e1, e2) if e is not None]
    if start is not None and end is not None:
        if end <= start:
            issues.append(
                Issue(
                    path="request.end",
                    message=f"end {end:%Y-%m-%d} must be after start {start:%Y-%m-%d}",
                )
            )
        elif (end - start).days > cfg.backtest_max_days:
            issues.append(
                Issue(
                    path="request.end",
                    message=f"window of {(end - start).days} days is longer than backtest_max_days={cfg.backtest_max_days}; shorten it",
                )
            )
        elif end > datetime.now(UTC):
            issues.append(
                Issue(
                    path="request.end",
                    message=f"end {end:%Y-%m-%d} is in the future; there is no data yet",
                )
            )
    if issues or doc is None or start is None or end is None:
        raise RequestInvalid(issues)
    return RunRequest(doc, instrument, start, end)


def default_command(
    strategy_file: Path, instrument: str, start: str, end: str, cfg: ApiConfig, data_dir: Path
) -> list[str]:
    return [
        sys.executable, "-m", "sa_engine", "backtest", str(strategy_file),
        "--instrument", instrument, "--start", start, "--end", end,
        "--capital", repr(cfg.capital), "--data-dir", str(data_dir), "--json",
    ]  # fmt: skip


class JobManager:
    def __init__(self, cfg: ApiConfig, command: Command = default_command) -> None:
        self.cfg = cfg
        self._command = command
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._running: str | None = None
        self._threads: list[threading.Thread] = []

    def _out_dir(self) -> Path:
        folder = self.cfg.backtests_dir
        if folder is None:
            raise RequestInvalid(
                [
                    Issue(
                        path="config.backtests_dir",
                        message="not configured: the report has nowhere to go; set backtests_dir in the API config",
                    )
                ]
            )
        return Path(folder)

    def submit(self, req: RunRequest) -> Job:
        out_dir = self._out_dir()
        with self._lock:
            if self._running is not None:
                raise JobBusy(self._running)
            job = Job(
                id=f"bt-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}",
                state="running",
                started_ms=int(time.time() * 1000),
            )
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_JOBS_KEPT:
                self._jobs.popitem(last=False)
            self._running = job.id
        thread = threading.Thread(
            target=self._run, args=(job, req, out_dir), name=job.id, daemon=True
        )
        self._threads.append(thread)
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else replace(job)

    def wait(self, timeout_s: float = 60.0) -> None:
        """Block until the running job (if any) is over. For tests and shutdown."""
        for t in list(self._threads):
            t.join(timeout_s)

    # ------------------------------------------------------------ the job
    def _finish(
        self, job: Job, state: JobState, *, error: str | None = None, backtest_id: str | None = None
    ) -> None:
        with self._lock:
            job.state, job.error, job.backtest_id = state, error, backtest_id
            job.finished_ms = int(time.time() * 1000)
            self._running = None

    def _run(self, job: Job, req: RunRequest, out_dir: Path) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="sa-api-bt-") as tmp:
                strategy_file = Path(tmp) / "strategy.json"
                strategy_file.write_text(req.doc.model_dump_json(), encoding="utf-8")
                argv = self._command(
                    strategy_file,
                    req.instrument,
                    f"{req.start:%Y-%m-%d}",
                    f"{req.end:%Y-%m-%d}",
                    self.cfg,
                    self._data_dir(),
                )
                proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, nothing from the request but validated values
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=_child_env(),
                )
                try:
                    stdout, stderr = proc.communicate(timeout=self.cfg.backtest_timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    self._finish(
                        job, "timeout",
                        error=f"killed after {self.cfg.backtest_timeout_s:g} s (backtest_timeout_s); use a shorter window or a coarser timeframe",
                    )  # fmt: skip
                    return
            if proc.returncode != 0:
                tail = (stderr or stdout).strip()[-ERROR_TAIL:]
                self._finish(
                    job, "failed", error=f"backtest exited with code {proc.returncode}: {tail}"
                )
                return
            report = BacktestReport.model_validate_json(stdout)
            self._write(out_dir, job.id, report)
            self._finish(job, "done", backtest_id=job.id)
        except Exception as exc:
            log.exception("backtest job %s failed", job.id)
            self._finish(job, "failed", error=f"{type(exc).__name__}: {str(exc)[:ERROR_TAIL]}")

    def _data_dir(self) -> Path:
        return Path(self.cfg.data_dir) if self.cfg.data_dir else default_data_dir()

    @staticmethod
    def _write(folder: Path, job_id: str, report: BacktestReport) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"{job_id}.json"
        tmp = (
            folder / f".{job_id}.json.tmp"
        )  # a dot file: the listing globs *.json and never sees a half-written report
        tmp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(final)


def _child_env() -> dict[str, str]:
    keep = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "SA_DATA_DIR",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "TMPDIR",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )
    return {k: v for k, v in os.environ.items() if k in keep}


def is_loopback_host(host: str | None) -> bool:
    """True for ``localhost`` and loopback addresses (``127.0.0.0/8``, ``::1``); False for ``0.0.0.0``, ``::``, names, None."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def request_host_is_loopback(host_header: str | None) -> bool:
    """The ``Host`` header names this machine (``localhost:8787``, ``127.0.0.1``, ``[::1]:8787``); DNS rebinding fails here."""
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else host
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return is_loopback_host(host)
