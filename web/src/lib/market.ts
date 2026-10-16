// Pure logic of the market chart (F7): URL state, indicator specs and pane wiring, marker mapping,
// crosshair readout. No indicator maths lives here: values come from /api/market/indicators (SEP-8.2).

import type {
  BandPoint,
  Candle,
  Fill,
  IndicatorResponse,
  IndicatorSeries,
  Signal,
  Timeframe,
  ValuePoint,
} from "../api/types";
import { fmtNum, fmtTime } from "./format";

export const TIMEFRAMES: readonly Timeframe[] = ["1m", "5m", "15m", "1h", "4h", "1d"];

export const TF_MS: Record<Timeframe, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
  "4h": 14_400_000,
  "1d": 86_400_000,
};

export const DEFAULT_INSTRUMENT = "BTCUSDT";
export const DEFAULT_TF: Timeframe = "1h";
export const CANDLE_LIMIT = 500;

export function isTimeframe(s: string | null | undefined): s is Timeframe {
  return !!s && (TIMEFRAMES as readonly string[]).includes(s);
}

/** "BINANCE:BTCUSDT" and "btcusdt" both mean the market symbol "BTCUSDT". */
export function marketSymbol(instrument: string): string {
  const i = instrument.lastIndexOf(":");
  return (i >= 0 ? instrument.slice(i + 1) : instrument).trim().toUpperCase();
}

/** The chart timeframe closest to a provider's signal timeframe (seconds). */
export function nearestTimeframe(seconds: number): Timeframe {
  let best: Timeframe = DEFAULT_TF;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const tf of TIMEFRAMES) {
    const d = Math.abs(Math.log(TF_MS[tf] / 1000) - Math.log(Math.max(seconds, 1)));
    if (d < bestDist) {
      best = tf;
      bestDist = d;
    }
  }
  return best;
}

// ---------------------------------------------------------------- indicators

export type IndicatorKind = "ema" | "sma" | "bbands" | "rsi" | "atr";

interface ParamDef {
  name: string;
  def: number;
  min: number;
  max: number;
  integer: boolean;
}

interface KindDef {
  label: string;
  pane: boolean; // true: own sub-pane; false: overlay on the price pane
  params: ParamDef[];
}

const PERIOD = (def: number): ParamDef => ({
  name: "period",
  def,
  min: 2,
  max: 500,
  integer: true,
});

export const INDICATOR_KINDS: Record<IndicatorKind, KindDef> = {
  ema: { label: "EMA", pane: false, params: [PERIOD(20)] },
  sma: { label: "SMA", pane: false, params: [PERIOD(20)] },
  bbands: {
    label: "Bollinger",
    pane: false,
    params: [PERIOD(20), { name: "std dev", def: 2, min: 0.1, max: 10, integer: false }],
  },
  rsi: { label: "RSI", pane: true, params: [PERIOD(14)] },
  atr: { label: "ATR", pane: true, params: [PERIOD(14)] },
};

export const KIND_ORDER: IndicatorKind[] = ["ema", "sma", "bbands", "rsi", "atr"];

export interface IndicatorSpec {
  kind: IndicatorKind;
  params: number[];
}

function isKind(s: string): s is IndicatorKind {
  return Object.hasOwn(INDICATOR_KINDS, s);
}

export function defaultSpec(kind: IndicatorKind): IndicatorSpec {
  return { kind, params: INDICATOR_KINDS[kind].params.map((p) => p.def) };
}

/** Wire form sent to the API: "ema(20)", "bbands(20,2)". */
export function specString(s: IndicatorSpec): string {
  return `${s.kind}(${s.params.join(",")})`;
}

export function specLabel(s: IndicatorSpec): string {
  return `${INDICATOR_KINDS[s.kind].label} ${s.params.join(", ")}`;
}

export function isPaneIndicator(kind: IndicatorKind): boolean {
  return INDICATOR_KINDS[kind].pane;
}

