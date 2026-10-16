"""Typed strategy document, schema version 2 (v1's Trading System -> Strategy -> stages -> events, as JSON).

A document has one trigger (on/off), an open stage holding **many** orders (a 10-level grid is one document), a manage
stage (rules plus ordered phases for the stop and the target) and an optional close stage.

* **Events** (v1 semantics, SEP-5.1): a *situation* is an AND of conditions, an *event* is an OR of situations, the
  first passing situation wins (:mod:`sa_engine.events`).
* **Conditions** are boolean and **formulas** numeric expressions of the safe DSL (:mod:`sa_engine.dsl`, never
  ``eval``). Both are checked while the document is validated; an error carries its JSON path and, inside an
  expression, the 0-based column (SEP-5.2).
* **Timing** is written down in ``ARCHITECTURE.md`` and enforced by ``tests/test_timing.py``.

Invalid documents raise :class:`StrategyValidationError` listing every failing ``{path, column, message}``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from sa_engine.dsl import (
    TIMEFRAME_SECONDS,
    DslError,
    Kind,
    collect_indicators,
    parse,
    parse_bool,
    parse_num,
)

__all__ = ["TIMEFRAME_SECONDS", "Timeframe"]  # re-exported: the one table of timeframes lives in the DSL

SCHEMA_VERSION: Final = 2
Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]
SCHEMA_ID: Final = "https://superalgos.org/schemas/strategy/v2.json"

_FUNCTIONS = (
    "ema sma rsi bb_upper bb_middle bb_lower atr donchian_upper donchian_lower donchian_mid "
    "prev cross_above cross_below abs min max coalesce"
)
_NAMES = (
    "open high low close volume in_position is_long is_short position_size entry_price unrealized_pnl_pct "
    "bars_in_position stop_price target_price"
)
_TF_NOTE = 'Indicators and prices accept a last argument, a closed higher timeframe: ema(close, 20, "4h"), close("1d").'


def _dsl_error(e: DslError) -> PydanticCustomError:
    return PydanticCustomError(
        "dsl_error", "{message}", {"message": e.message, "column": e.col if e.col >= 0 else None}
    )


def _check_condition(value: str) -> str:
    try:
        parse_bool(value)
    except DslError as e:
        raise _dsl_error(e) from None
    return value


def _check_formula(value: str) -> str:
    try:
        parse_num(value)
    except DslError as e:
        raise _dsl_error(e) from None
    return value


Condition = Annotated[
    str,
    AfterValidator(_check_condition),
    Field(
        min_length=1,
        max_length=500,
        description="Boolean expression, e.g. 'cross_above(ema(close, 12), ema(close, 26)) and rsi(close, 14) < 70'. "
        f"Functions: {_FUNCTIONS}. Names: {_NAMES}. {_TF_NOTE}",
        json_schema_extra={"x-dsl": "boolean"},
    ),
]

Formula = Annotated[
    str,
    AfterValidator(_check_formula),
    Field(
        min_length=1,
        max_length=500,
        description="Numeric expression, e.g. 'max(coalesce(stop_price, 0), close - 2 * atr(14))'. "
        f"Arithmetic + - * /, parentheses, numbers. Functions: {_FUNCTIONS}. Names: {_NAMES}. {_TF_NOTE} "
        "Evaluated on bar close; no value yet (not enough history, division by zero) means 'keep the previous value'.",
        json_schema_extra={"x-dsl": "number"},
    ),
]

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- events


class Situation(_Model):
    name: str = Field(default="", max_length=80, description="audit label recorded when this situation fires")
    all: list[Condition] = Field(
        min_length=1, max_length=20, description="AND: every condition must be true for the situation to pass"
    )


class Event(_Model):
    any: list[Situation] = Field(
        min_length=1,
        max_length=20,
        description="OR: the event fires when any situation passes; the first passing situation (array order) wins",
    )


def always(name: str = "always") -> Event:
    """An event that fires on every bar."""
    return Event(any=[Situation(name=name, all=["true"])])


# ---------------------------------------------------------------- open stage


class OrderSpec(_Model):
    id: Identifier = Field(description="unique within the strategy")
    side: Literal["buy"] = Field(default="buy", description="spot, long-only: entries are buys")
    type: Literal["market", "limit", "stop"] = "market"
    size_fraction: float | None = Field(
        default=None, ge=0.0001, le=1, description="fixed fraction of account equity committed by this order"
    )
    size: Formula | None = Field(
        default=None,
        description="instead of size_fraction: a formula giving the fraction of equity, evaluated when the order is "
        "placed. Clamped to what is left of 100% of equity; <= 0 or no value means 'place nothing this bar'",
    )
    offset_pct: float = Field(
        default=0.0,
        ge=-100,
        le=100,
        description="limit/stop only: signed percent from the bar close at placement (limit buys: <= 0 = below)",
    )
    rate: Formula | None = Field(
        default=None,
        description="limit/stop only, instead of offset_pct: the order price as a formula, evaluated when the order is "
        "placed. A limit is capped at the bar close, a stop floored at it; <= 0 or no value means 'not this bar'",
    )
    create: Event | None = Field(
        default=None,
        description="checked on every bar of the cycle while the order is not placed; when it fires the order is "
        "placed (it fills from the next bar). Absent: place it as soon as the cycle starts",
    )
    cancel: Event | None = Field(default=None, description="cancels this order while it is working (limit/stop only)")
    rearm: bool = Field(
        default=False,
        description="after the order fills (or is cancelled) it may be placed again by its create event while the "
        "cycle lasts (v1 spawnMultipleOrders): a filled ladder level is re-placed",
    )
    take_profit_pct: float | None = Field(
        default=None, gt=0, le=1000, description="after this order fills, sell it at fill price x (1 + pct/100)"
    )
    stop_loss_pct: float | None = Field(
        default=None, gt=0, lt=100, description="after this order fills, stop-sell at fill price x (1 - pct/100)"
    )

    @model_validator(mode="after")
    def _consistent(self) -> OrderSpec:
        if (self.size_fraction is None) == (self.size is None):
            raise ValueError("give exactly one of size_fraction (fixed) or size (formula)")
        if self.rate is not None:
            if self.type == "market":
                raise ValueError("market orders have no rate; use type 'limit' or 'stop' for a price formula")
            if self.offset_pct != 0:
                raise ValueError("give either rate (formula) or offset_pct, not both")
        if self.type == "market" and self.offset_pct != 0:
            raise ValueError("market orders cannot have offset_pct; use type 'limit' or 'stop'")
        if self.type == "limit" and self.offset_pct > 0:
            raise ValueError(
                "a limit buy must be at or below the market: offset_pct <= 0 (use type 'stop' to buy higher)"
            )
        if self.type == "stop" and self.offset_pct < 0:
            raise ValueError("a stop buy triggers above the market: offset_pct >= 0")
        if self.type == "market" and self.cancel is not None:
            raise ValueError("a market order fills at once, so it has nothing to cancel; use a limit or stop order")
        if self.type == "market" and self.rearm and self.create is None:
            raise ValueError("a re-armed market order needs a create event, or it would be bought again on every bar")
        return self


class Trigger(_Model):
    on: Event = Field(default_factory=always, description="activates the strategy when it fires")
    off: Event | None = Field(
        default=None, description="deactivates (no new entries; working entries cancelled; a held position stays)"
    )


class OpenStage(_Model):
    when: Event = Field(
        description="take-position event: an entry cycle starts when it fires while active, flat, with nothing working"
    )
    orders: list[OrderSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _orders(self) -> OpenStage:
        ids = [o.id for o in self.orders]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"order ids must be unique; duplicated: {', '.join(dupes)}")
        total = sum(o.size_fraction or 0.0 for o in self.orders)
        if total > 1 + 1e-9:
            raise ValueError(f"order size_fractions sum to {total:g}; they must sum to at most 1 (spot, no leverage)")
        return self


# ---------------------------------------------------------------- manage stage


class ManageRule(_Model):
    id: Identifier
    when: Event
    action: Literal["cancel_pending", "reduce"] = Field(
        description="cancel_pending: close the open stage (cancel unfilled entries, place no more); "
        "reduce: sell size_fraction of the position"
    )
    size_fraction: float | None = Field(default=None, gt=0, le=1)
    once: bool = Field(default=True, description="fire at most once per entry cycle")

    @model_validator(mode="after")
    def _fraction(self) -> ManageRule:
        if self.action == "reduce" and self.size_fraction is None:
            raise ValueError("action 'reduce' needs size_fraction")
        if self.action == "cancel_pending" and self.size_fraction is not None:
            raise ValueError("action 'cancel_pending' takes no size_fraction")
        return self


class Goto(_Model):
    when: Event
    to: Identifier = Field(description="id of another phase of the same list")


class Phase(_Model):
    id: Identifier = Field(description="unique within its stop or target")
    level: Formula = Field(description="the stop (or target) price while this phase is current")
    next: Event | None = Field(default=None, description="moves to the next phase of the list (checked first)")
    goto: list[Goto] = Field(
        default_factory=list, max_length=20, description="move-to-phase events, in order; the first that fires wins"
    )


class PhasedLevel(_Model):
    phases: list[Phase] = Field(
        min_length=1, max_length=20, description="ordered; phase 1 is current when the position opens"
    )


class Manage(_Model):
    rules: list[ManageRule] = Field(default_factory=list, max_length=20)
    stop: PhasedLevel | None = Field(
        default=None, description="stop loss: the position is sold at market once a bar's low reaches the level"
    )
    target: PhasedLevel | None = Field(
        default=None,
        description="take profit: sold at market once a bar's high reaches the level (the stop is tested first)",
    )

    @model_validator(mode="after")
    def _unique_rules(self) -> Manage:
        ids = [r.id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"manage rule ids must be unique; duplicated: {', '.join(dupes)}")
        return self


class CloseStage(_Model):
    when: Event = Field(description="cancels working orders and sells the whole position with a market order")


class Stages(_Model):
    trigger: Trigger = Field(default_factory=Trigger)
    open: OpenStage
    manage: Manage = Field(default_factory=Manage)
    close: CloseStage | None = None


# ---------------------------------------------------------------- document


@dataclass(frozen=True, slots=True)
class Expression:
    path: str  # JSON path of the string
    source: str
    kind: Kind  # bool: a condition, num: a formula


def _event_expressions(path: str, event: Event | None) -> Iterator[Expression]:
    if event is None:
        return
    for i, situation in enumerate(event.any):
        for j, cond in enumerate(situation.all):
            yield Expression(f"{path}.any[{i}].all[{j}]", cond, "bool")


class StrategyDoc(_Model):
    schema_version: Literal[2] = Field(default=2, description="2; documents of schema 1 must be migrated")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    timeframe: Timeframe
    stages: Stages

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, v: object) -> object:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {v!r} is not supported; this engine reads version {SCHEMA_VERSION}. "
                "Migrate the document to schema 2 and set schema_version to 2"
            )
        return v

    @property
    def timeframe_seconds(self) -> int:
        return TIMEFRAME_SECONDS[self.timeframe]

    def expressions(self) -> list[Expression]:
        """Every condition and formula in the document, in document order, with its JSON path."""
        s = self.stages
        out: list[Expression] = []
        out += _event_expressions("$.stages.trigger.on", s.trigger.on)
        out += _event_expressions("$.stages.trigger.off", s.trigger.off)
        out += _event_expressions("$.stages.open.when", s.open.when)
        for i, o in enumerate(s.open.orders):
            base = f"$.stages.open.orders[{i}]"
            if o.size is not None:
                out.append(Expression(f"{base}.size", o.size, "num"))
            if o.rate is not None:
                out.append(Expression(f"{base}.rate", o.rate, "num"))
            out += _event_expressions(f"{base}.create", o.create)
            out += _event_expressions(f"{base}.cancel", o.cancel)
        for i, r in enumerate(s.manage.rules):
            out += _event_expressions(f"$.stages.manage.rules[{i}].when", r.when)
        for label, level in (("stop", s.manage.stop), ("target", s.manage.target)):
            if level is None:
                continue
            for i, ph in enumerate(level.phases):
                base = f"$.stages.manage.{label}.phases[{i}]"
                out.append(Expression(f"{base}.level", ph.level, "num"))
                out += _event_expressions(f"{base}.next", ph.next)
                for j, g in enumerate(ph.goto):
                    out += _event_expressions(f"{base}.goto[{j}].when", g.when)
        if s.close is not None:
            out += _event_expressions("$.stages.close.when", s.close.when)
        return out

    def warmup_bars(self) -> int:
        """Rough number of strategy bars before every indicator has a value (informational)."""
        base = self.timeframe_seconds
        most = 0
        for e in self.expressions():
            for sp in collect_indicators(parse(e.source)):
                ratio = TIMEFRAME_SECONDS[sp.tf] // base if sp.tf else 1
                most = max(most, sp.period * 3 * ratio)
        return most

    @property
    def needs_exec_bars(self) -> bool:
        """True when stop or target levels are watched between strategy bars (see ARCHITECTURE.md, timing rules)."""
        m = self.stages.manage
        return m.stop is not None or m.target is not None


# ---------------------------------------------------------------- errors


class Issue(BaseModel):
    """One problem: where (``path``), where inside the expression (``column``, 0-based, else null) and what."""

    path: str
    column: int | None = None
    message: str

    def __str__(self) -> str:
        where = f"col {self.column}: " if self.column is not None else ""
        return f"{self.path}: {where}{self.message}"


class StrategyValidationError(ValueError):
    """Invalid strategy document; ``issues`` carry JSON paths like ``$.stages.open.orders[2].size_fraction``."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("invalid strategy document: " + "; ".join(str(i) for i in issues))


