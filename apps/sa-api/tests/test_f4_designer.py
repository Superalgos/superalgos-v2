"""F4 backend: POST /api/strategy/validate (pure) and the loopback-only, one-at-a-time, time-bounded
POST /api/backtests/run with GET /api/backtests/jobs/{id} (SEP-5.2, SEP-5.7 backend, SEP-5.8 backend)."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_api.app import create_app
from sa_api.config import ApiConfig
from sa_api.jobs import JobManager, is_loopback_host, request_host_is_loopback
from sa_engine.catalog import instrument_for, write_minute_bars
from sa_engine.intent import BACKTEST_SYMBOLS

EXAMPLES = Path(__file__).resolve().parents[3] / "packages" / "sa-engine" / "examples"
ALL_INTERFACES = ".".join(["0"] * 4)  # the wildcard bind address


def example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return data


def fast_doc() -> dict[str, Any]:
    """The ema crossover with short periods on 15m bars, so two synthetic days produce trades."""
    doc = example("ema_crossover.json")
    doc["timeframe"] = "15m"
    doc["stages"]["open"]["when"]["any"][0]["all"] = ["cross_above(ema(close, 5), ema(close, 13))"]
    doc["stages"]["close"]["when"]["any"][0]["all"] = ["cross_below(ema(close, 5), ema(close, 13))"]
    return doc


def make_data_dir(root: Path) -> Path:
    """A data dir whose catalog already holds two days of 1m bars for 2025-01-01..03 (no download)."""
    n = 2 * 1440
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC") + pd.Timedelta(minutes=1)
    t = np.arange(n)
    price = 30_000 + 400 * np.sin(2 * np.pi * t / 480) + 150 * np.sin(2 * np.pi * t / 97)
    frame = pd.DataFrame(
        {"open": price, "high": price + 5, "low": price - 5, "close": price, "volume": 1.0},
        index=idx,
    ).round(2)
    frame.index.name = "timestamp"
    catalog_dir = root / "catalog"
    write_minute_bars(ParquetDataCatalog(catalog_dir), "BTCUSDT", frame)
    (catalog_dir / ".converted").mkdir()
    (catalog_dir / ".converted" / "BTCUSDT-2025-01").write_text("ok\n")
    (catalog_dir / ".converted" / "BTCUSDT-2025-02").write_text(
        "ok\n"
    )  # converted but empty: a DataError, offline
    return root


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_data_dir(tmp_path_factory.mktemp("data"))


@pytest.fixture
def cfg(tmp_path: Path, data_dir: Path) -> ApiConfig:
    return ApiConfig(
        backtests_dir=tmp_path / "backtests", data_dir=data_dir, backtest_timeout_s=120
    )


def make_client(cfg: ApiConfig, bind_host: str | None = "127.0.0.1", **kw: Any) -> TestClient:
    return TestClient(create_app(cfg, bind_host=bind_host, **kw), base_url="http://localhost:8787")


@pytest.fixture
def local(cfg: ApiConfig) -> Iterator[TestClient]:
    with make_client(cfg) as c:
        yield c


def run_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "strategy": fast_doc(),
        "instrument": "BINANCE:BTCUSDT",
        "timeframe": "15m",
        "start": "2025-01-01",
        "end": "2025-01-03",
    }
    return body | over


def wait_for(
    client: TestClient, job_id: str, states: tuple[str, ...] = ("done", "failed", "timeout")
) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/backtests/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def slow_command(seconds: float) -> Callable[..., list[str]]:
    def command(*_: object) -> list[str]:
        return [sys.executable, "-c", f"import time; time.sleep({seconds})"]

    return command


# --------------------------------------------------------------------------- POST /api/strategy/validate


def test_validate_accepts_every_example(local: TestClient) -> None:
    for p in sorted(EXAMPLES.glob("*.json")):
        res = local.post(
            "/api/strategy/validate",
            content=p.read_text(),
            headers={"content-type": "application/json"},
        )
        assert res.status_code == 200 and res.json() == {"ok": True, "errors": []}, p.name


def test_validate_reports_path_column_and_message_for_every_problem(local: TestClient) -> None:
    doc = example("trend_phases.json")
    doc["stages"]["manage"]["stop"]["phases"][0]["level"] = "entry_price *"
    doc["stages"]["manage"]["target"]["phases"][1]["level"] = "nope * 2"
    doc["stages"]["open"]["when"]["any"][0]["all"][0] = "close >"
    doc["stages"]["open"]["orders"][0]["size_fraction"] = 7
    body = local.post("/api/strategy/validate", json=doc).json()
    assert body["ok"] is False
    assert all(set(e) == {"path", "column", "message"} for e in body["errors"])
    by_path = {e["path"]: e for e in body["errors"]}
    assert by_path["$.stages.manage.stop.phases[0].level"]["column"] == 13
    assert by_path["$.stages.manage.target.phases[1].level"] == {
        "path": "$.stages.manage.target.phases[1].level",
        "column": 0,
        "message": by_path["$.stages.manage.target.phases[1].level"]["message"],
    }
    assert "unknown name 'nope'" in by_path["$.stages.manage.target.phases[1].level"]["message"]
    assert by_path["$.stages.open.when.any[0].all[0]"]["column"] == 7
    assert (
        by_path["$.stages.open.orders[0].size_fraction"]["column"] is None
    )  # not inside an expression


def test_validate_handles_malformed_bodies_without_failing(local: TestClient) -> None:
    for raw, fragment in (
        (b"{nope", "not valid JSON"),
        (b"[1]", "expected a JSON object"),
        (b"3", "expected a JSON object"),
    ):
        res = local.post(
            "/api/strategy/validate", content=raw, headers={"content-type": "application/json"}
        )
        body = res.json()
        assert res.status_code == 200 and body["ok"] is False
        assert body["errors"][0]["path"] == "$" and body["errors"][0]["column"] is None
        assert fragment in body["errors"][0]["message"]
    old = example("ema_crossover.json") | {"schema_version": 1}
    assert (
        "Migrate" in local.post("/api/strategy/validate", json=old).json()["errors"][0]["message"]
    )


def test_validate_has_no_side_effects(
    cfg: ApiConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import threading

    monkeypatch.chdir(tmp_path)
    before = (
        sorted(p.name for p in tmp_path.rglob("*")),
        threading.active_count(),
        dict(os.environ),
    )
    with make_client(cfg) as c:
        for doc in (example("ladder_rearm.json"), {"name": 1}, example("trend_phases.json")):
            c.post("/api/strategy/validate", json=doc)
        # also from an API that is not bound to loopback: validation is allowed everywhere
        assert (
            make_client(cfg, ALL_INTERFACES)
            .post("/api/strategy/validate", json=example("grid_10_level.json"))
            .json()["ok"]
        )
    after = (
        sorted(p.name for p in tmp_path.rglob("*")),
        threading.active_count(),
        dict(os.environ),
    )
    assert after[0] == before[0] and after[2] == before[2]
    assert cfg.backtests_dir is not None and not cfg.backtests_dir.exists()


def test_validate_content_type_and_size_limits(local: TestClient) -> None:
    res = local.post(
        "/api/strategy/validate", content=b"{}", headers={"content-type": "text/plain"}
    )
    assert res.status_code == 415 and "application/json" in res.json()["error"]
    assert local.post("/api/strategy/validate", content=b"{}").status_code == 415
    big = local.post(
        "/api/strategy/validate",
        content=b" " * (300 * 1024),
        headers={"content-type": "application/json"},
    )
    assert big.status_code == 413 and "limit" in big.json()["error"]


def test_validate_is_rate_limited_per_client(cfg: ApiConfig) -> None:
    with make_client(cfg.model_copy(update={"market_rate_per_min": 3})) as c:
        codes = [
            c.post("/api/strategy/validate", json=example("grid_10_level.json")).status_code
            for _ in range(5)
        ]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


# --------------------------------------------------------------------------- POST /api/backtests/run: who may call


def test_run_is_refused_unless_the_api_is_bound_to_loopback(cfg: ApiConfig) -> None:
    for bind in (ALL_INTERFACES, "::", "192.168.1.20", "example.org", "", None):
        res = make_client(cfg, bind).post("/api/backtests/run", json=run_body())
        assert res.status_code == 403, bind
        assert "loopback" in res.json()["error"] and "--host 127.0.0.1" in res.json()["error"]
    assert cfg.backtests_dir is not None and not cfg.backtests_dir.exists()


def test_run_is_refused_through_a_proxy_a_foreign_host_or_a_foreign_origin(cfg: ApiConfig) -> None:
    c = make_client(cfg)
    assert (
        c.post(
            "/api/backtests/run", json=run_body(), headers={"x-forwarded-for": "203.0.113.9"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/backtests/run", json=run_body(), headers={"forwarded": "for=203.0.113.9"}
        ).status_code
        == 403
    )
    assert (
        make_client(cfg.model_copy(update={"proxy_headers": True}))
        .post("/api/backtests/run", json=run_body())
        .status_code
        == 403
    )
    rebind = TestClient(create_app(cfg, bind_host="127.0.0.1"), base_url="http://evil.example:8787")
    res = rebind.post("/api/backtests/run", json=run_body())
    assert res.status_code == 403 and "not a loopback name" in res.json()["error"]  # DNS rebinding
    cross = c.post("/api/backtests/run", json=run_body(), headers={"origin": "http://evil.example"})
    assert cross.status_code == 403 and "Origin" in cross.json()["error"]
    assert cfg.backtests_dir is not None and not cfg.backtests_dir.exists()


def test_loopback_helpers() -> None:
    assert all(
        is_loopback_host(h)
        for h in ("127.0.0.1", "127.9.9.9", "::1", "[::1]", "localhost", "LOCALHOST")
    )
    assert not any(
        is_loopback_host(h)
        for h in (None, "", ALL_INTERFACES, "::", "10.0.0.1", "example.org", "127.0.0.1.evil.io")
    )
    assert all(
        request_host_is_loopback(h)
        for h in ("localhost", "localhost:8787", "127.0.0.1:8787", "[::1]:8787", "[::1]")
    )
    assert not any(
        request_host_is_loopback(h)
        for h in (
            None,
            "",
            "evil.example",
            "evil.example:80",
            "localhost.evil.io",
            ALL_INTERFACES + ":8787",
        )
    )


# --------------------------------------------------------------------------- POST /api/backtests/run: the request


def test_run_rejects_a_bad_request_with_paths_and_starts_nothing(
    local: TestClient, cfg: ApiConfig
) -> None:
    doc = fast_doc()
    doc["stages"]["close"]["when"]["any"][0]["all"][0] = "close >"
    res = local.post(
        "/api/backtests/run",
        json=run_body(strategy=doc, instrument="BINANCE:DOGEUSDT", end="2025-01-01"),
    )
    assert res.status_code == 422
    body = res.json()
    assert "invalid backtest request" in body["error"]
    paths = {e["path"]: e for e in body["errors"]}
    assert paths["$.stages.close.when.any[0].all[0]"]["column"] == 7  # the strategy's own JSON path
    assert (
        "DOGEUSDT" in paths["request.instrument"]["message"]
        and "BTCUSDT" in paths["request.instrument"]["message"]
    )
    assert "must be after start" in paths["request.end"]["message"]
    assert cfg.backtests_dir is not None and not cfg.backtests_dir.exists()


@pytest.mark.parametrize(
    ("over", "path", "fragment"),
    [
        ({"timeframe": "1h"}, "request.timeframe", "differs from the strategy's '15m'"),
        ({"start": "soon"}, "request.start", "not an ISO date"),
        ({"end": "2030-01-01", "start": "2029-12-01"}, "request.end", "in the future"),
        ({"start": "2020-01-01", "end": "2025-06-01"}, "request.end", "backtest_max_days"),
        ({"instrument": "nonsense!"}, "request.instrument", "VENUE:SYMBOL"),
        ({"instrument": "BYBIT:BTCUSDT"}, "request.instrument", "cannot be backtested"),
    ],
)
def test_run_checks_each_request_field(
    local: TestClient, over: dict[str, Any], path: str, fragment: str
) -> None:
    res = local.post("/api/backtests/run", json=run_body(**over))
    assert res.status_code == 422
    found = {e["path"]: e["message"] for e in res.json()["errors"]}
    assert fragment in found[path], found


def test_run_malformed_bodies(local: TestClient) -> None:
    assert (
        local.post(
            "/api/backtests/run", content=b"{}", headers={"content-type": "text/plain"}
        ).status_code
        == 415
    )
    res = local.post("/api/backtests/run", json={"strategy": {}, "instrument": "BTCUSDT"})
    assert (
        res.status_code == 400
        and "bad request at" in res.json()["error"]
        and "start" in res.json()["error"]
    )
    res = local.post(
        "/api/backtests/run", content=b"nope", headers={"content-type": "application/json"}
    )
    assert res.status_code == 400
    res = local.post("/api/backtests/run", json=run_body(start=5))
    assert res.status_code == 400 and "bad request at start" in res.json()["error"]
    res = local.post("/api/backtests/run", json=run_body(extra=1))
    assert res.status_code == 400 and "extra" in res.json()["error"]


def test_run_needs_somewhere_to_put_the_report(data_dir: Path) -> None:
    with make_client(ApiConfig(data_dir=data_dir)) as c:
        res = c.post("/api/backtests/run", json=run_body())
    assert res.status_code == 422 and res.json()["errors"][0]["path"] == "config.backtests_dir"


def test_backtest_symbols_match_the_engines_instruments() -> None:
    for symbol in BACKTEST_SYMBOLS:
        assert instrument_for(symbol)
    with pytest.raises(ValueError, match="no built-in instrument"):
        instrument_for("DOGEUSDT")


# --------------------------------------------------------------------------- the job: success, lists, fills


def test_a_backtest_job_runs_and_its_report_appears_with_fills_in_the_list(
    local: TestClient, cfg: ApiConfig
) -> None:
    res = local.post("/api/backtests/run", json=run_body())
    assert res.status_code == 202
    started = res.json()
    assert (
        started["state"] == "running"
        and started["id"].startswith("bt-")
        and started["backtest_id"] is None
    )
    assert set(started) == {"id", "state", "started_ms", "finished_ms", "backtest_id", "error"}
    job = wait_for(local, started["id"])
    assert job["state"] == "done" and job["backtest_id"] == started["id"] and job["error"] is None
    assert job["finished_ms"] is not None and job["finished_ms"] >= job["started_ms"]
    listed = {b["id"]: b for b in local.get("/api/backtests").json()}
    mine = listed[started["id"]]
    assert mine["strategy_name"] == "ema-crossover" and mine["instrument"] == "BINANCE:BTCUSDT"
    assert (
        mine["timeframe"] == "15m" and mine["start"] == "2025-01-01" and mine["end"] == "2025-01-03"
    )
    assert len(mine["fills"]) > 2 and {f["kind"] for f in mine["fills"]} == {"entry", "exit"}
    assert local.get(f"/api/backtests/{started['id']}").json() == mine
    assert cfg.backtests_dir is not None
    assert [p.name for p in cfg.backtests_dir.iterdir()] == [
        f"{started['id']}.json"
    ]  # no temp file left behind


def test_the_report_equals_a_direct_backtest(
    local: TestClient, cfg: ApiConfig, data_dir: Path
) -> None:
    from sa_engine.backtest import run_backtest
    from sa_engine.report import BacktestReport

    job = wait_for(local, local.post("/api/backtests/run", json=run_body()).json()["id"])
    assert cfg.backtests_dir is not None
    stored = BacktestReport.model_validate_json(
        (cfg.backtests_dir / f"{job['backtest_id']}.json").read_text()
    )
    direct = run_backtest(
        fast_doc(), "BINANCE:BTCUSDT", "2025-01-01", "2025-01-03", data_dir=data_dir, offline=True
    )
    assert stored.fingerprint() == direct.fingerprint()


def test_unknown_job_is_a_404_that_names_it(local: TestClient) -> None:
    res = local.get("/api/backtests/jobs/bt-nope")
    assert res.status_code == 404 and "backtest job not found: bt-nope" in res.json()["error"]


# --------------------------------------------------------------------------- one at a time, bounded time


def test_only_one_job_runs_at_a_time(cfg: ApiConfig) -> None:
    manager = JobManager(cfg, command=slow_command(2.0))
    with make_client(cfg, jobs=manager) as c:
        first = c.post("/api/backtests/run", json=run_body())
        assert first.status_code == 202
        second = c.post("/api/backtests/run", json=run_body())
        assert second.status_code == 409
        assert (
            second.json()["job"] == first.json()["id"] and "still running" in second.json()["error"]
        )
        manager.wait(30)
        # the sleeping stand-in prints nothing, so it fails to parse a report, but the slot is free again
        assert c.get(f"/api/backtests/jobs/{first.json()['id']}").json()["state"] == "failed"
        again = c.post("/api/backtests/run", json=run_body())
        assert again.status_code == 202
        manager.wait(30)


def test_a_job_over_its_time_budget_is_killed_and_reported(cfg: ApiConfig) -> None:
    manager = JobManager(
        cfg.model_copy(update={"backtest_timeout_s": 0.5}), command=slow_command(30)
    )
    with make_client(cfg, jobs=manager) as c:
        started = time.monotonic()
        res = c.post("/api/backtests/run", json=run_body())
        manager.wait(15)
        job = c.get(f"/api/backtests/jobs/{res.json()['id']}").json()
        assert (
            job["state"] == "timeout"
            and "killed after 0.5 s" in job["error"]
            and "backtest_timeout_s" in job["error"]
        )
        assert time.monotonic() - started < 10  # killed, not waited for
        assert (
            c.post("/api/backtests/run", json=run_body()).status_code == 202
        )  # the slot is free again
        manager.wait(15)
    assert cfg.backtests_dir is not None and not list(cfg.backtests_dir.glob("*.json"))


def test_a_failing_job_reports_why(cfg: ApiConfig) -> None:
    def fail(*_: object) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import sys; print('data missing for 2025-01', file=sys.stderr); sys.exit(3)",
        ]

    manager = JobManager(cfg, command=fail)
    with make_client(cfg, jobs=manager) as c:
        job_id = c.post("/api/backtests/run", json=run_body()).json()["id"]
        manager.wait(15)
        job = c.get(f"/api/backtests/jobs/{job_id}").json()
    assert job["state"] == "failed" and job["backtest_id"] is None
    assert "code 3" in job["error"] and "data missing for 2025-01" in job["error"]


def test_a_real_engine_error_is_surfaced(local: TestClient) -> None:
    # February is marked converted but holds no bars: the engine says so, offline
    job = wait_for(
        local,
        local.post(
            "/api/backtests/run", json=run_body(start="2025-02-01", end="2025-02-03")
        ).json()["id"],
    )
    assert job["state"] == "failed" and job["backtest_id"] is None
    assert "no 1m bars for BINANCE:BTCUSDT" in job["error"]


# --------------------------------------------------------------------------- the route table


def test_the_only_write_routes_are_auth_validate_and_run(cfg: ApiConfig) -> None:
    app = create_app(cfg)
    writes = {
        (m, r.path)  # type: ignore[attr-defined]
        for r in app.routes
        for m in getattr(r, "methods", set())
        if m not in {"GET", "HEAD"}
    }
    assert writes == {
        ("POST", "/api/auth/nonce"),
        ("POST", "/api/auth/verify"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/strategy/validate"),
        ("POST", "/api/backtests/run"),
    }
    c = TestClient(app, base_url="http://localhost:8787")
    for path in ("/api/strategy/validate", "/api/backtests/run"):
        for verb in ("put", "patch", "delete"):
            assert getattr(c, verb)(path).status_code == 405, f"{verb} {path}"
    assert c.get("/api/strategy/validate").status_code in (404, 405)  # reading it is not a thing
    assert c.post("/api/backtests/jobs/x").status_code in (404, 405)