/** Null when valid, otherwise a message saying which parameter is wrong and what range is allowed. */
export function specError(s: IndicatorSpec): string | null {
  const defs = INDICATOR_KINDS[s.kind].params;
  if (s.params.length !== defs.length) {
    return `${s.kind} takes ${defs.length} parameter(s), got ${s.params.length}. Use ${specString(defaultSpec(s.kind))} as a template.`;
  }
  for (const [i, d] of defs.entries()) {
    const v = s.params[i];
    if (v === undefined || !Number.isFinite(v)) {
      return `${s.kind} ${d.name} is not a number. Enter a value between ${d.min} and ${d.max}.`;
    }
    if (d.integer && !Number.isInteger(v)) {
      return `${s.kind} ${d.name} is ${v}: it must be a whole number between ${d.min} and ${d.max}.`;
    }
    if (v < d.min || v > d.max) {
      return `${s.kind} ${d.name} is ${v}: it must be between ${d.min} and ${d.max}.`;
    }
  }
  return null;
}

/** Parse "bbands(20,2)"; null for an unknown kind or invalid parameters. */
export function parseSpec(text: string): IndicatorSpec | null {
  const m = /^\s*([a-z]+)\(([^)]*)\)\s*$/i.exec(text);
  if (!m) return null;
  const kind = (m[1] ?? "").toLowerCase();
  if (!isKind(kind)) return null;
  const params = (m[2] ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter((x) => x !== "")
    .map(Number);
  const spec = { kind, params };
  return specError(spec) ? null : spec;
}

/** Decode leftover percent-escapes ("ema%2820%29", even doubly encoded); malformed escapes are kept as typed. */
function unescapeSpecText(text: string): string {
  let out = text;
  for (let i = 0; i < 3 && /%[0-9a-f]{2}/i.test(out); i++) {
    try {
      out = decodeURIComponent(out);
    } catch {
      break;
    }
  }
  return out;
}

/** Every valid spec in one `ind` value: "ema(20)", or a list "ema(20),bbands(20,2),rsi(14)". */
export function parseSpecList(text: string): IndicatorSpec[] {
  const out: IndicatorSpec[] = [];
  for (const piece of unescapeSpecText(text).match(/[a-z]+\([^)]*\)/gi) ?? []) {
    const s = parseSpec(piece);
    if (s) out.push(s);
  }
  return out;
}

/** Add a spec unless the same one is already on the chart. */
export function addSpec(list: IndicatorSpec[], s: IndicatorSpec): IndicatorSpec[] {
  const key = specString(s);
  return list.some((x) => specString(x) === key) ? list : [...list, s];
}

export function removeSpec(list: IndicatorSpec[], s: IndicatorSpec): IndicatorSpec[] {
  const key = specString(s);
  return list.filter((x) => specString(x) !== key);
}

// ---------------------------------------------------------------- pane wiring

export type Placement = { type: "overlay" } | { type: "pane"; pane: number };

export interface PlannedIndicator {
  spec: IndicatorSpec;
  key: string;
  placement: Placement;
  color: string; // CSS variable name, resolved by the view
}

export interface PanePlan {
  items: PlannedIndicator[];
  equityPane: number | null;
  paneCount: number;
}

const OVERLAY_COLORS = ["--c1", "--c2", "--c3", "--c4", "--c0"];
const PANE_COLORS = ["--c2", "--c1", "--c3"];

/**
 * Pane 0 is the price pane (candles, volume, overlays). Each RSI/ATR gets the next pane in the order
 * added; the backtest equity curve, when present, takes the pane after them.
 */
export function planPanes(specs: IndicatorSpec[], hasEquity: boolean): PanePlan {
  let next = 1;
  let overlays = 0;
  let panes = 0;
  const items = specs.map((spec): PlannedIndicator => {
    if (isPaneIndicator(spec.kind)) {
      const color = PANE_COLORS[panes++ % PANE_COLORS.length] ?? "--c2";
      return { spec, key: specString(spec), placement: { type: "pane", pane: next++ }, color };
    }
    const color = OVERLAY_COLORS[overlays++ % OVERLAY_COLORS.length] ?? "--c1";
    return { spec, key: specString(spec), placement: { type: "overlay" }, color };
  });
  const equityPane = hasEquity ? next++ : null;
  return { items, equityPane, paneCount: next };
}

