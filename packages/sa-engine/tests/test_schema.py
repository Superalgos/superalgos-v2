from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from hypothesis import given, settings

from sa_engine.schema import (
    Issue,
    StrategyDoc,
    StrategyValidationError,
    json_path,
    json_schema,
    schema_text,
    validate_issues,
    validate_strategy,
)
from tests.strategies import ALWAYS, ev, strategy_docs

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))
SCHEMA_FILE = ROOT / "schema" / "strategy.schema.json"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ROOT / "examples" / name).read_text())
    return data


def covers(full: Any, part: Any) -> bool:
    """``part`` is ``full`` with some defaulted keys left out (numbers compare by value)."""
    if isinstance(part, dict):
        return isinstance(full, dict) and all(k in full and covers(full[k], v) for k, v in part.items())
    if isinstance(part, list):
        return (
            isinstance(full, list)
            and len(full) == len(part)
            and all(covers(f, q) for f, q in zip(full, part, strict=True))
        )
    return bool(full == part)


def minimal() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "name": "t",
        "timeframe": "1h",
        "stages": {"open": {"when": ev(("s", ["close > 1"])), "orders": [{"id": "a", "size_fraction": 0.5}]}},
    }


def issues(doc: Any) -> dict[str, str]:
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy(doc)
    return {i.path: str(i) for i in exc.value.issues}


# --------------------------------------------------------------------------- valid documents


def test_five_examples_exist_and_validate() -> None:
    assert [p.name for p in EXAMPLES] == [
        "ema_crossover.json",
        "grid_10_level.json",
        "ladder_rearm.json",
        "rsi_mean_reversion.json",
        "trend_phases.json",
    ]
    for p in EXAMPLES:
        doc = validate_strategy(p.read_text())
        assert doc.schema_version == 2 and doc.name and doc.timeframe_seconds > 0


def test_examples_are_in_the_canonical_json_form_the_designer_writes() -> None:
    """Indent 2, trailing newline, and what the engine parses dumps back to the same content (SEP-5.6 backend half)."""
    for p in EXAMPLES:
        text = p.read_text()
        assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n", p.name
        full = json.loads(validate_strategy(text).model_dump_json())
        assert covers(full, json.loads(text)), p.name  # the engine keeps every value the file states


def test_grid_is_one_document_with_ten_concurrent_orders() -> None:
    doc = validate_strategy(load("grid_10_level.json"))
    assert len(doc.stages.open.orders) == 10
    assert len({o.id for o in doc.stages.open.orders}) == 10
    assert all(o.type == "limit" for o in doc.stages.open.orders)
    assert sum(o.size_fraction or 0 for o in doc.stages.open.orders) <= 1


def test_new_examples_use_phases_and_a_rearming_ladder() -> None:
    trend = validate_strategy(load("trend_phases.json"))
    assert trend.stages.manage.stop is not None and len(trend.stages.manage.stop.phases) == 3
    assert trend.stages.manage.target is not None
    assert any("4h" in e.source for e in trend.expressions())  # higher-timeframe filter
    ladder = validate_strategy(load("ladder_rearm.json"))
    assert len(ladder.stages.open.orders) == 10
    assert all(o.rearm and o.rate and o.create and o.cancel for o in ladder.stages.open.orders)


