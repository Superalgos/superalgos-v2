import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  type ISeriesApi,
  LineSeries,
  LineStyle,
  type SeriesMarker,
  type SeriesType,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Candle, EquityPoint, IndicatorResponse, Timeframe } from "../api/types";
import { type ChartLike, type ChartScene, createScene } from "../lib/chartScene";
import { chartTickLabel, chartTimeLabel, localOffsetMin, utcLabel } from "../lib/format";
import {
  type ChartMarker,
  formatReadout,
  markersByTime,
  type PlannedIndicator,
  readingsAt,
  toSeconds,
} from "../lib/market";

interface Theme {
  bg: string;
  text: string;
  muted: string;
  border: string;
  pos: string;
  neg: string;
  accent: string;
  vars: Record<string, string>;
}

function readTheme(): Theme {
  const cs = getComputedStyle(document.documentElement);
  const v = (n: string) => cs.getPropertyValue(n).trim();
  return {
    bg: v("--surface") || "#ffffff",
    text: v("--text") || "#1d2428",
    muted: v("--muted") || "#5b676d",
    border: v("--border") || "#d9dedb",
    pos: v("--pos") || "#0b6b3a",
    neg: v("--neg") || "#b3261e",
    accent: v("--accent") || "#0b6e6e",
    vars: Object.fromEntries(
      ["--c0", "--c1", "--c2", "--c3", "--c4"].map((n) => [n, v(n) || "#888888"]),
    ),
  };
}

/** Re-read the CSS palette when the OS switches between light and dark, as the rest of the app does. */
function useTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => setTheme(readTheme());
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return theme;
}

export interface MarketChartProps {
  candles: Candle[];
  tf: Timeframe;
  /** indicators placed on panes (see planPanes) and their values from the API */
  plan?: PlannedIndicator[];
  data?: IndicatorResponse;
  markers?: ChartMarker[];
  equity?: EquityPoint[];
  equityPane?: number | null;
  height?: number;
  label: string;
  /** newer data is loading: the chart stays as it is, dimmed, with a badge */
  loading?: boolean;
}

interface Hover {
  candle: Candle | null;
  marks: ChartMarker[];
  x: number;
  y: number;
}

export function MarketChart({
  candles,
  tf,
  plan = [],
  data = {},
  markers = [],
  equity,
  equityPane = null,
  height = 420,
  label,
  loading = false,
}: MarketChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const theme = useTheme();
  const [hover, setHover] = useState<Hover | null>(null);
  // The scene is bound to the chart object created by the mount effect below. It is state, not a
  // ref, so the drawing effect re-runs when a (StrictMode) remount creates a new chart.
  const [scene, setScene] = useState<ChartScene | null>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Normal },
      // times are shown in the viewer's local zone, the offset labelled in the crosshair label
      localization: { timeFormatter: chartTimeLabel },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderVisible: false,
        tickMarkFormatter: chartTickLabel,
      },
    });
    const created = createScene(chart as unknown as ChartLike, {
      defs: { candle: CandlestickSeries, histogram: HistogramSeries, line: LineSeries },
      styles: { solid: LineStyle.Solid, dashed: LineStyle.Dashed, dotted: LineStyle.Dotted },
      colorSolid: ColorType.Solid,
      createMarkers: (series, marks) =>
        createSeriesMarkers(
          series as unknown as ISeriesApi<SeriesType>,
          marks as SeriesMarker<UTCTimestamp>[],
        ),
    });
    setScene(created);
    return () => {
      created.dispose(); // removes its series, then the chart, once
      setScene((cur) => (cur === created ? null : cur));
    };
  }, []);

  const candleByTime = useMemo(() => new Map(candles.map((c) => [toSeconds(c.t), c])), [candles]);
  const marksByTime = useMemo(() => markersByTime(markers), [markers]);

  // redraw: the scene replaces the series it owns; there is no cleanup here by design
  useEffect(() => {
    scene?.render({
      candles,
      tf,
      plan,
      data,
      markers,
      equity,
      equityPane,
      theme,
      onCrosshair: (param) => {
        if (typeof param.time !== "number" || !param.point) {
          setHover(null);
          return;
        }
        setHover({
          candle: candleByTime.get(param.time) ?? null,
          marks: marksByTime.get(param.time) ?? [],
          x: param.point.x,
          y: param.point.y,
        });
      },
    });
  }, [
    scene,
    candles,
    tf,
    plan,
    data,
    markers,
    equity,
    equityPane,
    theme,
    candleByTime,
    marksByTime,
  ]);

  const shown = hover?.candle ?? candles[candles.length - 1] ?? null;
  const readout = shown ? formatReadout(shown, readingsAt(plan, data, shown.t)) : "No candles.";
  const w = host.current?.clientWidth ?? 400;

  return (
    <figure className="mchart">
      <p className="readout small" aria-live="off">
        {readout}
      </p>
      <div className={loading ? "mchart-wrap mchart-loading" : "mchart-wrap"} aria-busy={loading}>
        {loading && (
          <span className="mchart-badge small" role="status">
            Loading…
          </span>
        )}
        <div
          ref={host}
          className="mchart-host"
          role="img"
          aria-label={`${label}. ${candles.length} candles.`}
          style={{ height }}
        />
        {hover && hover.marks.length > 0 && (
          <div
            className="mchart-tip small"
            role="tooltip"
            style={{
              left: Math.max(4, Math.min(hover.x + 14, w - 250)),
              top: Math.max(4, Math.min(hover.y + 14, height - 40)),
            }}
          >
            {hover.marks.map((m) => (
              <pre key={m.id}>{m.tooltip}</pre>
            ))}
          </div>
        )}
      </div>
      {shown && (
        <p className="muted small">
          Times on the axis are your local time ({utcLabel(localOffsetMin(shown.t))}).
        </p>
      )}
      <figcaption className="sr">{label}</figcaption>
    </figure>
  );
}