// ---------------------------------------------------------------- series conversion

export interface LinePoint {
  time: number; // epoch seconds (UTC), as Lightweight Charts wants
  value: number;
}

export function toSeconds(ms: number): number {
  return Math.floor(ms / 1000);
}

export function candleBucket(ms: number, tf: Timeframe): number {
  const step = TF_MS[tf];
  return Math.floor(ms / step) * step;
}

export function isBand(p: ValuePoint | BandPoint): p is BandPoint {
  return "upper" in p;
}

export function lineData(series: IndicatorSeries | undefined): LinePoint[] {
  if (!series) return [];
  return (series as (ValuePoint | BandPoint)[]).flatMap((p) =>
    isBand(p) ? [] : [{ time: toSeconds(p.t), value: p.value }],
  );
}

export function bandData(series: IndicatorSeries | undefined): {
  upper: LinePoint[];
  mid: LinePoint[];
  lower: LinePoint[];
} {
  const out = { upper: [] as LinePoint[], mid: [] as LinePoint[], lower: [] as LinePoint[] };
  if (!series) return out;
  for (const p of series as (ValuePoint | BandPoint)[]) {
    if (!isBand(p)) continue;
    const time = toSeconds(p.t);
    out.upper.push({ time, value: p.upper });
    out.mid.push({ time, value: p.mid });
    out.lower.push({ time, value: p.lower });
  }
  return out;
}

/** Values of every planned indicator at candle open time `t` (ms), for the crosshair readout. */
export interface IndicatorReading {
  label: string;
  parts: { name: string; value: number }[];
}

export function readingsAt(
  plan: PlannedIndicator[],
  data: IndicatorResponse,
  t: number,
): IndicatorReading[] {
  const out: IndicatorReading[] = [];
  for (const it of plan) {
    const series = data[it.key] as (ValuePoint | BandPoint)[] | undefined;
    const p = series?.find((x) => x.t === t);
    if (!p) continue;
    out.push({
      label: specLabel(it.spec),
      parts: isBand(p)
        ? [
            { name: "upper", value: p.upper },
            { name: "mid", value: p.mid },
            { name: "lower", value: p.lower },
          ]
        : [{ name: "value", value: p.value }],
    });
  }
  return out;
}

export function priceDigits(price: number): number {
  const a = Math.abs(price);
  if (a >= 1000) return 2;
  if (a >= 1) return 3;
  return 6;
}

export function formatReadout(c: Candle, readings: IndicatorReading[]): string {
  const d = priceDigits(c.c);
  const base = `${fmtTime(c.t)} · O ${fmtNum(c.o, d)} H ${fmtNum(c.h, d)} L ${fmtNum(c.l, d)} C ${fmtNum(c.c, d)} · Vol ${fmtNum(c.v, 2)}`;
  const ind = readings.map(
    (r) =>
      `${r.label}: ${r.parts
        .map((p) => (r.parts.length === 1 ? fmtNum(p.value, 2) : `${p.name} ${fmtNum(p.value, 2)}`))
        .join(" ")}`,
  );
  return [base, ...ind].join(" · ");
}

// ---------------------------------------------------------------- markers

export type MarkerTone = "buy" | "sell" | "entry" | "exit";

export interface ChartMarker {
  id: string;
  /** exact event time, epoch ms */
  t: number;
  /** candle the marker sits on, epoch seconds */
  time: number;
  position: "aboveBar" | "belowBar";
  shape: "arrowUp" | "arrowDown" | "circle";
  tone: MarkerTone;
  text: string;
  tooltip: string;
}

function sidePlacement(side: "buy" | "sell"): Pick<ChartMarker, "position" | "shape"> {
  // buy below the bar pointing up, sell above pointing down
  return side === "buy"
    ? { position: "belowBar", shape: "arrowUp" }
    : { position: "aboveBar", shape: "arrowDown" };
}

