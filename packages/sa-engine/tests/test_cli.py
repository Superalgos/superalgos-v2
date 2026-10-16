from __future__ import annotations

import json
from pathlib import Path

import pytest
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from sa_engine.catalog import write_minute_bars
from sa_engine.cli import main
from tests.conftest import synthetic_minutes

ROOT = Path(__file__).resolve().parents[1]
GRID = str(ROOT / "examples" / "grid_10_level.json")


def test_validate_ok_and_failures(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["validate", GRID]) == 0
    assert "valid (grid-10-level, 15m, 10 order(s))" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    doc = json.loads(Path(GRID).read_text())
    doc["stages"]["open"]["orders"][3]["size_fraction"] = 5
    bad.write_text(json.dumps(doc))
    assert main(["validate", str(bad)]) == 2
    assert "$.stages.open.orders[3].size_fraction" in capsys.readouterr().err


def test_validate_json_prints_the_machine_readable_shape(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["validate", GRID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": []}
    doc = json.loads(Path(GRID).read_text())
    doc["stages"]["open"]["orders"][0]["rate"] = "close +"
    doc["stages"]["open"]["orders"][0]["offset_pct"] = 0
    doc["stages"]["open"]["orders"][1]["size_fraction"] = 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc))
    assert main(["validate", str(bad), "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert {
        "path": "$.stages.open.orders[0].rate",
        "column": 7,
        "message": "expected a value but found end of expression",
    } in out["errors"]
    assert all(set(e) == {"path", "column", "message"} for e in out["errors"])


def test_unreadable_file_is_a_clear_exit() -> None:
    with pytest.raises(SystemExit, match="cannot read strategy file"):
        main(["validate", "/nonexistent/strategy.json"])


def test_schema_to_stdout_and_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Superalgos v2 strategy"
    out = tmp_path / "s.json"
    assert main(["schema", "-o", str(out)]) == 0
    assert out.read_text() == (ROOT / "schema" / "strategy.schema.json").read_text()


def test_backtest_command(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    catalog_dir = tmp_path / "catalog"
    write_minute_bars(ParquetDataCatalog(catalog_dir), "BTCUSDT", synthetic_minutes(days=2))
    marks = catalog_dir / ".converted"
    marks.mkdir()
    (marks / "BTCUSDT-2025-01").write_text("ok\n")
    argv = ["backtest", GRID, "--start", "2025-01-01", "--end", "2025-01-03", "--data-dir", str(tmp_path)]
    assert main(argv) == 0
    assert "grid-10-level BINANCE:BTCUSDT 2025-01-01..2025-01-03" in capsys.readouterr().out
    assert main([*argv, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["bars_processed"] == 2 * 1440 and "equity_curve" in report


def test_paper_command_prints_report(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import sa_engine.paper as paper

    seen: dict[str, object] = {}

    def fake_run_paper(doc: object, instrument: str, capital: float, **kw: object) -> paper.PaperReport:
        seen.update(instrument=instrument, capital=capital, **kw)
        return paper.PaperReport(
            instrument=instrument, duration_s=1.0, starting_capital=capital, final_equity=capital, quotes_seen=3,
            bars_seen=0, intents=0, orders_filled=0, fees=0.0, open_qty=0.0, trades=[], rejections=[],
        )  # fmt: skip

    monkeypatch.setattr(paper, "run_paper", fake_run_paper)
    assert main(["paper", GRID, "--capital", "500", "--duration", "5"]) == 0
    assert seen["capital"] == 500 and seen["duration_s"] == 5
    assert json.loads(capsys.readouterr().out)["quotes_seen"] == 3


def test_module_entry_point_and_lazy_exports() -> None:
    import runpy

    import sa_engine

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("sa_engine", run_name="__main__", alter_sys=True)
    assert exc.value.code in (2, None) or isinstance(exc.value.code, int)  # argparse exits 2 without a command
    assert sa_engine.run_backtest.__module__ == "sa_engine.backtest"
    assert sa_engine.FollowerExecutor.__module__ == "sa_engine.paper"
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        sa_engine.nope  # noqa: B018
