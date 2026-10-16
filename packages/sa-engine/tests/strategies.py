"""Hypothesis strategies producing well-typed DSL ASTs and valid strategy documents."""

from __future__ import annotations

from hypothesis import strategies as st

from sa_engine.dsl import (
    BOOL_VARS,
    NUM_VARS,
    SERIES_NAMES,
    Binary,
    Bool,
    Call,
    Node,
    Num,
    Series,
    Unary,
    Var,
    to_source,
)

series = st.sampled_from(SERIES_NAMES).map(Series)
small_floats = st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False).map(abs)
periods = st.integers(min_value=1, max_value=60).map(float).map(Num)
shifts = st.integers(min_value=0, max_value=20).map(float).map(Num)
consts = st.floats(min_value=-10, max_value=10, allow_nan=False, allow_infinity=False).map(Num)

num_leaf = st.one_of(
    small_floats.map(Num),
    series,
    st.sampled_from(NUM_VARS).map(Var),
    st.builds(lambda s, p, kind: Call(kind, (s, p)), series, periods, st.sampled_from(["ema", "sma", "rsi"])),
    st.builds(
        lambda s, p, k, kind: Call(kind, (s, p, k)),
        series,
        periods,
        consts,
        st.sampled_from(["bb_upper", "bb_middle", "bb_lower"]),
    ),
    st.builds(
        lambda p, kind: Call(kind, (p,)),
        periods,
        st.sampled_from(["atr", "donchian_upper", "donchian_lower", "donchian_mid"]),
    ),
)


def _num_extend(children: st.SearchStrategy[Node]) -> st.SearchStrategy[Node]:
    return st.one_of(
        children.map(lambda c: Unary("-", c)),
        st.builds(lambda a, b, op: Binary(op, a, b), children, children, st.sampled_from(["+", "-", "*", "/"])),
        st.builds(lambda a, n: Call("prev", (a, n)), children, shifts),
        children.map(lambda c: Call("abs", (c,))),
        st.builds(lambda a, b: Call("coalesce", (a, b)), children, children),
        st.builds(lambda a, b, fn: Call(fn, (a, b)), children, children, st.sampled_from(["min", "max"])),
    )


num_exprs: st.SearchStrategy[Node] = st.recursive(num_leaf, _num_extend, max_leaves=8)

bool_leaf = st.one_of(
    st.booleans().map(Bool),
    st.sampled_from(BOOL_VARS).map(Var),
    st.builds(
        lambda a, b, op: Binary(op, a, b), num_exprs, num_exprs, st.sampled_from(["<", "<=", ">", ">=", "==", "!="])
    ),
    st.builds(lambda a, b, fn: Call(fn, (a, b)), num_exprs, num_exprs, st.sampled_from(["cross_above", "cross_below"])),
)


def _bool_extend(children: st.SearchStrategy[Node]) -> st.SearchStrategy[Node]:
    return st.one_of(
        children.map(lambda c: Unary("not", c)),
        st.builds(lambda a, b, op: Binary(op, a, b), children, children, st.sampled_from(["and", "or"])),
    )


bool_exprs: st.SearchStrategy[Node] = st.recursive(bool_leaf, _bool_extend, max_leaves=6)

bool_sources = bool_exprs.map(to_source).filter(lambda s: len(s) <= 480)
num_sources = num_exprs.map(to_source).filter(lambda s: len(s) <= 480)


def ev(*situations: tuple[str, list[str]]) -> dict[str, object]:
    """An event document: ``ev(("name", ["cond a", "cond b"]), ...)`` is (a AND b) OR ..."""
    return {"any": [{"name": n, "all": list(c)} for n, c in situations]}


ALWAYS = ev(("always", ["true"]))

situations = st.builds(
    lambda name, conds: (name, conds),
    st.text(alphabet="abc xyz", max_size=12),
    st.lists(bool_sources, min_size=1, max_size=3),
)
events = st.lists(situations, min_size=1, max_size=3).map(lambda ss: ev(*ss))

ids = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)


@st.composite
def phased_levels(draw: st.DrawFn) -> dict[str, object]:
    n = draw(st.integers(min_value=1, max_value=4))
    names = [f"p{i}" for i in range(n)]
    phases: list[dict[str, object]] = []
    for i, name in enumerate(names):
        phase: dict[str, object] = {"id": name, "level": draw(num_sources)}
        if i < n - 1 and draw(st.booleans()):
            phase["next"] = draw(events)
        others = [x for x in names if x != name]
        wants_goto = draw(st.booleans())
        if others and wants_goto:
            phase["goto"] = [{"when": draw(events), "to": draw(st.sampled_from(others))}]
        phases.append(phase)
    return {"phases": phases}


@st.composite
def strategy_docs(draw: st.DrawFn) -> dict[str, object]:
    n = draw(st.integers(min_value=1, max_value=10))
    orders: list[dict[str, object]] = []
    for i in range(n):
        kind = draw(st.sampled_from(["market", "limit", "stop"]))
        order: dict[str, object] = {"id": f"o{i}", "type": kind}
        if draw(st.booleans()):
            order["size_fraction"] = round(draw(st.floats(min_value=0.01, max_value=1.0)) / n, 6)
        else:
            order["size"] = draw(num_sources)
        if kind != "market":
            if draw(st.booleans()):
                order["rate"] = draw(num_sources)
            else:
                sign = -1 if kind == "limit" else 1
                order["offset_pct"] = sign * draw(st.floats(min_value=0, max_value=50))
            if draw(st.booleans()):
                order["cancel"] = draw(events)
        if draw(st.booleans()):
            order["create"] = draw(events)
        if draw(st.booleans()) and (kind != "market" or "create" in order):
            order["rearm"] = True
        if draw(st.booleans()):
            order["take_profit_pct"] = draw(st.floats(min_value=0.1, max_value=50))
        if draw(st.booleans()):
            order["stop_loss_pct"] = draw(st.floats(min_value=0.1, max_value=50))
        orders.append(order)
    manage: dict[str, object] = {
        "rules": [
            {"id": f"r{j}", "when": draw(events), "action": "reduce", "size_fraction": 0.5, "once": draw(st.booleans())}
            for j in range(draw(st.integers(min_value=0, max_value=3)))
        ],
    }
    if draw(st.booleans()):
        manage["stop"] = draw(phased_levels())
    if draw(st.booleans()):
        manage["target"] = draw(phased_levels())
    stages: dict[str, object] = {
        "trigger": {"on": draw(events), **({"off": draw(events)} if draw(st.booleans()) else {})},
        "open": {"when": draw(events), "orders": orders},
        "manage": manage,
    }
    if draw(st.booleans()):
        stages["close"] = {"when": draw(events)}
    return {
        "schema_version": 2,
        "name": draw(st.text(min_size=1, max_size=40).filter(lambda t: t.strip() != "")),
        "timeframe": draw(st.sampled_from(["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"])),
        "stages": stages,
    }