export function signalTooltip(s: Signal): string {
  const anchor =
    s.anchored_ms === null
      ? "waiting for time-stamp"
      : `time-stamped ${fmtTime(s.anchored_ms)} (+${Math.round((s.anchored_ms - s.created_ms) / 1000)} s)`;
  const head = `#${s.seq} ${s.side} ${s.action} · size ${fmtNum(s.size_fraction * 100, 0)}% · ${anchor}`;
  return [head, `created ${fmtTime(s.created_ms)}`].join("\n");
}

/** One marker per signal of `instrument`, on the candle containing its created time (SEP-8.3). */
export function signalMarkers(signals: Signal[], instrument: string, tf: Timeframe): ChartMarker[] {
  const sym = marketSymbol(instrument);
  return signals
    .filter((s) => marketSymbol(s.instrument) === sym)
    .map((s) => ({
      id: s.id,
      t: s.created_ms,
      time: toSeconds(candleBucket(s.created_ms, tf)),
      ...sidePlacement(s.side),
      tone: s.side,
      text: `#${s.seq}`,
      tooltip: signalTooltip(s),
    }))
    .sort((a, b) => a.time - b.time || a.t - b.t);
}

/**
 * Entry or exit for every fill, by walking the signed position: a fill that moves the position away
 * from flat is an entry, one that moves it toward flat is an exit (a fill that crosses zero flips the
 * position, so it is reported as an entry).
 */
export function classifyFills(fills: Fill[]): { fill: Fill; kind: "entry" | "exit" }[] {
  let pos = 0;
  const out: { fill: Fill; kind: "entry" | "exit" }[] = [];
  const eps = 1e-12;
  for (const f of [...fills].sort((a, b) => a.t - b.t)) {
    const delta = f.side === "buy" ? f.qty : -f.qty;
    const reducing = pos !== 0 && Math.sign(delta) !== Math.sign(pos);
    const next = pos + delta;
    const flips = reducing && Math.abs(delta) > Math.abs(pos) + eps;
    out.push({ fill: f, kind: reducing && !flips ? "exit" : "entry" });
    pos = Math.abs(next) < eps ? 0 : next;
  }
  return out;
}

/** Backtest fills as entry/exit markers at the report's times and prices (SEP-8.5). */
export function fillMarkers(fills: Fill[], tf: Timeframe): ChartMarker[] {
  return classifyFills(fills).map(({ fill: f, kind }, i) => ({
    id: `fill-${i}-${f.t}`,
    t: f.t,
    time: toSeconds(candleBucket(f.t, tf)),
    ...sidePlacement(f.side),
    ...(kind === "exit" ? { shape: "circle" as const } : {}),
    tone: kind,
    text: kind === "entry" ? "In" : "Out",
    tooltip: `${kind === "entry" ? "Entry" : "Exit"} ${f.side} ${fmtNum(f.qty, 4)} @ ${fmtNum(f.price, priceDigits(f.price))}\n${fmtTime(f.t)}`,
  }));
}

/** Markers keyed by candle time, for the hover tooltip. */
export function markersByTime(markers: ChartMarker[]): Map<number, ChartMarker[]> {
  const m = new Map<number, ChartMarker[]>();
  for (const k of markers) {
    const list = m.get(k.time);
    if (list) list.push(k);
    else m.set(k.time, [k]);
  }
  return m;
}

/** Candle end for a view that should include `times`: the latest one plus a few bars of context. */
export function endMsFor(times: number[], tf: Timeframe, bars = 20): number | undefined {
  if (times.length === 0) return undefined;
  return candleBucket(Math.max(...times), tf) + bars * TF_MS[tf];
}

/**
 * Last candle the provider chart asks for (`end_ms`: candles opening at or before it).
 * Live (the relay runs in real time): now, so the forming candle is the last one.
 * Replay (time is scaled, signal times are market history rather than the wall clock): as `endMsFor`.
 */
export function providerChartEndMs(
  times: number[],
  tf: Timeframe,
  opts: { live: boolean; now: number },
): number | undefined {
  return opts.live ? opts.now : endMsFor(times, tf);
}

