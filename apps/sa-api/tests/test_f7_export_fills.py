"""F7: backtest fills from the stored report (SEP-8.5 backend half) and signal export (SEP-8.6)."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from sa_api.config import ApiConfig
from sa_api.service import CSV_COLUMNS, fills_of, iso_ms
from sa_core.bundle import bundle_from_json, check_bundle, export_bundle, write_bundle
from sa_core.models import Reveal
from sa_engine.report import ReportFill
from world import World, make_report

ENGINE_EXAMPLES = Path(__file__).resolve().parents[3] / "packages" / "sa-engine" / "examples"

# ---------------------------------------------------------------- backtest fills


def put_report(config: ApiConfig, name: str = "ema-week", **kw: Any) -> Any:
    assert config.backtests_dir is not None
    config.backtests_dir.mkdir(exist_ok=True)
    report = make_report(**kw)
    (config.backtests_dir / f"{name}.json").write_text(report.model_dump_json())
    return report


def test_backtest_list_and_detail_carry_the_reports_fills_instrument_and_timeframe(
    client: TestClient, config: ApiConfig
) -> None:
    report = put_report(config)
    detail = client.get("/api/backtests/ema-week").json()
    (listed,) = client.get("/api/backtests").json()
    assert detail == listed
    assert (
        (detail["instrument"], detail["timeframe"])
        == (report.instrument, report.timeframe)
        == ("BINANCE:BTCUSDT", "1h")
    )
    assert [(f["t"], f["side"], f["price"], f["qty"]) for f in detail["fills"]] == [
        (f.t, f.side, f.price, f.qty) for f in report.fills
    ]
    assert [f["kind"] for f in detail["fills"]] == ["entry", "exit", "entry", "exit", "exit"]
    assert [f["t"] for f in detail["fills"]] == sorted(f["t"] for f in detail["fills"])
    assert set(detail["fills"][0]) == {"t", "side", "price", "qty", "kind"}


def test_a_report_stored_before_fills_existed_still_serves_with_none(
    client: TestClient, config: ApiConfig
) -> None:
    assert config.backtests_dir is not None
    config.backtests_dir.mkdir()
    old = json.loads(make_report().model_dump_json())
    del old["fills"]
    (config.backtests_dir / "old.json").write_text(json.dumps(old))
    assert client.get("/api/backtests/old").json()["fills"] == []


def fill(t: int, side: str, qty: float) -> ReportFill:
    return ReportFill(t=t, side=side, price=1.0, qty=qty)


def test_fill_kind_walks_the_signed_position() -> None:
    def kinds(*fs: ReportFill) -> list[str]:
        return [f.kind for f in fills_of(make_report(fills=list(fs)))]

    assert kinds(fill(1, "buy", 1), fill(2, "buy", 1), fill(3, "sell", 2)) == [
        "entry",
        "entry",
        "exit",
    ]
    assert kinds(fill(1, "buy", 1), fill(2, "sell", 3)) == [
        "entry",
        "entry",
    ]  # crosses zero: a new (short) entry
    assert kinds(fill(1, "sell", 1), fill(2, "buy", 1)) == ["entry", "exit"]
    assert kinds(
        fill(1, "buy", 0.1), fill(2, "buy", 0.2), fill(3, "sell", 0.30000000000000004)
    ) == ["entry", "entry", "exit"]
    assert kinds() == []


def test_the_real_engine_report_round_trips_into_the_api(
    client: TestClient, config: ApiConfig, tmp_path: Path
) -> None:
    """`sa backtest -o` writes BacktestReport.model_dump_json(): a report with real engine fills is served unchanged."""
    from datetime import UTC, datetime

    import numpy as np
    import pandas as pd
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    from sa_engine.backtest import run_backtest
    from sa_engine.catalog import write_minute_bars
    from sa_engine.schema import validate_strategy

    n = 3 * 1440
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC") + pd.Timedelta(minutes=1)
    wave = (
        30_000
        + 400 * np.sin(2 * np.pi * np.arange(n) / 480)
        + 150 * np.sin(2 * np.pi * np.arange(n) / 97)
    )
    price = pd.Series(wave, index=idx)
    frame = pd.DataFrame(
        {"open": price, "high": price + 5, "low": price - 5, "close": price, "volume": 1.0}
    ).round(2)
    frame.index.name = "timestamp"
    catalog = ParquetDataCatalog(tmp_path / "cat")
    write_minute_bars(catalog, "BTCUSDT", frame)
    doc = json.loads((ENGINE_EXAMPLES / "ema_crossover.json").read_text())
    doc["timeframe"] = "15m"
    doc["stages"]["open"]["when"]["any"][0]["all"] = ["cross_above(ema(close, 5), ema(close, 13))"]
    doc["stages"]["close"]["when"]["any"][0]["all"] = ["cross_below(ema(close, 5), ema(close, 13))"]
    report = run_backtest(
        validate_strategy(doc), "BINANCE:BTCUSDT", "2025-01-01", "2025-01-04", catalog=catalog
    )
    assert report.fills, "the strategy should have traded"
    put_report_text = report.model_dump_json()
    assert config.backtests_dir is not None
    config.backtests_dir.mkdir()
    (config.backtests_dir / "real.json").write_text(put_report_text)
    served = client.get("/api/backtests/real").json()
    assert [(f["t"], f["side"], f["price"], f["qty"]) for f in served["fills"]] == [
        (f.t, f.side, f.price, f.qty) for f in report.fills
    ]
    assert (
        served["timeframe"] == report.timeframe == "15m"
        and served["instrument"] == "BINANCE:BTCUSDT"
    )
    assert all(
        report.start.timestamp() * 1000 <= f["t"] <= report.end.timestamp() * 1000 + 60_000
        for f in served["fills"]
    )
    assert isinstance(report.start, datetime) and report.start.tzinfo is UTC


# ---------------------------------------------------------------- export


def reveal_private(world: World, config: ApiConfig) -> None:
    assert config.bundles_dir is not None
    config.bundles_dir.mkdir(exist_ok=True)
    commits = [e for e in world.events if e.id in world.private_ids]
    reveals = [Reveal(commitment_id=r.commitment_id, plaintext=r.plaintext) for r in world.reveals]
    write_bundle(
        config.bundles_dir / "fox.json", export_bundle(world.priv.pubkey, commits, [], reveals)
    )


def sa_verify(path: Path, tsa_config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sa_cli", "verify", str(path), "--tsa-config", str(tsa_config)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_exported_bundle_passes_sa_verify_with_signatures_and_chain_complete(
    client: TestClient, world: World, farm_config: Path, tmp_path: Path
) -> None:
    """SEP-8.6: export, then `sa verify` says complete."""
    res = client.get(f"/api/providers/{world.pub.pubkey}/export", params={"format": "bundle"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.headers["content-disposition"].startswith("attachment; filename=")
    assert world.pub.pubkey[:12] in res.headers["content-disposition"]
    path = tmp_path / "exported.json"
    path.write_bytes(res.content)
    out = sa_verify(path, farm_config)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "bundle is complete" in out.stdout
    assert "problem" not in out.stdout and "gap" not in out.stdout
    check = check_bundle(bundle_from_json(res.text))
    assert check.complete and check.problems == () and len(check.items) == 4
    assert {i.event.id for i in check.items} == set(world.public_ids)
    assert "4 chain items, 4 anchored" in out.stdout


def test_default_format_is_the_bundle_and_the_bundle_is_byte_stable(
    client: TestClient, world: World
) -> None:
    a = client.get(f"/api/providers/{world.pub.pubkey}/export")
    b = client.get(f"/api/providers/{world.pub.pubkey}/export")
    assert a.headers["content-type"].startswith("application/json")
    assert json.loads(a.text)["events"] == json.loads(b.text)["events"]
    assert json.loads(a.text)["provider"] == world.pub.pubkey


def test_a_tampered_export_fails_sa_verify(
    client: TestClient, world: World, farm_config: Path, tmp_path: Path
) -> None:
    doc = json.loads(client.get(f"/api/providers/{world.pub.pubkey}/export").text)
    doc["events"].pop(1)  # an omitted signal
    path = tmp_path / "cut.json"
    path.write_text(json.dumps(doc))
    out = sa_verify(path, farm_config)
    assert out.returncode == 1 and "NOT complete" in out.stdout


def read_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]


def test_csv_export_has_one_row_per_signal_matching_the_signals_endpoint(
    client: TestClient, world: World
) -> None:
    res = client.get(f"/api/providers/{world.pub.pubkey}/export", params={"format": "csv"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/csv; charset=utf-8"
    assert res.headers["content-disposition"].startswith('attachment; filename="')
    assert res.headers["content-disposition"].endswith('-signals.csv"')
    header, rows = read_csv(res.text)
    assert tuple(header) == CSV_COLUMNS
    assert header[:10] == [  # the dashboard's documented columns, in its order
        "seq", "id", "created_ms", "created_iso", "anchored_ms", "instrument", "side", "action", "size_fraction",
        "private",
    ]  # fmt: skip
    assert "releases" not in header  # per-tier delivery is a dev view, not part of the export
    api = sorted(
        client.get(f"/api/providers/{world.pub.pubkey}/signals", params={"limit": 50}).json(),
        key=lambda s: s["seq"],
    )
    assert len(rows) == len(api) == 4
    for row, s in zip(rows, api, strict=True):
        assert (int(row["seq"]), row["id"], int(row["created_ms"])) == (
            s["seq"],
            s["id"],
            s["created_ms"],
        )
        assert row["anchored_ms"] == ("" if s["anchored_ms"] is None else str(s["anchored_ms"]))
        assert (row["instrument"], row["side"], row["action"], float(row["size_fraction"])) == (
            s["instrument"], s["side"], s["action"], s["size_fraction"],
        )  # fmt: skip
        assert row["private"] == "false" and row["created_iso"] == iso_ms(s["created_ms"])
        assert (row["order_type"], row["limit_price"], row["stop_price"], row["timeframe_s"]) == (
            "market",
            "",
            "",
            "3600",
        )


def test_csv_private_signals_export_only_what_the_api_shows(
    client: TestClient, world: World, config: ApiConfig
) -> None:
    url = f"/api/providers/{world.priv.pubkey}/export"
    assert client.get(url, params={"format": "csv"}).text.splitlines() == [
        ",".join(CSV_COLUMNS)
    ]  # nothing revealed: no rows
    plain = client.get(url, params={"format": "bundle"}).text
    assert "plaintext" not in plain and '"reveals":[]' in plain.replace(" ", "")
    for ev in world.reveals:
        assert (
            ev.plaintext not in plain
        )  # the API has no plaintext of a private signal until a reveal is supplied
    reveal_private(world, config)
    _, rows = read_csv(client.get(url, params={"format": "csv"}).text)
    shown = sorted(
        client.get(f"/api/providers/{world.priv.pubkey}/signals", params={"limit": 10}).json(),
        key=lambda s: s["seq"],
    )
    assert [(r["id"], r["private"], r["side"]) for r in rows] == [
        (s["id"], "true", s["side"]) for s in shown
    ] and len(rows) == 2
    bundle = bundle_from_json(client.get(url, params={"format": "bundle"}).text)
    assert {r.commitment_id for r in bundle.reveals} == {r.commitment_id for r in world.reveals}


def test_export_errors_name_the_problem(client: TestClient, world: World) -> None:
    bad = client.get(f"/api/providers/{world.pub.pubkey}/export", params={"format": "xml"})
    assert (
        bad.status_code == 400
        and "'xml'" in bad.json()["error"]
        and "format=bundle or format=csv" in bad.json()["error"]
    )
    missing = client.get("/api/providers/" + "0" * 64 + "/export")
    assert missing.status_code == 404 and "provider not found" in missing.json()["error"]


def test_export_is_get_only(client: TestClient, world: World) -> None:
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(client, verb)(f"/api/providers/{world.pub.pubkey}/export").status_code in (
            404,
            405,
        )


def test_iso_ms_has_milliseconds_and_z() -> None:
    assert iso_ms(1_791_882_000_123) == "2026-10-13T09:00:00.123Z"
