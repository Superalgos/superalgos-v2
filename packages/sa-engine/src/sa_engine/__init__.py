"""Superalgos v2 engine: strategy schema + DSL, Nautilus compile, data, backtest, paper trading.

Pure modules (schema, dsl, intent, brain, sizing, report) import without Nautilus; the Nautilus-backed names
below are loaded lazily on first use.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from sa_engine.intent import OrderIntent
from sa_engine.schema import StrategyDoc, StrategyValidationError, json_schema, validate_strategy

if TYPE_CHECKING:
    from sa_engine.backtest import run_backtest as run_backtest
    from sa_engine.compile import compile_strategy as compile_strategy
    from sa_engine.paper import FollowerExecutor as FollowerExecutor
    from sa_engine.paper import run_paper as run_paper
    from sa_engine.report import BacktestReport as BacktestReport

__all__ = [
    "BacktestReport",
    "FollowerExecutor",
    "OrderIntent",
    "StrategyDoc",
    "StrategyValidationError",
    "compile_strategy",
    "json_schema",
    "run_backtest",
    "run_paper",
    "validate_strategy",
]

_LAZY = {
    "run_backtest": "sa_engine.backtest",
    "compile_strategy": "sa_engine.compile",
    "FollowerExecutor": "sa_engine.paper",
    "run_paper": "sa_engine.paper",
    "BacktestReport": "sa_engine.report",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'sa_engine' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