def test_examples_validate_against_exported_json_schema() -> None:
    schema = json.loads(SCHEMA_FILE.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    for p in EXAMPLES:
        jsonschema.validate(json.loads(p.read_text()), schema)


def test_committed_schema_file_is_current() -> None:
    assert SCHEMA_FILE.read_text() == schema_text(), (
        "schema/strategy.schema.json is stale; run: uv run python -m sa_engine schema -o schema/strategy.schema.json"
    )
    schema = json_schema()
    assert schema["$id"].endswith("strategy/v2.json")
    assert schema["properties"]["schema_version"]["const"] == 2


def test_schema_marks_which_strings_are_conditions_and_which_formulas() -> None:
    defs = json_schema()["$defs"]
    assert defs["Situation"]["properties"]["all"]["items"]["x-dsl"] == "boolean"
    assert defs["OrderSpec"]["properties"]["rate"]["anyOf"][0]["x-dsl"] == "number"
    assert defs["Phase"]["properties"]["level"]["x-dsl"] == "number"


def test_json_schema_rejects_structural_errors() -> None:
    schema = json.loads(SCHEMA_FILE.read_text())
    bad = minimal()
    bad["stages"]["open"]["orders"][0]["size_fraction"] = 2
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    bad = minimal()
    bad["surprise"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_defaults_and_helpers() -> None:
    doc = validate_strategy(minimal())
    assert doc.stages.trigger.on.any[0].all == ["true"]
    assert doc.stages.close is None and doc.stages.manage.rules == [] and not doc.needs_exec_bars
    assert doc.stages.open.orders[0].type == "market" and not doc.stages.open.orders[0].rearm
    assert doc.warmup_bars() == 0
    ema = validate_strategy(load("ema_crossover.json"))
    assert ema.warmup_bars() == 150
    assert "cross_above" in " ".join(e.source for e in ema.expressions())
    trend = validate_strategy(load("trend_phases.json"))
    assert trend.needs_exec_bars and trend.warmup_bars() == 50 * 3 * 4  # ema(close, 50, "4h") on 1h bars


def test_expressions_list_every_condition_and_formula_with_its_path() -> None:
    found = {e.path: e.kind for e in validate_strategy(load("trend_phases.json")).expressions()}
    assert found["$.stages.open.when.any[0].all[1]"] == "bool"
    assert found["$.stages.manage.stop.phases[2].level"] == "num"
    assert found["$.stages.manage.target.phases[0].next.any[0].all[0]"] == "bool"
    assert found["$.stages.close.when.any[0].all[0]"] == "bool"
    ladder = {e.path: e.kind for e in validate_strategy(load("ladder_rearm.json")).expressions()}
    assert ladder["$.stages.open.orders[3].rate"] == "num"
    assert ladder["$.stages.open.orders[3].cancel.any[0].all[0]"] == "bool"


def test_accepts_json_text_and_bytes() -> None:
    text = json.dumps(minimal())
    assert validate_strategy(text) == validate_strategy(text.encode()) == validate_strategy(minimal())


# --------------------------------------------------------------------------- SEP-4.4 / SEP-5.2: path and column


def test_bad_condition_reports_path_and_column() -> None:
    doc = minimal()
    doc["stages"]["open"]["when"]["any"][0]["all"][0] = "ema(close, 12) >"
    found = issues(doc)
    path = "$.stages.open.when.any[0].all[0]"
    assert list(found) == [path]
    assert "col 16" in found[path] and "expected a value" in found[path]


def test_issue_shape_is_path_column_message() -> None:
    doc = minimal()
    doc["stages"]["open"]["orders"][0].pop("size_fraction")
    doc["stages"]["open"]["orders"][0]["size"] = "close * (1 +"
    _, errs = validate_issues(doc)
    assert [e.model_dump() for e in errs] == [
        {
            "path": "$.stages.open.orders[0].size",
            "column": 12,
            "message": "expected a value but found end of expression",
        }
    ]
    _, errs = validate_issues({**minimal(), "typo": 1})
    assert errs == [
        Issue(path="$.typo", column=None, message="unknown field; check spelling against the strategy JSON Schema")
    ]
    assert str(errs[0]).startswith("$.typo: unknown field")


def test_formula_must_be_a_number_and_condition_a_boolean() -> None:
    doc = minimal()
    doc["stages"]["open"]["orders"][0].pop("size_fraction")
    doc["stages"]["open"]["orders"][0]["size"] = "close > 1"
    found = issues(doc)
    assert "a formula must be a number" in found["$.stages.open.orders[0].size"]
    assert "col 0" in found["$.stages.open.orders[0].size"]
    doc = minimal()
    doc["stages"]["open"]["when"]["any"][0]["all"][0] = "close + 1"
    assert "a condition must be a true/false expression" in issues(doc)["$.stages.open.when.any[0].all[0]"]


def test_error_path_points_into_lists() -> None:
    doc = load("grid_10_level.json")
    doc["stages"]["open"]["orders"][6]["size_fraction"] = 3
    doc["stages"]["open"]["orders"][2]["create"] = ev(("late", ["true", "close >"]))
    found = issues(doc)
    assert "$.stages.open.orders[6].size_fraction" in found
    assert "col 7" in found["$.stages.open.orders[2].create.any[0].all[1]"]


def test_every_error_in_a_document_is_reported_at_once() -> None:
    doc = load("trend_phases.json")
    doc["stages"]["manage"]["stop"]["phases"][0]["level"] = "entry_price -"
    doc["stages"]["manage"]["target"]["phases"][1]["level"] = "nope * 2"
    doc["stages"]["close"]["when"]["any"][0]["all"][0] = "close >"
    paths = {i.path: i.column for i in validate_issues(doc)[1]}
    assert paths == {
        "$.stages.manage.stop.phases[0].level": 13,
        "$.stages.manage.target.phases[1].level": 0,
        "$.stages.close.when.any[0].all[0]": 7,
    }


def test_error_message_names_every_path() -> None:
    doc = minimal()
    doc["timeframe"] = "7m"
    doc["stages"]["close"] = {"when": ev(("x", ["nope > 1"]))}
    with pytest.raises(StrategyValidationError) as exc:
        validate_strategy(doc)
    text = str(exc.value)
    assert "$.timeframe" in text and "$.stages.close.when.any[0].all[0]" in text
    assert "unknown name 'nope'" in text


def test_old_schema_version_is_refused_with_the_way_forward() -> None:
    doc = minimal()
    doc["schema_version"] = 1
    msg = issues(doc)["$.schema_version"]
    assert "version 2" in msg and "Migrate" in msg


@pytest.mark.parametrize(
    ("mutate", "path", "fragment"),
    [
        (lambda d: d.pop("name"), "$.name", "required"),
        (lambda d: d.pop("stages"), "$.stages", "required"),
        (lambda d: d["stages"].pop("open"), "$.stages.open", "required"),
        (lambda d: d.__setitem__("typo", 1), "$.typo", "unknown field"),
        (lambda d: d["stages"].__setitem__("opne", {}), "$.stages.opne", "unknown field"),
        (lambda d: d.__setitem__("schema_version", 3), "$.schema_version", "version 2"),
        (lambda d: d["stages"]["open"]["orders"].clear(), "$.stages.open.orders", ""),
        (lambda d: d["stages"]["open"]["orders"][0].__setitem__("id", "Bad Id"), "$.stages.open.orders[0].id", ""),
        (lambda d: d["stages"]["open"]["orders"][0].__setitem__("side", "sell"), "$.stages.open.orders[0].side", ""),
        (lambda d: d["stages"]["open"]["orders"][0].__setitem__("type", "oco"), "$.stages.open.orders[0].type", ""),
        (
            lambda d: d["stages"]["open"]["orders"][0].__setitem__("stop_loss_pct", 100),
            "$.stages.open.orders[0].stop_loss_pct",
            "",
        ),
        (lambda d: d["stages"]["open"].__setitem__("when", "close > 1"), "$.stages.open.when", "dictionary"),
        (lambda d: d["stages"]["open"].__setitem__("when", {"any": []}), "$.stages.open.when.any", ""),
        (
            lambda d: d["stages"]["open"].__setitem__("when", {"any": [{"all": []}]}),
            "$.stages.open.when.any[0].all",
            "",
        ),
        (
            lambda d: d["stages"]["open"].__setitem__("when", {"any": [{"all": [5]}]}),
            "$.stages.open.when.any[0].all[0]",
            "",
        ),
        (
            lambda d: d["stages"]["open"].__setitem__("when", {"any": [{"all": [""]}]}),
            "$.stages.open.when.any[0].all[0]",
            "",
        ),
        (lambda d: d["stages"]["open"].__setitem__("when", {"or": []}), "$.stages.open.when.or", "unknown field"),
        (
            lambda d: d["stages"].__setitem__("trigger", {"off": ev(("x", ["x"]))}),
            "$.stages.trigger.off.any[0].all[0]",
            "unknown name",
        ),
        (
            lambda d: d["stages"].__setitem__("manage", {"rules": [{"id": "m", "when": ALWAYS, "action": "reduce"}]}),
            "$.stages.manage.rules[0]",
            "size_fraction",
        ),
        (
            lambda d: d["stages"].__setitem__(
                "manage", {"rules": [{"id": "m", "when": ALWAYS, "action": "cancel_pending", "size_fraction": 0.5}]}
            ),
            "$.stages.manage.rules[0]",
            "takes no size_fraction",
        ),
        (lambda d: d["stages"].__setitem__("manage", []), "$.stages.manage", "dictionary"),
    ],
)
def test_invalid_documents_are_rejected_with_path(mutate: Any, path: str, fragment: str) -> None:
    doc = copy.deepcopy(minimal())
    mutate(doc)
    found = issues(doc)
    assert path in found, found
    assert fragment in found[path]


def test_cross_field_rules_have_the_owning_path() -> None:
    doc = minimal()
    doc["stages"]["open"]["orders"] = [{"id": "a", "size_fraction": 0.6}, {"id": "a", "size_fraction": 0.6}]
    found = issues(doc)
    assert "$.stages.open" in found and "unique" in found["$.stages.open"]
    doc["stages"]["open"]["orders"][1]["id"] = "b"
    assert "sum to 1.2" in issues(doc)["$.stages.open"]
    order = "$.stages.open.orders[0]"

    def order_issue(**fields: Any) -> str:
        d = minimal()
        d["stages"]["open"]["orders"][0].update(fields)
        return issues(d)[order]

    assert "market orders cannot have offset_pct" in order_issue(type="market", offset_pct=-1)
    assert "at or below the market" in order_issue(type="limit", offset_pct=1)
    assert "triggers above" in order_issue(type="stop", offset_pct=-1)
    assert "exactly one of size_fraction" in order_issue(size="0.5")
    assert "exactly one of size_fraction" in order_issue(size_fraction=None)
    assert "market orders have no rate" in order_issue(rate="close")
    assert "either rate" in order_issue(type="limit", rate="close", offset_pct=-1)
    assert "nothing to cancel" in order_issue(cancel=ALWAYS)
    assert "needs a create event" in order_issue(rearm=True)
    d = minimal()
    d["stages"]["manage"] = {
        "rules": [
            {"id": "m", "when": ALWAYS, "action": "cancel_pending"},
            {"id": "m", "when": ALWAYS, "action": "cancel_pending"},
        ]
    }
    assert "manage rule ids must be unique" in issues(d)["$.stages.manage"]


def test_phase_references_are_checked_with_exact_paths() -> None:
    def with_stop(*phases: dict[str, Any]) -> dict[str, Any]:
        d = minimal()
        d["stages"]["manage"] = {"stop": {"phases": list(phases)}}
        return d

    p = "$.stages.manage.stop.phases"
    one = {"id": "a", "level": "entry_price * 0.9"}
    assert "no next phase" in issues(with_stop({**one, "next": ALWAYS}))[f"{p}[0].next"]
    assert (
        "unknown phase 'zz'" in issues(with_stop({**one, "goto": [{"when": ALWAYS, "to": "zz"}]}))[f"{p}[0].goto[0].to"]
    )
    assert (
        "cannot move to itself"
        in issues(with_stop({**one, "goto": [{"when": ALWAYS, "to": "a"}]}))[f"{p}[0].goto[0].to"]
    )
    assert "used twice" in issues(with_stop(one, {**one, "level": "close"}))[f"{p}[1].id"]
    assert validate_strategy(with_stop({**one, "next": ALWAYS}, {"id": "b", "level": "close"})).needs_exec_bars


def test_timeframes_inside_expressions_must_fit_the_strategy_timeframe() -> None:
    doc = minimal()
    doc["timeframe"] = "4h"
    doc["stages"]["open"]["when"]["any"][0]["all"][0] = 'close > ema(close, 20, "1h")'
    found = issues(doc)
    path = "$.stages.open.when.any[0].all[0]"
    assert "multiple of the strategy timeframe" in found[path] and "col 23" in found[path]
    doc["stages"]["open"]["when"]["any"][0]["all"][0] = 'close > ema(close, 20, "7m")'
    assert "unknown timeframe" in issues(doc)[path]
    doc["stages"]["open"]["when"]["any"][0]["all"][0] = 'close > ema(close, 20, "1d")'
    assert validate_strategy(doc)


def test_non_object_and_malformed_json_are_rejected_at_the_root() -> None:
    assert "not valid JSON" in issues("{nope")["$"]
    assert "line 1 column 2" in issues("{nope")["$"]
    assert "expected a JSON object, got list" in issues([1])["$"]
    assert "expected a JSON object, got int" in issues("3")["$"]
    parsed, errs = validate_issues("{nope")
    assert parsed is None and errs[0].column is None


def test_json_path_formatting() -> None:
    assert json_path(()) == "$"
    assert json_path(("a", 3, "b")) == "$.a[3].b"


# --------------------------------------------------------------------------- properties


@given(strategy_docs())
@settings(max_examples=60, deadline=None)
def test_schema_round_trip(doc: dict[str, object]) -> None:
    parsed = validate_strategy(doc)
    dumped = parsed.model_dump(mode="json")
    assert validate_strategy(dumped) == parsed
    assert StrategyDoc.model_validate_json(parsed.model_dump_json()) == parsed
    assert validate_strategy(parsed.model_dump_json(exclude_defaults=True)) == parsed
    jsonschema.validate(dumped, json_schema())


@given(strategy_docs())
@settings(max_examples=40, deadline=None)
def test_generated_documents_have_parsable_expressions(doc: dict[str, object]) -> None:
    from sa_engine.dsl import parse_bool, parse_num

    parsed = validate_strategy(doc)
    for e in parsed.expressions():
        assert (parse_bool if e.kind == "bool" else parse_num)(e.source) is not None
