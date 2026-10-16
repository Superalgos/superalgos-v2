// The drawing side of the market chart, written against a minimal chart interface so its lifecycle
// can be tested with a fake chart. A scene is bound to ONE chart object: it owns every series and
// marker plugin it creates, removes them only while that chart is alive, and disposes the chart
// exactly once. Once disposed it ignores further calls, so a stale reference can never throw.

import type { Candle, EquityPoint, IndicatorResponse } from "../api/types";
import {
  bandData,
  type ChartMarker,
  lineData,
  type MarkerTone,
  type PlannedIndicator,
  toSeconds,
} from "./market";

export interface SeriesLike {
  setData(data: unknown[]): void;
  applyOptions(o: object): void;
  priceScale(): { applyOptions(o: object): void };
  createPriceLine(o: object): unknown;
}

export interface CrosshairParam {
  time?: unknown;
  point?: { x: number; y: number };
}

export interface TimeRange {
  from: number;
  to: number;
}

export interface TimeScaleLike {
  fitContent(): void;
  getVisibleRange(): TimeRange | null;
  setVisibleRange(r: TimeRange): void;
}

export interface ChartLike {
  addSeries(def: unknown, options: object, pane?: number): SeriesLike;
  removeSeries(series: SeriesLike): void;
  panes(): { setStretchFactor(n: number): void }[];
  timeScale(): TimeScaleLike;
  applyOptions(o: object): void;
  subscribeCrosshairMove(h: (p: CrosshairParam) => void): void;
  unsubscribeCrosshairMove(h: (p: CrosshairParam) => void): void;
  remove(): void;
}

/** The library values the scene needs; injected so tests do not need the real library. */
export interface MarkersLike {
  setMarkers(marks: object[]): void;
  detach(): void;
}

export interface SceneDeps {
  defs: { candle: unknown; histogram: unknown; line: unknown };
  styles: { solid: number; dashed: number; dotted: number };
  colorSolid: unknown;
  createMarkers(series: SeriesLike, marks: object[]): MarkersLike;
}

export interface SceneTheme {
  bg: string;
  text: string;
  muted: string;
  border: string;
  pos: string;
  neg: string;
  accent: string;
  vars: Record<string, string>;
}

export interface SceneInput {
  candles: Candle[];
  tf: string;
  plan: PlannedIndicator[];
  data: IndicatorResponse;
  markers: ChartMarker[];
  equity?: EquityPoint[] | undefined;
  equityPane: number | null;
  theme: SceneTheme;
  onCrosshair?: ((p: CrosshairParam) => void) | undefined;
}

export interface ChartScene {
  render(input: SceneInput): void;
  dispose(): void;
  readonly disposed: boolean;
}

/**
 * Where the visible window goes after a timeframe change. The right-edge time and the time span are
 * kept, so the view does not jump; if the user was looking at the latest bar (right edge at or
 * past it) the view stays pinned to the new latest bar. Times are in seconds.
 */
export function carryRange(
  prev: TimeRange,
  prevLast: number,
  next: { first: number; last: number },
): TimeRange {
  const span = Math.max(0, prev.to - prev.from);
  const pinned = prev.to >= prevLast;
  const to = pinned ? next.last : Math.min(Math.max(prev.to, next.first), next.last);
  return { from: to - span, to };
}

interface Desc {
  key: string;
  def: unknown;
  options: object;
  pane: number;
  /** structural signature: when it changes the series is rebuilt instead of updated */
  sig: string;
  data: unknown[];
  after?: (s: SeriesLike) => void;
}

const toneColor = (t: MarkerTone, th: SceneTheme) =>
  t === "buy" || t === "entry" ? th.pos : th.neg;
const points = (pts: { time: number; value: number }[]) =>
  pts.map((p) => ({ time: p.time, value: p.value }));