def json_path(loc: tuple[int | str, ...]) -> str:
    out = "$"
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _issues(err: ValidationError) -> list[Issue]:
    issues: list[Issue] = []
    for e in err.errors():
        column: int | None = None
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = "unknown field; check spelling against the strategy JSON Schema"
        elif e["type"] == "missing":
            msg = "required field is missing"
        elif e["type"] == "dsl_error":
            column = (e.get("ctx") or {}).get("column")
        issues.append(Issue(path=json_path(e["loc"]), column=column, message=msg))
    return issues


def semantic_issues(doc: StrategyDoc) -> list[Issue]:
    """Checks that need the whole document: timeframes in expressions, phase references."""
    issues: list[Issue] = []
    base = doc.timeframe_seconds
    for e in doc.expressions():
        try:
            (parse_bool if e.kind == "bool" else parse_num)(e.source, base)
        except DslError as err:
            issues.append(Issue(path=e.path, column=err.col if err.col >= 0 else None, message=err.message))
    for label, level in (("stop", doc.stages.manage.stop), ("target", doc.stages.manage.target)):
        if level is None:
            continue
        ids = [p.id for p in level.phases]
        for i, ph in enumerate(level.phases):
            base_path = f"$.stages.manage.{label}.phases[{i}]"
            if ids.count(ph.id) > 1 and ids.index(ph.id) != i:
                issues.append(Issue(path=f"{base_path}.id", message=f"phase id {ph.id!r} is used twice in {label}"))
            if ph.next is not None and i == len(level.phases) - 1:
                issues.append(
                    Issue(path=f"{base_path}.next", message="the last phase has no next phase; use goto or remove next")
                )
            for j, g in enumerate(ph.goto):
                if g.to not in ids:
                    issues.append(
                        Issue(
                            path=f"{base_path}.goto[{j}].to",
                            message=f"unknown phase {g.to!r}; the phases of {label} are: {', '.join(ids)}",
                        )
                    )
                elif g.to == ph.id:
                    issues.append(Issue(path=f"{base_path}.goto[{j}].to", message="a phase cannot move to itself"))
    return issues


