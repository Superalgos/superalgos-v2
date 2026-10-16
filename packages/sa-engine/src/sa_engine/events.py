"""Events as v1 defined them: a situation is an AND of conditions, an event is an OR of situations, first match wins.

v1 facts this keeps: a situation passes only when *every* condition is exactly true; an event
fires when *any* situation passes and the first passing one (array order) is the one reported; a situation without
conditions and an event without situations never fire. v2 conditions are boolean DSL expressions, so "exactly true"
is just ``True``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sa_engine.dsl import EvalContext, Node, evaluate


@dataclass(frozen=True, slots=True)
class CompiledSituation:
    name: str
    conditions: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class CompiledEvent:
    situations: tuple[CompiledSituation, ...]


def situation_passes(situation: CompiledSituation, ctx: EvalContext) -> bool:
    """AND of the conditions; an empty situation never passes."""
    if not situation.conditions:
        return False
    return all(evaluate(c, ctx) is True for c in situation.conditions)


def first_match(event: CompiledEvent, ctx: EvalContext) -> str | None:
    """The name of the first passing situation (``#<index>`` when unnamed), or ``None`` when the event does not fire."""
    for i, situation in enumerate(event.situations):
        if situation_passes(situation, ctx):
            return situation.name or f"#{i}"
    return None


def fires(event: CompiledEvent, ctx: EvalContext) -> bool:
    return first_match(event, ctx) is not None
