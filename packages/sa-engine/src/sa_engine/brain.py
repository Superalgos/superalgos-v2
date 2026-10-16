"""Pure-Python strategy state machine: bars and fills in, order commands out.

No Nautilus imports. The Nautilus adapter (:mod:`sa_engine.compile`) feeds it bars and fills, executes the
returned commands, and routes every ``Submit.intent`` through ``on_order_intent`` first.

Cycle: idle -> (trigger active and ``open.when``) -> each order is placed when its ``create`` event fires, cancelled
when its ``cancel`` event fires, and re-placed after a fill or cancel when it re-arms -> the cycle ends when flat with
nothing working and nothing left to place. Entry fills spawn per-fill take-profit (limit) and stop-loss (stop) exits
that cancel each other (OCO). The manage stage's phased stop and target are watched bar by bar (``on_exec_bar``) and
sold at market when touched.

Timing (see ARCHITECTURE.md, "Timing rules"):

1. Everything is decided on a bar close (``on_bar``): events, sizes, rates, phase moves and phase formulas.
2. A decision on bar N is never matched against bar N's own range. Resting orders (limit, stop) meet bar N+1 onward;
   a market order fills at the first price after the close of N. Stop and target levels computed at the close of N are
   first compared with the bars that close after N.
3. When one bar touches both the stop and the target level the stop wins.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from sa_engine.dsl import (
    IndicatorSpec,
    Node,
    collect_indicators,
    collect_series_tfs,
    evaluate,
    parse_bool,
    parse_num,
)
from sa_engine.events import CompiledEvent, CompiledSituation, first_match
from sa_engine.indicators import OHLCV, IndicatorBank
from sa_engine.intent import OrderIntent
from sa_engine.schema import Event, OrderSpec, StrategyDoc, check_semantics

EPS: Final = 1e-12
LOG_LIMIT: Final = 5000
MIN_FRACTION: Final = 1e-4  # an order smaller than 0.01% of equity is not placed (it would only be refused as dust)


@dataclass(frozen=True, slots=True)
class PositionState:
    qty: float = 0.0  # base units held (long-only)
    avg_price: float | None = None


@dataclass(frozen=True, slots=True)
class Submit:
    tag: str
    intent: OrderIntent
    reduce_only: bool


@dataclass(frozen=True, slots=True)
class Cancel:
    tag: str


Command = Submit | Cancel


@dataclass(frozen=True, slots=True)
class Fired:
    """Audit record: which situation of which event fired, on which bar close (``ts``, ms)."""

    ts: int
    event: str  # e.g. trigger.on, order:level_01.create, stop.phase:p1.next
    situation: str


@dataclass(slots=True)
class _Pending:
    kind: str  # entry | tp | sl | close | reduce | level_exit
    group: str | None = None  # OCO group (entry tag the exit belongs to)
    spec: OrderSpec | None = None
    fraction: float = 0.0  # entry: fraction of equity it commits


@dataclass(slots=True)
class _Slot:
    """One order of the open stage and where it is in its life this cycle."""

    spec: OrderSpec
    size: Node | None
    rate: Node | None
    create: CompiledEvent | None
    cancel: CompiledEvent | None
    state: Literal["idle", "working", "done"] = "idle"
    placed: int = 0


@dataclass(frozen=True, slots=True)
class _Phase:
    id: str
    level: Node
    next: CompiledEvent | None
    goto: tuple[tuple[CompiledEvent, int], ...]


@dataclass(slots=True)
class _Level:
    label: str  # stop | target
    phases: tuple[_Phase, ...]
    index: int = 0
    value: float | None = None
    armed_ts: int = -1  # close time of the bar that last set ``value``; only later bars are compared with it

    def reset(self) -> None:
        self.index, self.value, self.armed_ts = 0, None, -1


def compile_event(event: Event) -> CompiledEvent:
    return CompiledEvent(tuple(CompiledSituation(s.name, tuple(parse_bool(c) for c in s.all)) for s in event.any))


@dataclass(slots=True)
class _Ctx:
    bank: IndicatorBank
    pos: PositionState
    bars_in_position: int
    dust: float
    stop: _Level | None = None
    target: _Level | None = None

    def series(self, name: str, shift: int, tf: str | None = None) -> float | None:
        return self.bank.price(name, shift, tf)

    def indicator(self, spec: IndicatorSpec, shift: int) -> float | None:
        return self.bank.value(spec, shift)

    def var(self, name: str) -> float | bool | None:
        held = self.pos.qty > self.dust
        match name:
            case "in_position" | "is_long":
                return held
            case "is_short":
                return False
            case "position_size":
                return self.pos.qty
            case "bars_in_position":
                return float(self.bars_in_position)
            case "entry_price":
                return self.pos.avg_price if held else None
            case "stop_price":
                return self.stop.value if self.stop is not None and held else None
            case "target_price":
                return self.target.value if self.target is not None and held else None
            case "unrealized_pnl_pct":
                close = self.bank.price("close", 0)
                if not held or self.pos.avg_price in (None, 0) or close is None:
                    return None
                assert self.pos.avg_price is not None
                return (close / self.pos.avg_price - 1.0) * 100.0
        raise KeyError(name)  # pragma: no cover


@dataclass(slots=True)
class StrategyBrain:
    doc: StrategyDoc
    instrument: str  # VENUE:SYMBOL
    price_precision: int = 2
    tick: float | None = None  # price increment; emitted prices are rounded to it
    dust: float = EPS  # position quantities at or below this count as flat
    active: bool = False
    cycle: int = 0
    in_cycle: bool = False
    entries_closed: bool = False  # no more entries this cycle (trigger off, cancel_pending, close, stop/target hit)
    bars_in_position: int = 0
    fired: set[str] = field(default_factory=set)
    pending: dict[str, _Pending] = field(default_factory=dict)
    log: deque[Fired] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    _exit_seq: int = 0
    slots: dict[str, _Slot] = field(default_factory=dict)
    stop: _Level | None = None
    target: _Level | None = None
    bank: IndicatorBank = field(init=False)
    _events: dict[str, CompiledEvent] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_semantics(self.doc)
        stages = self.doc.stages
        self._events["trigger.on"] = compile_event(stages.trigger.on)
        if stages.trigger.off is not None:
            self._events["trigger.off"] = compile_event(stages.trigger.off)
        self._events["open.when"] = compile_event(stages.open.when)
        if stages.close is not None:
            self._events["close.when"] = compile_event(stages.close.when)
        for spec in stages.open.orders:
            self.slots[spec.id] = _Slot(
                spec,
                parse_num(spec.size) if spec.size else None,
                parse_num(spec.rate) if spec.rate else None,
                compile_event(spec.create) if spec.create else None,
                compile_event(spec.cancel) if spec.cancel else None,
            )
        for rule in stages.manage.rules:
            self._events[f"rule:{rule.id}"] = compile_event(rule.when)
        for label, level in (("stop", stages.manage.stop), ("target", stages.manage.target)):
            if level is None:
                continue
            index = {p.id: i for i, p in enumerate(level.phases)}
            phases = tuple(
                _Phase(
                    p.id,
                    parse_num(p.level),
                    compile_event(p.next) if p.next else None,
                    tuple((compile_event(g.when), index[g.to]) for g in p.goto),
                )
                for p in level.phases
            )
            setattr(self, label, _Level(label, phases))
        specs: set[IndicatorSpec] = set()
        series_tfs: set[tuple[str, str]] = set()
        for e in self.doc.expressions():
            node = parse_bool(e.source) if e.kind == "bool" else parse_num(e.source)
            specs |= collect_indicators(node)
            series_tfs |= collect_series_tfs(node)
        self.bank = IndicatorBank(specs, self.doc.timeframe_seconds, series_tfs)

    # ------------------------------------------------------------ helpers
    def _fire(self, name: str, event: CompiledEvent, ctx: _Ctx, ts: int) -> bool:
        situation = first_match(event, ctx)
        if situation is None:
            return False
        self.log.append(Fired(ts, name, situation))
        return True

    def _num(self, node: Node, ctx: _Ctx) -> float | None:
        v = evaluate(node, ctx)
        return None if v is None or isinstance(v, bool) else float(v)

    def _intent(
        self,
        *,
        side: str,
        action: str,
        fraction: float,
        order_type: str,
        ts: int,
        limit: float | None = None,
        stop: float | None = None,
    ) -> OrderIntent:
        return OrderIntent.model_validate(
            {
                "instrument": self.instrument,
                "side": side,
                "action": action,
                "size_fraction": min(1.0, fraction),
                "order_type": order_type,
                "limit_price": limit,
                "stop_price": stop,
                "timeframe": self.doc.timeframe_seconds,
                "ts": ts,
            }
        )

    def _px(self, value: float) -> float:
        if self.tick:
            tick = Decimal(str(self.tick))
            return float((Decimal(str(value)) / tick).to_integral_value(ROUND_HALF_EVEN) * tick)
        return round(value, self.price_precision)

    def _entry_tags(self) -> list[str]:
        return [t for t, p in self.pending.items() if p.kind == "entry"]

    def _exiting(self) -> bool:
        return any(p.kind in ("close", "level_exit") for p in self.pending.values())

    def _waiting(self) -> bool:
        """An order of this cycle is still to be placed (so the cycle must not end yet)."""
        return not self.entries_closed and any(s.state == "idle" for s in self.slots.values())

    def _levels(self) -> list[_Level]:
        return [lv for lv in (self.stop, self.target) if lv is not None]

    # ------------------------------------------------------------ events
    def on_bar(self, bar: OHLCV, pos: PositionState, equity: float | None = None) -> list[Command]:
        """A strategy-timeframe bar has closed. ``equity`` (quote currency) enables the 100%-of-equity cap."""
        self.bank.update(bar)
        held = pos.qty > self.dust
        self.bars_in_position = self.bars_in_position + 1 if held else 0
        if not held:
            for lv in self._levels():
                lv.reset()
        ctx = _Ctx(self.bank, pos, self.bars_in_position, self.dust, self.stop, self.target)
        cmds: list[Command] = []

        if not self.active:
            self.active = self._fire("trigger.on", self._events["trigger.on"], ctx, bar.ts)
        elif "trigger.off" in self._events and self._fire("trigger.off", self._events["trigger.off"], ctx, bar.ts):
            self.active = False
            self.entries_closed = True
            cmds += [Cancel(t) for t in self._entry_tags()]

        if self.in_cycle or held or self.pending:  # a position the strategy did not open (a restart) is managed too
            cmds += self._manage(bar, ctx, pos)
        if self.in_cycle and not held and not self.pending and not self._waiting():
            self._end_cycle()
        if not self.in_cycle and self.active and self._fire("open.when", self._events["open.when"], ctx, bar.ts):
            self._start_cycle()
        if self.in_cycle:
            cmds += self._place(bar, ctx, pos, equity)
        return cmds

    def _start_cycle(self) -> None:
        self.cycle += 1
        self.in_cycle = True
        self.entries_closed = False
        self.fired.clear()
        for s in self.slots.values():
            s.state, s.placed = "idle", 0

    def _end_cycle(self) -> None:
        self.in_cycle = False
        self.entries_closed = False
        self.fired.clear()

    def _place(self, bar: OHLCV, ctx: _Ctx, pos: PositionState, equity: float | None) -> list[Command]:
        if not self.active or self.entries_closed or self._exiting():
            return []
        held = pos.qty > self.dust
        exposure = pos.qty * bar.close / equity if held and equity else 0.0
        committed = exposure + sum(p.fraction for p in self.pending.values() if p.kind == "entry")
        cmds: list[Command] = []
        for slot in self.slots.values():
            spec = slot.spec
            if slot.state != "idle":
                continue
            if slot.create is not None and not self._fire(f"order:{spec.id}.create", slot.create, ctx, bar.ts):
                continue
            fraction = spec.size_fraction if slot.size is None else self._num(slot.size, ctx)
            if fraction is None:
                continue
            fraction = min(fraction, 1.0 - committed)  # v1 "Order Size Shrinked": never beyond 100% of equity
            if fraction < MIN_FRACTION:
                continue
            limit = stop = None
            if spec.type != "market":
                if slot.rate is not None:
                    price = self._num(slot.rate, ctx)
                    if price is None or price <= 0:
                        continue
                    price = min(price, bar.close) if spec.type == "limit" else max(price, bar.close)
                else:
                    price = bar.close * (1 + spec.offset_pct / 100)
                if spec.type == "limit":
                    limit = self._px(price)
                else:
                    stop = self._px(price)
            slot.placed += 1
            tag = f"{spec.id}#{self.cycle}" if slot.placed == 1 else f"{spec.id}#{self.cycle}.{slot.placed}"
            intent = self._intent(
                side="buy", action="open", fraction=fraction, order_type=spec.type, ts=bar.ts, limit=limit, stop=stop
            )
            slot.state = "working"
            committed += fraction
            self.pending[tag] = _Pending("entry", spec=spec, fraction=fraction)
            cmds.append(Submit(tag, intent, reduce_only=False))
        return cmds

    def _manage(self, bar: OHLCV, ctx: _Ctx, pos: PositionState) -> list[Command]:
        stages = self.doc.stages
        cmds: list[Command] = []
        held = pos.qty > self.dust
        closing = self._exiting()
        if (
            "close.when" in self._events
            and not closing
            and self._fire("close.when", self._events["close.when"], ctx, bar.ts)
        ):
            cmds += [Cancel(t) for t in list(self.pending)]
            self.entries_closed = True
            if held:
                tag = f"close#{self.cycle}"
                self.pending[tag] = _Pending("close")
                cmds.append(self._close_all(tag, bar.ts))
            return cmds
        for slot in self.slots.values():
            working = self._working_tag(slot)
            if working is None or slot.cancel is None or closing:
                continue
            if self._fire(f"order:{slot.spec.id}.cancel", slot.cancel, ctx, bar.ts):
                cmds.append(Cancel(working))
        if held and not closing:
            for lv in self._levels():
                self._advance(lv, ctx, bar.ts)
        for rule in stages.manage.rules:
            if rule.once and rule.id in self.fired:
                continue
            if not self._fire(f"rule:{rule.id}", self._events[f"rule:{rule.id}"], ctx, bar.ts):
                continue
            if rule.action == "cancel_pending":
                targets = self._entry_tags()
                if not targets and not self._waiting():
                    continue
                cmds += [Cancel(t) for t in targets]
                self.entries_closed = True
            else:
                if not held or closing:
                    continue
                assert rule.size_fraction is not None
                self._exit_seq += 1
                tag = f"{rule.id}.reduce{self._exit_seq}"
                self.pending[tag] = _Pending("reduce")
                intent = self._intent(
                    side="sell", action="close", fraction=rule.size_fraction, order_type="market", ts=bar.ts
                )
                cmds.append(Submit(tag, intent, True))
            self.fired.add(rule.id)
        return cmds

    def _working_tag(self, slot: _Slot) -> str | None:
        if slot.state != "working":
            return None
        return next((t for t, p in self.pending.items() if p.kind == "entry" and p.spec is slot.spec), None)

    def _close_all(self, tag: str, ts: int) -> Submit:
        intent = self._intent(side="sell", action="close", fraction=1.0, order_type="market", ts=ts)
        return Submit(tag, intent, True)

    # ------------------------------------------------------------ phases
    def _advance(self, lv: _Level, ctx: _Ctx, ts: int) -> None:
        """v1 order: this phase's next event, else its move-to events in order (at most one move per bar), then the
        formula of the phase that is current afterwards sets the level."""
        phase = lv.phases[lv.index]
        moved = False
        if phase.next is not None and lv.index + 1 < len(lv.phases):
            moved = self._fire(f"{lv.label}.phase:{phase.id}.next", phase.next, ctx, ts)
            if moved:
                lv.index += 1
        if not moved:
            for n, (event, to) in enumerate(phase.goto):
                if self._fire(f"{lv.label}.phase:{phase.id}.goto[{n}]", event, ctx, ts):
                    lv.index = to
                    break
        value = self._num(lv.phases[lv.index].level, ctx)
        if value is not None:
            lv.value = value if value > 0 else None  # 0 is v1's "no stop"
            lv.armed_ts = ts

    def on_exec_bar(self, bar: OHLCV, pos: PositionState) -> list[Command]:
        """A bar finer than (or equal to) the strategy's has closed: compare it with the stop and target levels.

        Only levels armed by an *earlier* bar close are compared; the stop is tested first, so a bar that touches both
        sells on the stop. The position is sold at market: the order is decided here and fills from the next price.
        """
        if not self.doc.needs_exec_bars or pos.qty <= self.dust or self._exiting():
            return []
        hit: str | None = None
        for lv in self._levels():  # stop first, then target
            if lv.value is None or lv.armed_ts >= bar.ts:
                continue
            touched = bar.low <= lv.value if lv.label == "stop" else bar.high >= lv.value
            if touched:
                hit = lv.label
                break
        if hit is None:
            return []
        self.log.append(Fired(bar.ts, f"{hit}.hit", hit))
        self.entries_closed = True
        cmds: list[Command] = [Cancel(t) for t in list(self.pending)]
        tag = f"{hit}#{self.cycle}"
        self.pending[tag] = _Pending("level_exit")
        cmds.append(self._close_all(tag, bar.ts))
        return cmds

    # ------------------------------------------------------------ fills
    def on_fill(
        self, tag: str, price: float, qty: float, pos_after: PositionState, ts: int, *, done: bool
    ) -> list[Command]:
        """An order (fully or partly) filled. ``done`` = the order has no remaining quantity."""
        p = self.pending.get(tag)
        if p is None:
            return []
        cmds: list[Command] = []
        if done:
            del self.pending[tag]
            if p.kind == "entry" and p.spec is not None:
                self._slot_free(p.spec)
        if p.kind == "entry" and p.spec is not None and pos_after.qty > self.dust:
            fraction = min(1.0, qty / pos_after.qty)
            self._exit_seq += 1
            seq = self._exit_seq
            if p.spec.take_profit_pct is not None:
                etag = f"{tag}.tp{seq}"
                limit = self._px(price * (1 + p.spec.take_profit_pct / 100))
                self.pending[etag] = _Pending("tp", group=tag)
                intent = self._intent(
                    side="sell", action="close", fraction=fraction, order_type="limit", ts=ts, limit=limit
                )
                cmds.append(Submit(etag, intent, reduce_only=True))
            if p.spec.stop_loss_pct is not None:
                etag = f"{tag}.sl{seq}"
                stop = self._px(price * (1 - p.spec.stop_loss_pct / 100))
                self.pending[etag] = _Pending("sl", group=tag)
                intent = self._intent(
                    side="sell", action="close", fraction=fraction, order_type="stop", ts=ts, stop=stop
                )
                cmds.append(Submit(etag, intent, reduce_only=True))
            self._link(cmds)
        elif p.kind in ("tp", "sl") and done:
            sibling = self._sibling(tag, p)
            if sibling is not None:
                cmds.append(Cancel(sibling))
        return cmds

    def _slot_free(self, spec: OrderSpec, *, rejected: bool = False) -> None:
        """An entry order is over (filled, cancelled or refused): it may be placed again only if it re-arms."""
        slot = self.slots[spec.id]
        slot.state = "idle" if spec.rearm and not rejected else "done"

    def _link(self, cmds: list[Command]) -> None:
        """Give a tp/sl pair spawned by one fill a shared OCO group (their common sequence number)."""
        subs = [c.tag for c in cmds if isinstance(c, Submit)]
        if len(subs) == 2:
            for t in subs:
                self.pending[t].group = "|".join(subs)

    def _sibling(self, tag: str, p: _Pending) -> str | None:
        if p.group is None or "|" not in p.group:
            return None
        return next((t for t in p.group.split("|") if t != tag and t in self.pending), None)

    def on_order_end(self, tag: str, *, rejected: bool = False) -> None:
        """Order cancelled / rejected / expired without further fills. ``rejected``: refused by venue or sizing."""
        p = self.pending.pop(tag, None)
        if p is not None and p.kind == "entry" and p.spec is not None:
            self._slot_free(p.spec, rejected=rejected)
