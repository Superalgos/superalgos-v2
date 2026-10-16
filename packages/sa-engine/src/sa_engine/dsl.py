"""Safe expression DSL for strategy conditions.

Grammar (lowest to highest precedence)::

    expr  := or
    or    := and ('or' and)*
    and   := not ('and' not)*
    not   := 'not' not | cmp
    cmp   := sum (('<'|'<='|'>'|'>='|'=='|'!=') sum)?
    sum   := term (('+'|'-') term)*
    term  := unary (('*'|'/') unary)*
    unary := '-' unary | atom
    atom  := NUMBER | 'true' | 'false' | NAME | NAME '(' args ')' | SERIES '(' STRING ')' | '(' expr ')'

Indicator calls take an optional last argument, a quoted timeframe (``ema(close, 20, "4h")``, ``atr(14, "1d")``,
``close("4h")``); a leading source may be left out for the price-based ones (``ema(20)`` means ``ema(close, 20)``).
A higher-timeframe value is the one of the last *closed* bar of that timeframe at the moment the strategy bar closes
(:class:`sa_engine.indicators.IndicatorBank` builds it), so an unclosed higher-timeframe bar is never read.

Expressions are parsed by a hand-written recursive-descent parser into a typed AST and evaluated by a
tree walker. Nothing is ever passed to ``eval``/``exec``. Every expression is either numeric
(``float | None``; ``None`` means "not enough history yet" and propagates) or boolean (never ``None``:
a comparison involving ``None`` is ``False``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal, Protocol

TIMEFRAME_SECONDS: Final[dict[str, int]] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}

MAX_SHIFT: Final = 1000
MAX_PERIOD: Final = 1000
MAX_DEPTH: Final = 64

SERIES_NAMES: Final = ("open", "high", "low", "close", "volume")
NUM_VARS: Final = (
    "position_size",
    "entry_price",
    "unrealized_pnl_pct",
    "bars_in_position",
    "stop_price",
    "target_price",
)
BOOL_VARS: Final = ("in_position", "is_long", "is_short")

Kind = Literal["num", "bool"]


class DslError(ValueError):
    """Parse or type error in a condition. ``col`` is the 0-based column in the source (or -1)."""

    def __init__(self, message: str, col: int = -1) -> None:
        super().__init__(f"col {col}: {message}" if col >= 0 else message)
        self.message = message
        self.col = col


# ---------------------------------------------------------------- AST


@dataclass(frozen=True, slots=True)
class Num:
    value: float


@dataclass(frozen=True, slots=True)
class Bool:
    value: bool


@dataclass(frozen=True, slots=True)
class Series:
    name: str
    tf: str | None = None  # higher timeframe, e.g. "4h" (None: the strategy timeframe)


@dataclass(frozen=True, slots=True)
class Var:
    name: str


@dataclass(frozen=True, slots=True)
class Call:
    fn: str
    args: tuple[Node, ...]
    tf: str | None = None  # indicator calls only


@dataclass(frozen=True, slots=True)
class Unary:
    op: Literal["-", "not"]
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: Node
    right: Node


Node = Num | Bool | Series | Var | Call | Unary | Binary


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Identity of one streaming indicator needed by an expression."""

    kind: str  # ema sma rsi bb_upper bb_middle bb_lower atr donchian_upper donchian_lower donchian_mid
    src: str  # price series name ("" for atr / donchian)
    period: int
    k: float = 0.0  # Bollinger width multiplier
    tf: str | None = None  # computed on closed bars of this timeframe (None: the strategy timeframe)


# Argument kinds: "series" price name, "int" integer literal 1..MAX_PERIOD, "const" numeric literal, "num"/"bool" expr
_SIGNATURES: Final[dict[str, tuple[tuple[str, ...], Kind]]] = {
    "ema": (("series", "int"), "num"),
    "sma": (("series", "int"), "num"),
    "rsi": (("series", "int"), "num"),
    "bb_upper": (("series", "int", "const"), "num"),
    "bb_middle": (("series", "int", "const"), "num"),
    "bb_lower": (("series", "int", "const"), "num"),
    "atr": (("int",), "num"),
    "donchian_upper": (("int",), "num"),
    "donchian_lower": (("int",), "num"),
    "donchian_mid": (("int",), "num"),
    "prev": (("num", "shift"), "num"),
    "cross_above": (("num", "num"), "bool"),
    "cross_below": (("num", "num"), "bool"),
    "abs": (("num",), "num"),
    "min": (("num", "num"), "num"),
    "max": (("num", "num"), "num"),
    "coalesce": (("num", "num"), "num"),
}
INDICATOR_FUNCS: Final = frozenset(
    {
        "ema",
        "sma",
        "rsi",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "atr",
        "donchian_upper",
        "donchian_lower",
        "donchian_mid",
    }
)
FUNCTION_NAMES: Final = tuple(_SIGNATURES)