// ---------------------------------------------------------------- URL state

export interface ChartState {
  instrument: string;
  tf: Timeframe;
  ind: IndicatorSpec[];
  provider?: string;
  bt?: string;
}

export function defaultChartState(): ChartState {
  return { instrument: DEFAULT_INSTRUMENT, tf: DEFAULT_TF, ind: [] };
}

/** Parse the query of `#/chart?instrument=&tf=&ind=ema(20)&provider=&bt=`; bad values fall back to defaults. */
export function parseChartQuery(query: string): ChartState {
  const q = new URLSearchParams(query.replace(/^\?/, ""));
  const inst = marketSymbol(q.get("instrument") ?? "");
  const tf = q.get("tf");
  let ind: IndicatorSpec[] = [];
  for (const raw of q.getAll("ind")) {
    for (const s of parseSpecList(raw)) ind = addSpec(ind, s);
  }
  const state: ChartState = {
    instrument: inst || DEFAULT_INSTRUMENT,
    tf: isTimeframe(tf) ? tf : DEFAULT_TF,
    ind,
  };
  const provider = q.get("provider");
  const bt = q.get("bt");
  if (provider) state.provider = provider;
  if (bt) state.bt = bt;
  return state;
}

export function chartQuery(s: ChartState): string {
  const q = new URLSearchParams();
  q.set("instrument", s.instrument);
  q.set("tf", s.tf);
  for (const i of s.ind) q.append("ind", specString(i));
  if (s.provider) q.set("provider", s.provider);
  if (s.bt) q.set("bt", s.bt);
  return q.toString();
}

export function chartHref(s: Partial<ChartState> & { instrument: string }): string {
  return `#/chart?${chartQuery({ tf: DEFAULT_TF, ind: [], ...s })}`;
}

// ---------------------------------------------------------------- debounce

/** Trailing debounce; `cancel()` drops a pending call. */
export function debounce<A extends unknown[]>(
  fn: (...args: A) => void,
  ms: number,
): { (...args: A): void; cancel(): void } {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const wrapped = (...args: A) => {
    if (timer !== undefined) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = undefined;
      fn(...args);
    }, ms);
  };
  wrapped.cancel = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  return wrapped;
}

/**
 * Timeframe for a provider's mini chart: its own timeframe, or the next finer ones when the signals
 * are bunched together (a compressed demo replay) so that they spread over at least `minBars` candles.
 */
export function fitTimeframe(timeframeS: number, times: number[], minBars = 8): Timeframe {
  const own = nearestTimeframe(timeframeS);
  if (times.length < 2) return own;
  const span = Math.max(...times) - Math.min(...times);
  for (let i = TIMEFRAMES.indexOf(own); i >= 0; i--) {
    const tf = TIMEFRAMES[i] as Timeframe;
    if (span / TF_MS[tf] >= minBars) return tf;
  }
  return TIMEFRAMES[0] as Timeframe;
}

/** `end_ms` and `limit` that cover a backtest's [start, end] dates (YYYY-MM-DD, UTC) on its timeframe. */
export function backtestWindow(
  b: { start: string; end: string; timeframe: Timeframe },
  maxLimit = 1000,
): { endMs: number; limit: number } {
  const startMs = Date.parse(`${b.start}T00:00:00Z`);
  const endMs = Date.parse(`${b.end}T00:00:00Z`) + TF_MS["1d"];
  const bars = Math.ceil((endMs - startMs) / TF_MS[b.timeframe]) + 2;
  return { endMs, limit: Math.max(1, Math.min(maxLimit, bars)) };
}

/** The instrument most signals are on (market symbol), or null without signals. */
export function dominantInstrument(signals: { instrument: string }[]): string | null {
  const count = new Map<string, number>();
  for (const s of signals) {
    const k = marketSymbol(s.instrument);
    count.set(k, (count.get(k) ?? 0) + 1);
  }
  let best: string | null = null;
  let n = 0;
  for (const [k, c] of count) {
    if (c > n) {
      best = k;
      n = c;
    }
  }
  return best;
}
