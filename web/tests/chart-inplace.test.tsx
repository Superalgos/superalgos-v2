import { afterAll, beforeAll, describe, expect, mock, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import type { Candle } from "../src/api/types";
import { FakeChart, fakeMarkers } from "./fakeChart";

mock.module("lightweight-charts", () => ({
  CandlestickSeries: { kind: "candle" },
  HistogramSeries: { kind: "histogram" },
  LineSeries: { kind: "line" },
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  createChart: () => new FakeChart(),
  createSeriesMarkers: (s: unknown, m: unknown[]) => fakeMarkers.create(s, m),
}));

const realFetch = globalThis.fetch;
beforeAll(() => {
  GlobalRegistrator.register();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterAll(() => {
  GlobalRegistrator.unregister();
  globalThis.fetch = realFetch;
});

const T0 = 1_700_000_000_000;
const HOUR = 3_600_000;
const candles = (n: number, step = HOUR, start = T0): Candle[] =>
  Array.from({ length: n }, (_, i) => ({ t: start + i * step, o: 10, h: 12, l: 9, c: 11, v: 5 }));

interface Gate {
  resolve(): void;
  reject(): void;
}

/** A fetch stub: every candles / indicators call waits on a gate the test opens. */
function stubApi() {
  const gates: Gate[] = [];
  const urls: string[] = [];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  globalThis.fetch = ((input: string) => {
    const url = String(input);
    urls.push(url);
    const q = new URL(url, "http://x").searchParams;
    const step = q.get("tf") === "4h" ? 4 * HOUR : HOUR;
    const n = q.get("instrument") === "ETHUSDT" ? 25 : 30;
    let body: unknown = [];
    if (url.startsWith("/api/market/candles")) body = candles(n, step);
    else if (url.startsWith("/api/market/indicators")) {
      const out: Record<string, unknown[]> = {};
      for (const spec of (q.get("spec") ?? "").split(",").filter(Boolean)) {
        out[spec] = candles(n, step).map((c) => ({ t: c.t, value: 10 }));
      }
      body = out;
    } else return Promise.resolve(json([]));
    return new Promise<Response>((resolve, reject) => {
      gates.push({
        resolve: () => resolve(json(body)),
        reject: () => reject(new Error("boom")),
      });
    });
  }) as unknown as typeof fetch;
  return { gates, urls };
}

async function page() {
  const { createElement, act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { ChartPage } = await import("../src/pages/Chart");
  const { parseSpec } = await import("../src/lib/market");
  FakeChart.reset();
  fakeMarkers.created = 0;
  fakeMarkers.detached = 0;
  const api = stubApi();
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const ind = (...s: string[]) => s.map((x) => parseSpec(x)).filter((x) => x !== null);
  const show = (state: { instrument: string; tf: "1h" | "4h"; ind?: string[] }) =>
    act(async () => {
      root.render(createElement(ChartPage, { state: { ...state, ind: ind(...(state.ind ?? [])) } }));
    });
  /** open every pending gate (the newest requests), then let React settle */
  const settle = async (mode: "resolve" | "reject" = "resolve") => {
    await act(async () => {
      const open = api.gates.splice(0);
      for (const g of open) g[mode]();
      await new Promise((r) => setTimeout(r, 0));
    });
  };
  const chart = () => FakeChart.all[0] as FakeChart;
  const kinds = () => [...chart().live.values()].map((s) => `${s.kind}@${s.pane}`).sort();
  return { show, settle, chart, kinds, host, api };
}

describe("the chart updates in place", () => {
  test("changing timeframe keeps one chart, shows the old candles while loading, then setData on the same series", async () => {
    const t = await page();
    await t.show({ instrument: "BTCUSDT", tf: "1h" });
    await t.settle();
    expect(FakeChart.all).toHaveLength(1);
    const price = [...t.chart().live.values()].find((s) => s.kind === "candle");
    expect(price?.data).toHaveLength(30);
    const before = price?.setDataCalls ?? 0;

    await t.show({ instrument: "BTCUSDT", tf: "4h" });
    // new candles still loading: chart alive, old data on screen, badge visible
    expect(FakeChart.all).toHaveLength(1);
    expect(t.chart().removeCount).toBe(0);
    expect(price?.setDataCalls).toBe(before);
    expect(t.host.querySelector(".mchart-badge")).not.toBeNull();
    expect(t.host.querySelector(".mchart-loading")).not.toBeNull();

    await t.settle();
    expect(FakeChart.all).toHaveLength(1);
    expect(t.chart().removeCount).toBe(0);
    expect(price?.setDataCalls).toBeGreaterThan(before);
    expect((price?.data as { time: number }[])[1]?.time).toBe((T0 + 4 * HOUR) / 1000);
    expect(t.host.querySelector(".mchart-badge")).toBeNull();
    expect(t.chart().calls.filter((c) => c.startsWith("remove:"))).toEqual([]);
  });

  test("changing instrument keeps the chart and series objects", async () => {
    const t = await page();
    await t.show({ instrument: "BTCUSDT", tf: "1h" });
    await t.settle();
    const ids = [...t.chart().live.keys()];
    await t.show({ instrument: "ETHUSDT", tf: "1h" });
    expect(FakeChart.all).toHaveLength(1);
    await t.settle();
    expect(FakeChart.all).toHaveLength(1);
    expect(t.chart().removeCount).toBe(0);
    expect([...t.chart().live.keys()]).toEqual(ids);
    const price = [...t.chart().live.values()].find((s) => s.kind === "candle");
    expect(price?.data).toHaveLength(25);
  });

  test("adding and removing an indicator adds / removes only that series", async () => {
    const t = await page();
    await t.show({ instrument: "BTCUSDT", tf: "1h" });
    await t.settle();
    const base = [...t.chart().live.keys()];
    await t.show({ instrument: "BTCUSDT", tf: "1h", ind: ["ema(20)"] });
    expect(t.chart().removeCount).toBe(0);
    await t.settle();
    expect(t.kinds()).toEqual(["candle@0", "histogram@0", "line@0"]);
    for (const id of base) expect(t.chart().live.has(id)).toBe(true);
    expect(t.chart().calls.filter((c) => c.startsWith("remove:"))).toEqual([]);

    await t.show({ instrument: "BTCUSDT", tf: "1h" });
    await t.settle();
    expect(t.kinds()).toEqual(["candle@0", "histogram@0"]);
    expect(t.chart().calls.filter((c) => c.startsWith("remove:"))).toEqual(["remove:line"]);
    expect(FakeChart.all).toHaveLength(1);
    expect(t.chart().removeCount).toBe(0);
  });

  test("a failed load shows inline and keeps the last good chart", async () => {
    const t = await page();
    await t.show({ instrument: "BTCUSDT", tf: "1h" });
    await t.settle();
    await t.show({ instrument: "BTCUSDT", tf: "4h" });
    await t.settle("reject");
    expect(t.host.querySelector('[role="alert"]')?.textContent).toContain("could not reach");
    expect(FakeChart.all).toHaveLength(1);
    expect(t.chart().removeCount).toBe(0);
    const price = [...t.chart().live.values()].find((s) => s.kind === "candle");
    expect(price?.data).toHaveLength(30);
    expect(t.host.querySelector(".mchart-badge")).toBeNull();
  });
});

describe("keeping the view on a timeframe change", () => {
  test("carryRange keeps the right edge and span, or stays pinned to the latest bar", async () => {
    const { carryRange } = await import("../src/lib/chartScene");
    // scrolled back: right edge 100 of last bar 500, span 60 -> same edge and span
    expect(carryRange({ from: 40, to: 100 }, 500, { first: 0, last: 600 })).toEqual({
      from: 40,
      to: 100,
    });
    // looking at the latest bar -> pinned to the new latest bar, same span
    expect(carryRange({ from: 440, to: 500 }, 500, { first: 0, last: 560 })).toEqual({
      from: 500,
      to: 560,
    });
    // old edge beyond the new data -> clamped to the new latest bar
    expect(carryRange({ from: 440, to: 480 }, 500, { first: 0, last: 450 })).toEqual({
      from: 410,
      to: 450,
    });
  });

  test("the scene applies it on a tf change and fits content otherwise", async () => {
    const { createScene } = await import("../src/lib/chartScene");
    const { fakeDefs } = await import("./fakeChart");
    const chart = new FakeChart();
    const scene = createScene(chart as never, {
      defs: fakeDefs,
      styles: { solid: 0, dashed: 2, dotted: 1 },
      colorSolid: "solid",
      createMarkers: fakeMarkers.create,
    } as never);
    const theme = { bg: "#fff", text: "#000", muted: "#555", border: "#ddd", pos: "#0a0", neg: "#a00", accent: "#0aa", vars: {} };
    const input = (tf: string, cs: Candle[]) => ({ candles: cs, tf, plan: [], data: {}, markers: [], equityPane: null, theme });
    const h1 = candles(100);
    const lastH1 = (h1[99]?.t ?? 0) / 1000;
    scene.render(input("1h", h1));
    expect(chart.fitCount).toBe(1);

    // user scrolled back: edge 20 bars before the end, 30 bars wide
    chart.visible = { from: lastH1 - 50 * 3600, to: lastH1 - 20 * 3600 };
    const h4 = candles(100, 4 * HOUR);
    scene.render(input("4h", h4));
    expect(chart.fitCount).toBe(1);
    expect(chart.visible).toEqual({ from: lastH1 - 50 * 3600, to: lastH1 - 20 * 3600 });

    // user at the latest bar -> pinned to the new latest bar
    chart.visible = { from: (h4[99]?.t ?? 0) / 1000 - 3600 * 100, to: (h4[99]?.t ?? 0) / 1000 };
    scene.render(input("1h", h1));
    const newLast = lastH1;
    expect(chart.visible?.to).toBe(newLast);
    expect(chart.visible?.to).not.toBe((h4[99]?.t ?? 0) / 1000);
    expect((chart.visible?.to ?? 0) - (chart.visible?.from ?? 0)).toBe(3600 * 100);
    scene.dispose();
  });
});