export function createScene(chart: ChartLike, deps: SceneDeps): ChartScene {
  let disposed = false;
  const owned = new Map<string, { series: SeriesLike; sig: string }>();
  let plugin: MarkersLike | null = null;
  let crosshair: ((p: CrosshairParam) => void) | null = null;
  let fitted = "";
  let lastTf = "";
  let lastCandle: number | null = null;

  const drop = (key: string) => {
    const o = owned.get(key);
    if (!o) return;
    owned.delete(key);
    chart.removeSeries(o.series);
  };

  /** Make the chart hold exactly `wanted`: update what is there, add what is new, drop the rest. */
  const sync = (wanted: Desc[]) => {
    const keep = new Set(wanted.map((d) => d.key));
    for (const key of [...owned.keys()]) if (!keep.has(key)) drop(key);
    for (const d of wanted) {
      let o = owned.get(d.key);
      if (o && o.sig !== d.sig) {
        drop(d.key);
        o = undefined;
      }
      if (o) {
        o.series.applyOptions(d.options);
        o.series.setData(d.data);
        continue;
      }
      const series = chart.addSeries(d.def, d.options, d.pane);
      owned.set(d.key, { series, sig: d.sig });
      d.after?.(series);
      series.setData(d.data);
    }
  };

  const clear = () => {
    if (crosshair) chart.unsubscribeCrosshairMove(crosshair);
    crosshair = null;
    plugin?.detach();
    plugin = null;
    for (const key of [...owned.keys()]) drop(key);
  };

  return {
    get disposed() {
      return disposed;
    },
    render(input) {
      if (disposed) return;
      const { theme, candles, plan, data, markers, equity, equityPane } = input;
      const first = candles[0] ? toSeconds(candles[0].t) : null;
      const last = candles.length > 0 ? toSeconds(candles[candles.length - 1]?.t ?? 0) : null;
      // read the window before new data replaces it
      const prevRange =
        lastTf && lastTf !== input.tf && lastCandle !== null
          ? chart.timeScale().getVisibleRange()
          : null;

      if (crosshair) chart.unsubscribeCrosshairMove(crosshair);
      crosshair = null;

      chart.applyOptions({
        layout: {
          background: { type: deps.colorSolid, color: theme.bg },
          textColor: theme.muted,
          panes: { separatorColor: theme.border },
        },
        grid: { vertLines: { color: theme.border }, horzLines: { color: theme.border } },
        rightPriceScale: { borderColor: theme.border },
        timeScale: { borderColor: theme.border },
      });

      const pick = (name: string) => theme.vars[name] ?? theme.accent;
      const wanted: Desc[] = [];
      wanted.push({
        key: "price",
        def: deps.defs.candle,
        options: {
          upColor: theme.pos,
          downColor: theme.neg,
          wickUpColor: theme.pos,
          wickDownColor: theme.neg,
          borderVisible: false,
        },
        pane: 0,
        sig: "price",
        data: candles.map((c) => ({
          time: toSeconds(c.t),
          open: c.o,
          high: c.h,
          low: c.l,
          close: c.c,
        })),
        after: (s) => s.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } }),
      });
      wanted.push({
        key: "volume",
        def: deps.defs.histogram,
        options: { priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false },
        pane: 0,
        sig: "volume",
        data: candles.map((c) => ({
          time: toSeconds(c.t),
          value: c.v,
          color: `${c.c >= c.o ? theme.pos : theme.neg}66`,
        })),
        after: (s) => s.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } }),
      });

      for (const it of plan) {
        const series = data[it.key];
        const color = pick(it.color);
        const pane = it.placement.type === "pane" ? it.placement.pane : 0;
        if (it.spec.kind === "bbands") {
          const b = bandData(series);
          const parts = [
            ["upper", b.upper, deps.styles.solid],
            ["mid", b.mid, deps.styles.dashed],
            ["lower", b.lower, deps.styles.solid],
          ] as const;
          for (const [name, pts, style] of parts) {
            wanted.push({
              key: `ind:${it.key}:${name}`,
              def: deps.defs.line,
              options: {
                color,
                lineWidth: 1,
                lineStyle: style,
                lastValueVisible: false,
                priceLineVisible: false,
              },
              pane,
              sig: `line@${pane}`,
              data: points(pts),
            });
          }
          continue;
        }
        const isRsi = it.spec.kind === "rsi";
        wanted.push({
          key: `ind:${it.key}`,
          def: deps.defs.line,
          options: { color, lineWidth: 2, priceLineVisible: false, title: it.key },
          pane,
          sig: `line@${pane}${isRsi ? `|${theme.muted}` : ""}`,
          data: points(lineData(series)),
          ...(isRsi
            ? {
                after: (s: SeriesLike) => {
                  for (const level of [30, 70]) {
                    s.createPriceLine({
                      price: level,
                      color: theme.muted,
                      lineWidth: 1,
                      lineStyle: deps.styles.dotted,
                      axisLabelVisible: false,
                      title: "",
                    });
                  }
                },
              }
            : {}),
        });
      }

      if (equity && equity.length > 0 && equityPane !== null) {
        wanted.push({
          key: "equity",
          def: deps.defs.line,
          options: { color: theme.accent, lineWidth: 2, priceLineVisible: false, title: "equity" },
          pane: equityPane,
          sig: `line@${equityPane}`,
          data: equity.map((p) => ({ time: toSeconds(p.t), value: p.equity })),
        });
      }

      sync(wanted);

      const marks = markers.map((m) => ({
        time: m.time,
        position: m.position,
        shape: m.shape,
        color: toneColor(m.tone, theme),
        text: m.text,
        id: m.id,
      }));
      const price = owned.get("price")?.series;
      if (plugin) plugin.setMarkers(marks);
      else if (price) plugin = deps.createMarkers(price, marks);

      const panes = chart.panes();
      panes[0]?.setStretchFactor(3);
      for (const p of panes.slice(1)) p.setStretchFactor(1);

      if (candles.length > 0 && first !== null && last !== null) {
        const key = `${input.tf}|${first}|${last}|${candles.length}`;
        if (key !== fitted) {
          fitted = key;
          if (prevRange && lastCandle !== null) {
            chart.timeScale().setVisibleRange(carryRange(prevRange, lastCandle, { first, last }));
          } else {
            chart.timeScale().fitContent();
          }
        }
        lastTf = input.tf;
        lastCandle = last;
      }

      if (input.onCrosshair) {
        crosshair = input.onCrosshair;
        chart.subscribeCrosshairMove(crosshair);
      }
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      try {
        clear();
      } finally {
        chart.remove();
      }
    },
  };
}
