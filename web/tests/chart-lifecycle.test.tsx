import { afterAll, beforeAll, describe, expect, mock, spyOn, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import type { Candle } from "../src/api/types";
import { FakeChart, fakeMarkers } from "./fakeChart";

const sdk = {
  CandlestickSeries: { kind: "candle" },
  HistogramSeries: { kind: "histogram" },
  LineSeries: { kind: "line" },
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  createChart: () => new FakeChart(),
  createSeriesMarkers: (s: unknown, m: unknown[]) => fakeMarkers.create(s, m),
};
mock.module("lightweight-charts", () => sdk);

beforeAll(() => {
  GlobalRegistrator.register();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterAll(() => GlobalRegistrator.unregister());

const T0 = 1_700_000_000_000;
const candles = (n: number, start = T0): Candle[] =>
  Array.from({ length: n }, (_, i) => ({
    t: start + i * 3_600_000,
    o: 10,
    h: 12,
    l: 9,
    c: 11,
    v: 5,
  }));

async function setup() {
  const { StrictMode, createElement, act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { MarketChart } = await import("../src/components/MarketChart");
  const { planPanes, parseSpec, fillMarkers } = await import("../src/lib/market");
  FakeChart.reset();
  fakeMarkers.created = 0;
  fakeMarkers.detached = 0;
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const specs = (...t: string[]) => t.map((s) => parseSpec(s)).filter((s) => s !== null);
  const series = (n: number, start = T0) => ({
    "ema(20)": candles(n, start).map((c) => ({ t: c.t, value: 10 })),
    "bbands(20,2)": candles(n, start).map((c) => ({ t: c.t, upper: 12, mid: 10, lower: 8 })),
    "rsi(14)": candles(n, start).map((c) => ({ t: c.t, value: 50 })),
  });
  const show = async (p: {
    cs: Candle[];
    ind?: string[];
    bt?: boolean;
    start?: number;
  }) => {
    const plan = planPanes(specs(...(p.ind ?? [])), !!p.bt);
    const fills = p.bt
      ? fillMarkers(
          [
            { t: p.cs[2]?.t ?? T0, side: "buy", qty: 1, price: 10 },
            { t: p.cs[5]?.t ?? T0, side: "sell", qty: 1, price: 11 },
          ] as never,
          "1h",
        )
      : [];
    await act(async () => {
      root.render(
        createElement(
          StrictMode,
          null,
          createElement(MarketChart, {
            candles: p.cs,
            tf: "1h",
            plan: plan.items,
            data: series(p.cs.length, p.cs[0]?.t) as never,
            markers: fills,
            equity: p.bt ? p.cs.map((c) => ({ t: c.t, equity: 100 })) : undefined,
            equityPane: plan.equityPane,
            label: "test",
          }),
        ),
      );
    });
  };
  const unmount = () => act(async () => root.unmount());
  const live = () => FakeChart.all.filter((c) => !c.removed);
  return { show, unmount, live, host };
}

const kinds = (c: FakeChart) => [...c.live.values()].map((s) => `${s.kind}@${s.pane}`).sort();

describe("MarketChart lifecycle against a strict fake chart", () => {
  test("mount, switch instrument, backtest, indicators, unmount: never throws, one live chart", async () => {
    const err = spyOn(console, "error").mockImplementation(() => {});
    const t = await setup();
    await t.show({ cs: candles(30) });
    expect(t.live()).toHaveLength(1);
    expect(kinds(t.live()[0] as FakeChart)).toEqual(["candle@0", "histogram@0"]);

    // instrument switch: new candle window
    await t.show({ cs: candles(25, T0 + 86_400_000) });
    expect(t.live()).toHaveLength(1);
    expect(kinds(t.live()[0] as FakeChart)).toEqual(["candle@0", "histogram@0"]);

    // backtest select: markers plus equity pane
    await t.show({ cs: candles(25), bt: true });
    const afterBt = t.live()[0] as FakeChart;
    expect(kinds(afterBt)).toEqual(["candle@0", "histogram@0", "line@1"]);
    expect(fakeMarkers.created - fakeMarkers.detached).toBe(1);

    // indicator add: ema, bollinger on price; rsi in its own pane before the equity pane
    await t.show({ cs: candles(25), bt: true, ind: ["ema(20)", "bbands(20,2)", "rsi(14)"] });
    expect(kinds(t.live()[0] as FakeChart)).toEqual([
      "candle@0",
      "histogram@0",
      "line@0",
      "line@0",
      "line@0",
      "line@0",
      "line@1",
      "line@2",
    ]);

    // indicator remove: only ema stays
    await t.show({ cs: candles(25), ind: ["ema(20)"] });
    expect(kinds(t.live()[0] as FakeChart)).toEqual(["candle@0", "histogram@0", "line@0"]);

    await t.unmount();
    expect(t.live()).toHaveLength(0);
    for (const c of FakeChart.all) expect(c.removeCount).toBe(1);
    expect(fakeMarkers.created).toBe(fakeMarkers.detached);
    const thrown = err.mock.calls.map((c) => String(c[0])).filter((m) => /undefined|disposed/.test(m));
    expect(thrown).toEqual([]);
    err.mockRestore();
  });

  test("unmounting straight after mount disposes the chart once", async () => {
    const t = await setup();
    await t.show({ cs: candles(5) });
    await t.unmount();
    for (const c of FakeChart.all) {
      expect(c.removeCount).toBe(1);
      expect(c.live.size).toBe(0);
    }
  });
});

describe("chart scene ownership", () => {
  test("a disposed scene touches nothing and dispose is idempotent", async () => {
    const { createScene } = await import("../src/lib/chartScene");
    const { fakeDefs } = await import("./fakeChart");
    const input = {
      candles: candles(3),
      tf: "1h",
      plan: [],
      data: {},
      markers: [],
      equityPane: null,
      theme: { bg: "#fff", text: "#000", muted: "#555", border: "#ddd", pos: "#0a0", neg: "#a00", accent: "#0aa", vars: {} },
    };
    const chart = new FakeChart();
    const scene = createScene(chart as never, {
        defs: fakeDefs,
        styles: { solid: 0, dashed: 2, dotted: 1 },
        colorSolid: "solid",
        createMarkers: fakeMarkers.create,
      } as never);
    scene.render(input);
    expect(chart.live.size).toBe(2);
    scene.dispose();
    scene.dispose();
    expect(chart.removeCount).toBe(1);
    expect(() => scene.render(input)).not.toThrow();
    expect(chart.live.size).toBe(0);
  });
});