def kind_of(node: Node) -> Kind:
    match node:
        case Num() | Series():
            return "num"
        case Bool():
            return "bool"
        case Var(name):
            return "bool" if name in BOOL_VARS else "num"
        case Call(fn):
            return _SIGNATURES[fn][1]
        case Unary(op, _):
            return "bool" if op == "not" else "num"
        case Binary(op, _, _):
            return "num" if op in ("+", "-", "*", "/") else "bool"


# ---------------------------------------------------------------- tokenizer

_TOKEN = re.compile(
    r"""\s*(?:
    (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
   |(?P<name>[A-Za-z_][A-Za-z0-9_]*)
   |(?P<str>"[^"\n]*")
   |(?P<op><=|>=|==|!=|<|>|\+|-|\*|/|\(|\)|,)
   )""",
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str  # num name op end
    text: str
    col: int


def _tokenize(src: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos = 0
    n = len(src)
    while pos < n:
        if src[pos:].strip() == "":
            break
        m = _TOKEN.match(src, pos)
        if m is None or m.end() == pos:
            col = pos + (len(src[pos:]) - len(src[pos:].lstrip()))
            if src[col] == '"':
                raise DslError('unterminated string; timeframes are written "4h" on one line', col)
            raise DslError(f"unexpected character {src[col]!r}", col)
        kind = m.lastgroup or ""
        start = m.start(kind)
        toks.append(_Tok(kind, m.group(kind), start))
        pos = m.end()
    toks.append(_Tok("end", "", n))
    return toks


# ---------------------------------------------------------------- parser


class _Parser:
    def __init__(self, src: str, base_seconds: int | None = None) -> None:
        self.src = src
        self.base_seconds = base_seconds
        self.toks = _tokenize(src)
        self.i = 0
        self.depth = 0

    @property
    def tok(self) -> _Tok:
        return self.toks[self.i]

    def _next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _is(self, text: str) -> bool:
        return self.tok.kind in ("op", "name") and self.tok.text == text

    def _expect(self, text: str) -> None:
        if not self._is(text):
            raise DslError(f"expected {text!r} but found {self._describe(self.tok)}", self.tok.col)
        self.i += 1

    @staticmethod
    def _describe(t: _Tok) -> str:
        return "end of expression" if t.kind == "end" else repr(t.text)

    def parse(self) -> Node:
        if self.tok.kind == "end":
            raise DslError("empty expression", 0)
        node = self._or()
        if self.tok.kind != "end":
            raise DslError(f"unexpected {self._describe(self.tok)} after complete expression", self.tok.col)
        return node

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise DslError(f"expression nested deeper than {MAX_DEPTH} levels", self.tok.col)

    def _need(self, node: Node, kind: Kind, col: int, what: str) -> None:
        if kind_of(node) != kind:
            raise DslError(f"{what} needs a {kind} operand but got a {kind_of(node)} expression", col)

    def _or(self) -> Node:
        self._enter()
        left = self._and()
        while self._is("or"):
            col = self._next().col
            self._need(left, "bool", col, "'or'")
            right = self._and()
            self._need(right, "bool", col, "'or'")
            left = Binary("or", left, right)
        self.depth -= 1
        return left

    def _and(self) -> Node:
        left = self._not()
        while self._is("and"):
            col = self._next().col
            self._need(left, "bool", col, "'and'")
            right = self._not()
            self._need(right, "bool", col, "'and'")
            left = Binary("and", left, right)
        return left

    def _not(self) -> Node:
        if self._is("not"):
            col = self._next().col
            self._enter()
            operand = self._not()
            self.depth -= 1
            self._need(operand, "bool", col, "'not'")
            return Unary("not", operand)
        return self._cmp()

    def _cmp(self) -> Node:
        left = self._sum()
        if self.tok.kind == "op" and self.tok.text in ("<", "<=", ">", ">=", "==", "!="):
            t = self._next()
            right = self._sum()
            self._need(left, "num", t.col, repr(t.text))
            self._need(right, "num", t.col, repr(t.text))
            return Binary(t.text, left, right)
        return left

    def _sum(self) -> Node:
        left = self._term()
        while self.tok.kind == "op" and self.tok.text in ("+", "-"):
            t = self._next()
            right = self._term()
            self._need(left, "num", t.col, repr(t.text))
            self._need(right, "num", t.col, repr(t.text))
            left = Binary(t.text, left, right)
        return left

    def _term(self) -> Node:
        left = self._unary()
        while self.tok.kind == "op" and self.tok.text in ("*", "/"):
            t = self._next()
            right = self._unary()
            self._need(left, "num", t.col, repr(t.text))
            self._need(right, "num", t.col, repr(t.text))
            left = Binary(t.text, left, right)
        return left

    def _unary(self) -> Node:
        if self.tok.kind == "op" and self.tok.text == "-":
            t = self._next()
            self._enter()
            operand = self._unary()
            self.depth -= 1
            self._need(operand, "num", t.col, "unary '-'")
            return Unary("-", operand)
        return self._atom()

    def _atom(self) -> Node:
        t = self.tok
        if t.kind == "num":
            self.i += 1
            return Num(_literal(t))
        if t.kind == "op" and t.text == "(":
            self.i += 1
            self._enter()
            inner = self._or()
            self.depth -= 1
            self._expect(")")
            return inner
        if t.kind == "name":
            self.i += 1
            if t.text in ("true", "false"):
                return Bool(t.text == "true")
            if t.text in SERIES_NAMES and self._is("("):
                self.i += 1
                if self.tok.kind != "str":
                    raise DslError(f'{t.text}() takes a quoted timeframe, e.g. {t.text}("4h")', self.tok.col)
                tf = self._tf(self._next())
                self._expect(")")
                return Series(t.text, tf)
            if self._is("("):
                return self._call(t)
            if t.text in SERIES_NAMES:
                return Series(t.text)
            if t.text in NUM_VARS or t.text in BOOL_VARS:
                return Var(t.text)
            known = ", ".join((*SERIES_NAMES, *NUM_VARS, *BOOL_VARS))
            raise DslError(f"unknown name {t.text!r}; known names: {known}", t.col)
        raise DslError(f"expected a value but found {self._describe(t)}", t.col)

    def _call(self, name_tok: _Tok) -> Node:
        fn = name_tok.text
        sig = _SIGNATURES.get(fn)
        if sig is None:
            raise DslError(f"unknown function {fn!r}; known functions: {', '.join(FUNCTION_NAMES)}", name_tok.col)
        self._expect("(")
        arg_kinds = sig[0]
        args: list[Node] = []
        start = 0
        if arg_kinds[0] == "series" and self.tok.kind == "num":  # ema(20) is ema(close, 20)
            args.append(Series("close"))
            start = 1
        for idx in range(start, len(arg_kinds)):
            if idx > start:
                self._expect(",")
            args.append(self._arg(fn, arg_kinds[idx], idx))
        tf: str | None = None
        if fn in INDICATOR_FUNCS and self._is(",") and self.toks[self.i + 1].kind == "str":
            self.i += 1
            tf = self._tf(self._next())
        if self._is(","):
            raise DslError(f"{fn}() takes {len(arg_kinds)} argument(s)", self.tok.col)
        self._expect(")")
        node = Call(fn, tuple(args), tf)
        if max_shift(node) > MAX_SHIFT:
            raise DslError(f"{fn}() looks back more than {MAX_SHIFT} bars", name_tok.col)
        return node

    def _tf(self, tok: _Tok) -> str:
        tf = tok.text[1:-1]
        seconds = TIMEFRAME_SECONDS.get(tf)
        if seconds is None:
            raise DslError(f"unknown timeframe {tok.text}; use one of {', '.join(TIMEFRAME_SECONDS)}", tok.col)
        base = self.base_seconds
        if base is not None and (seconds < base or seconds % base):
            raise DslError(
                f"timeframe {tok.text} ({seconds}s) must be a whole multiple of the strategy timeframe ({base}s); "
                "a strategy cannot read a faster timeframe than the one it runs on",
                tok.col,
            )
        return tf

    def _arg(self, fn: str, ak: str, idx: int) -> Node:
        col = self.tok.col
        if ak == "series":
            t = self._next()
            if t.kind != "name" or t.text not in SERIES_NAMES:
                raise DslError(
                    f"{fn}() argument {idx + 1} must be one of {', '.join(SERIES_NAMES)}, got {self._describe(t)}",
                    t.col,
                )
            return Series(t.text)
        if ak in ("int", "shift", "const"):
            neg = False
            if ak == "const" and self.tok.kind == "op" and self.tok.text == "-":
                self.i += 1
                neg = True
            t = self._next()
            if t.kind != "num":
                raise DslError(f"{fn}() argument {idx + 1} must be a number literal, got {self._describe(t)}", t.col)
            value = _literal(t)
            if ak != "const":
                low = 0 if ak == "shift" else 1
                if not value.is_integer() or not low <= value <= MAX_PERIOD:
                    raise DslError(
                        f"{fn}() argument {idx + 1} must be an integer in {low}..{MAX_PERIOD}, got {t.text}", t.col
                    )
            return Num(-value if neg else value)
        node = self._or()
        self._need(node, "num", col, f"{fn}() argument {idx + 1}")
        return node


def _literal(t: _Tok) -> float:
    value = float(t.text)
    if math.isinf(value):
        raise DslError(f"number {t.text} is too large", t.col)
    return value


MAX_SOURCE_LEN: Final = 500


def parse(src: str, base_seconds: int | None = None) -> Node:
    """Parse ``src`` into an AST or raise :class:`DslError` (with column) on any syntax/type problem.

    ``base_seconds`` (the strategy timeframe) additionally checks that every timeframe written in ``src`` is a whole
    multiple of it.
    """
    if not isinstance(src, str):
        raise DslError(f"expression must be a string, got {type(src).__name__}")
    if len(src) > MAX_SOURCE_LEN:
        raise DslError(f"expression is {len(src)} characters; the limit is {MAX_SOURCE_LEN}", 0)
    return _Parser(src, base_seconds).parse()


@lru_cache(maxsize=1024)
def parse_cached(src: str) -> Node:
    return parse(src)


def parse_bool(src: str, base_seconds: int | None = None) -> Node:
    """Parse and require a boolean (condition) expression."""
    node = parse(src, base_seconds)
    if kind_of(node) != "bool":
        raise DslError(
            "a condition must be a true/false expression (use a comparison such as 'rsi(close, 14) < 30')", 0
        )
    return node


def parse_num(src: str, base_seconds: int | None = None) -> Node:
    """Parse and require a numeric (formula) expression, e.g. ``max(stop_price, close * 0.97)``."""
    node = parse(src, base_seconds)
    if kind_of(node) != "num":
        raise DslError(
            "a formula must be a number, not a true/false expression (use arithmetic such as 'entry_price * 0.95')", 0
        )
    return node


# ---------------------------------------------------------------- printing


def _fmt_num(v: float) -> str:
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def to_source(node: Node) -> str:
    """Canonical, fully parenthesised source; ``parse(to_source(n)) == n`` for every parsable ``n``."""
    match node:
        case Num(v):
            return _fmt_num(v)
        case Bool(v):
            return "true" if v else "false"
        case Series(name, None) | Var(name):
            return name
        case Series(name, tf):
            return f'{name}("{tf}")'
        case Call(fn, args, tf):
            parts = [to_source(a) for a in args] + ([f'"{tf}"'] if tf else [])
            return f"{fn}({', '.join(parts)})"
        case Unary("-", operand):
            return f"(-{to_source(operand)})"
        case Unary(_, operand):
            return f"(not {to_source(operand)})"
        case Binary(op, left, right):
            return f"({to_source(left)} {op} {to_source(right)})"


# ---------------------------------------------------------------- analysis


def children(node: Node) -> tuple[Node, ...]:
    match node:
        case Call(_, args):
            return args
        case Unary(_, operand):
            return (operand,)
        case Binary(_, left, right):
            return (left, right)
        case _:
            return ()


def indicator_spec(call: Call) -> IndicatorSpec | None:
    if call.fn not in INDICATOR_FUNCS:
        return None
    nums = [a.value for a in call.args if isinstance(a, Num)]
    src = next((a.name for a in call.args if isinstance(a, Series)), "")
    period = int(nums[0])
    k = nums[1] if len(nums) > 1 else 0.0
    return IndicatorSpec(call.fn, src, period, k, call.tf)


def collect_indicators(node: Node) -> set[IndicatorSpec]:
    out: set[IndicatorSpec] = set()
    if isinstance(node, Call):
        spec = indicator_spec(node)
        if spec is not None:
            out.add(spec)
    for c in children(node):
        out |= collect_indicators(c)
    return out


def collect_series_tfs(node: Node) -> set[tuple[str, str]]:
    """Higher-timeframe price references ``(name, tf)`` such as ``close("4h")``."""
    out: set[tuple[str, str]] = set()
    if isinstance(node, Series) and node.tf is not None:
        out.add((node.name, node.tf))
    for c in children(node):
        out |= collect_series_tfs(c)
    return out


def collect_timeframes(node: Node) -> set[str]:
    """Every higher timeframe the expression reads."""
    return {sp.tf for sp in collect_indicators(node) if sp.tf} | {tf for _, tf in collect_series_tfs(node)}


def max_shift(node: Node) -> int:
    """Largest number of bars the expression looks back through ``prev``/``cross_*``."""
    inner = max((max_shift(c) for c in children(node)), default=0)
    if isinstance(node, Call):
        if node.fn == "prev":
            return inner + int(node.args[1].value) if isinstance(node.args[1], Num) else inner
        if node.fn in ("cross_above", "cross_below"):
            return inner + 1
    return inner


# ---------------------------------------------------------------- evaluation


class EvalContext(Protocol):
    def series(self, name: str, shift: int, tf: str | None = None) -> float | None: ...
    def indicator(self, spec: IndicatorSpec, shift: int) -> float | None: ...
    def var(self, name: str) -> float | bool | None: ...


def _num(v: float | bool | None) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    return float(v) if math.isfinite(v) else None


def evaluate(node: Node, ctx: EvalContext, shift: int = 0) -> float | bool | None:
    """Evaluate ``node`` ``shift`` bars in the past. Numeric -> ``float | None``; boolean -> ``bool``."""
    match node:
        case Num(v):
            return v
        case Bool(v):
            return v
        case Series(name, tf):
            return ctx.series(name, shift, tf)
        case Var(name):
            return ctx.var(name)
        case Unary("not", operand):
            return not bool(evaluate(operand, ctx, shift))
        case Unary(_, operand):
            neg = _num(evaluate(operand, ctx, shift))
            return None if neg is None else -neg
        case Binary("and", left, right):
            return bool(evaluate(left, ctx, shift)) and bool(evaluate(right, ctx, shift))
        case Binary("or", left, right):
            return bool(evaluate(left, ctx, shift)) or bool(evaluate(right, ctx, shift))
        case Binary(op, left, right):
            return _binary(op, _num(evaluate(left, ctx, shift)), _num(evaluate(right, ctx, shift)))
        case Call(fn, args):
            return _call(node, fn, args, ctx, shift)
    raise AssertionError(f"unhandled node {node!r}")  # pragma: no cover


def _binary(op: str, a: float | None, b: float | None) -> float | bool | None:
    if a is None or b is None:
        return None if op in ("+", "-", "*", "/") else False
    match op:
        case "+":
            return _finite(a + b)
        case "-":
            return _finite(a - b)
        case "*":
            return _finite(a * b)
        case "/":
            return None if b == 0 else _finite(a / b)
        case "<":
            return a < b
        case "<=":
            return a <= b
        case ">":
            return a > b
        case ">=":
            return a >= b
        case "==":
            return a == b
        case "!=":
            return a != b
    raise AssertionError(f"unhandled operator {op!r}")  # pragma: no cover


def _finite(v: float) -> float | None:
    return v if math.isfinite(v) else None


def _call(node: Call, fn: str, args: tuple[Node, ...], ctx: EvalContext, shift: int) -> float | bool | None:
    if fn in INDICATOR_FUNCS:
        spec = indicator_spec(node)
        assert spec is not None
        return ctx.indicator(spec, shift)
    if fn == "prev":
        n = args[1]
        assert isinstance(n, Num)
        return evaluate(args[0], ctx, shift + int(n.value))
    if fn in ("cross_above", "cross_below"):
        a0 = _num(evaluate(args[0], ctx, shift))
        b0 = _num(evaluate(args[1], ctx, shift))
        a1 = _num(evaluate(args[0], ctx, shift + 1))
        b1 = _num(evaluate(args[1], ctx, shift + 1))
        if a0 is None or b0 is None or a1 is None or b1 is None:
            return False
        return (a0 > b0 and a1 <= b1) if fn == "cross_above" else (a0 < b0 and a1 >= b1)
    x = _num(evaluate(args[0], ctx, shift))
    if fn == "coalesce":
        return x if x is not None else _num(evaluate(args[1], ctx, shift))
    if fn == "abs":
        return None if x is None else abs(x)
    y = _num(evaluate(args[1], ctx, shift))
    if x is None or y is None:
        return None
    return min(x, y) if fn == "min" else max(x, y)
