"""sa backtest and sa paper are thin wrappers; the engine itself is tested in sa-engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import sa_engine.backtest as backtest_module
import sa_engine.paper as paper_module
from cli_helpers import strategy_file
from cli_support import Cli
from sa_engine.data import DataError


class FakeReport:
    def summary(self) -> str:
        return "summary line"

    def model_dump_json(self, indent: int | None = None) -> str:
        return '{"pnl": 1}'


class FakePaper:
    def model_dump(self, exclude: set[str] | None = None) -> dict[str, Any]:
        return {"intents": 2}


def test_backtest_prints_the_summary_or_json(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(doc: Any, instrument: str, start: str, end: str, **kw: Any) -> FakeReport:
        seen.update(instrument=instrument, start=start, end=end, **kw)
        return FakeReport()

    monkeypatch.setattr(backtest_module, "run_backtest", fake)
    strategy = strategy_file(tmp_path)
    assert (
        cli.ok("backtest", strategy, "--start", "2026-09-01", "--end", "2026-09-08").strip()
        == "summary line"
    )
    assert seen["start"] == "2026-09-01" and seen["starting_capital"] == 10000.0
    assert json.loads(cli.ok("backtest", strategy, "--start", "a", "--end", "b", "--json")) == {
        "pnl": 1
    }


def test_backtest_output_writes_the_full_report(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backtest_module, "run_backtest", lambda *a, **k: FakeReport())
    out = tmp_path / "reports" / "r.json"
    out.parent.mkdir()
    cli.ok("backtest", strategy_file(tmp_path), "--start", "a", "--end", "b", "-o", out)
    assert json.loads(out.read_text()) == {"pnl": 1}
    err = cli.fail(
        "backtest",
        strategy_file(tmp_path),
        "--start",
        "a",
        "--end",
        "b",
        "-o",
        tmp_path / "no" / "r.json",
    )
    assert "cannot write the report" in err and "pick a writable path" in err


def test_backtest_data_errors_are_explained(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> None:
        raise DataError("GET failed: check the network")

    monkeypatch.setattr(backtest_module, "run_backtest", broken)
    assert "check the network" in cli.fail(
        "backtest", strategy_file(tmp_path), "--start", "2026-09-01", "--end", "2026-09-02"
    )


def test_paper_prints_the_report_and_explains_failures(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper_module, "run_paper", lambda *a, **k: FakePaper())
    assert json.loads(cli.ok("paper", strategy_file(tmp_path), "--duration", "1")) == {"intents": 2}

    def down(*a: Any, **k: Any) -> None:
        raise TimeoutError("paper node did not start")

    monkeypatch.setattr(paper_module, "run_paper", down)
    assert "paper trading failed" in cli.fail("paper", strategy_file(tmp_path))
