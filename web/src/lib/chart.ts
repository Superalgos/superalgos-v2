import type { EquityPoint } from "../api/types";
import { fmtNum, localOffsetMin, utcLabel, zonedClock, zonedDate } from "./format";

export interface Box {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export interface LineGeometry {
  d: string;
  area: string;
  yTicks: { y: number; value: number; label: string }[];
  xTicks: { x: number; t: number; label: string }[];
  /** Extra axis caption, e.g. the local date and UTC offset when labels are times of day. */
  xContext: string | null;
  min: number;
  max: number;
  last: { x: number; y: number } | null;
}

export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
    out.push(Number(v.toPrecision(12)));
  }
  return out;
}

const MAX_TICK_DECIMALS = 10;

/**
 * Labels for y ticks with just enough decimals to tell neighbours apart: derived from the smallest gap
 * between ticks (step 0.5 -> 1 decimal, 0.02 -> 2, 20 -> 0), then widened until all labels differ. A tiny
 * range on a large value therefore reads "10,000.0002 / 10,000.0004", never "10,000 / 10,000".
 */
export function formatTickLabels(values: number[]): string[] {
  let gap = Number.POSITIVE_INFINITY;
  for (let i = 1; i < values.length; i++) {
    const d = Math.abs((values[i] ?? 0) - (values[i - 1] ?? 0));
    if (d > 0 && d < gap) gap = d;
  }
  let decimals = 0;
  if (Number.isFinite(gap)) {
    decimals = Math.min(MAX_TICK_DECIMALS, Math.max(0, Math.ceil(-Math.log10(gap) - 1e-9)));
  }
  let labels = values.map((v) => fmtNum(v, decimals));
  while (decimals < MAX_TICK_DECIMALS && new Set(labels).size < new Set(values).size) {
    decimals += 1;
    labels = values.map((v) => fmtNum(v, decimals));
  }
  return labels;
}

function yTickLabels(values: number[]): { value: number; label: string }[] {
  const labels = formatTickLabels(values);
  return values.map((value, i) => ({ value, label: labels[i] ?? String(value) }));
}

export function lineGeometry(points: EquityPoint[], box: Box): LineGeometry {
  if (points.length === 0) {
    return { d: "", area: "", yTicks: [], xTicks: [], xContext: null, min: 0, max: 0, last: null };
  }
  const sorted = [...points].sort((a, b) => a.t - b.t);
  const t0 = sorted[0]?.t ?? 0;
  const t1 = sorted[sorted.length - 1]?.t ?? t0;
  let min = Math.min(...sorted.map((p) => p.equity));
  let max = Math.max(...sorted.map((p) => p.equity));
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.05;
  min -= pad;
  max += pad;
  const iw = box.width - box.left - box.right;
  const ih = box.height - box.top - box.bottom;
  const x = (t: number) => box.left + (t1 === t0 ? iw / 2 : ((t - t0) / (t1 - t0)) * iw);
  const y = (v: number) => box.top + ih - ((v - min) / (max - min)) * ih;
  const ticks = [t0, t0 + (t1 - t0) / 2, t1];
  const axis = axisLabels(ticks);
  const pts = sorted.map((p) => ({ x: x(p.t), y: y(p.equity) }));
  const d = pts
    .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`)
    .join(" ");
  const base = box.top + ih;
  const first = pts[0];
  const last = pts[pts.length - 1];
  const area =
    first && last ? `${d} L${last.x.toFixed(1)},${base} L${first.x.toFixed(1)},${base} Z` : "";
  return {
    d,
    area,
    yTicks: yTickLabels(niceTicks(min, max, 4)).map((tick) => ({ ...tick, y: y(tick.value) })),
    xTicks: ticks.map((t, i) => ({ t, x: x(t), label: axis.labels[i] ?? "" })),
    xContext: axis.context,
    min,
    max,
    last: last ?? null,
  };
}

/** Returns the percent change from first to last point, or 0 when there is no baseline. */
export function totalReturnPct(points: EquityPoint[]): number {
  const first = points[0];
  const last = points[points.length - 1];
  if (!first || !last || first.equity === 0) return 0;
  return ((last.equity - first.equity) / first.equity) * 100;
}

// Axis labels are in the viewer's local zone; the zone's offset goes into the caption (`context`).
const date = (t: number) => zonedDate(t, localOffsetMin(t)); // YYYY-MM-DD
const clock = (t: number, seconds = false) => zonedClock(t, localOffsetMin(t), seconds);
const caption = (t: number) => `${date(t)} (${utcLabel(localOffsetMin(t))})`;

const AXIS_FORMATS: { name: string; fmt: (t: number) => string }[] = [
  { name: "hm", fmt: (t) => clock(t) },
  { name: "hms", fmt: (t) => clock(t, true) },
  { name: "dhm", fmt: (t) => `${date(t).slice(5)} ${clock(t)}` },
  { name: "date", fmt: (t) => date(t) },
];

/**
 * Labels for x ticks that adapt to the span: seconds under 10 minutes, time of day within one local day
 * (the date and zone offset go into `context`), "MM-DD HH:MM" up to 3 days, dates beyond. Never returns two
 * identical labels for distinct ticks: it falls back to finer formats until they differ.
 */
export function axisLabels(ticks: number[]): { labels: string[]; context: string | null } {
  const first = ticks[0];
  const last = ticks[ticks.length - 1];
  if (first === undefined || last === undefined) return { labels: [], context: null };
  const span = last - first;
  const sameDay = date(first) === date(last);
  const MIN = 60_000;
  const DAY = 86_400_000;
  const order =
    span < 10 * MIN && sameDay
      ? [1, 2]
      : span < DAY && sameDay
        ? [0, 1]
        : span < 3 * DAY
          ? [2, 1]
          : [3, 2, 1];
  const distinct = new Set(ticks).size;
  for (const idx of order) {
    const f = AXIS_FORMATS[idx];
    if (!f) continue;
    const labels = ticks.map(f.fmt);
    if (new Set(labels).size === distinct) {
      return {
        labels,
        context:
          f.name === "hm" || f.name === "hms"
            ? caption(first)
            : f.name === "dhm"
              ? `times in ${utcLabel(localOffsetMin(first))}`
              : null,
      };
    }
  }
  const f = AXIS_FORMATS[1];
  return {
    labels: ticks.map((t) => f?.fmt(t) ?? date(t)),
    context: sameDay ? caption(first) : null,
  };
}