def check_semantics(doc: StrategyDoc) -> StrategyDoc:
    """Raise :class:`StrategyValidationError` when :func:`semantic_issues` finds problems; return ``doc``."""
    issues = semantic_issues(doc)
    if issues:
        raise StrategyValidationError(issues)
    return doc


def validate_issues(doc: Mapping[str, Any] | str | bytes) -> tuple[StrategyDoc | None, list[Issue]]:
    """Validate and return ``(document, [])`` or ``(None, issues)``; never raises for a bad document."""
    if isinstance(doc, str | bytes):
        try:
            data = json.loads(doc)
        except json.JSONDecodeError as e:
            return None, [Issue(path="$", message=f"not valid JSON: {e.msg} at line {e.lineno} column {e.colno}")]
    else:
        data = doc
    if not isinstance(data, Mapping):
        return None, [Issue(path="$", message=f"expected a JSON object, got {type(data).__name__}")]
    try:
        parsed = StrategyDoc.model_validate(dict(data))
    except ValidationError as e:
        return None, _issues(e)
    issues = semantic_issues(parsed)
    return (None, issues) if issues else (parsed, [])


def validate_strategy(doc: Mapping[str, Any] | str | bytes) -> StrategyDoc:
    """Validate a strategy document (dict, or JSON text from an LLM/file). Raises StrategyValidationError."""
    parsed, issues = validate_issues(doc)
    if parsed is None:
        raise StrategyValidationError(issues)
    return parsed


def json_schema() -> dict[str, Any]:
    schema = StrategyDoc.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCHEMA_ID
    schema["title"] = "Superalgos v2 strategy"
    return schema


def schema_text() -> str:
    return json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python -m sa_engine.schema OUTPUT.json")
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        fh.write(schema_text())
