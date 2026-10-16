import { afterAll, beforeAll, describe, expect, mock, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import type { Candle, Provider, Signal } from "../src/api/types";
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
const sig = (i: number, at: number) =>
  ({
    id: `s${i}`,
    seq: i,
    instrument: "BTCUSDT",
    side: "buy",
    action: "open",
    size_fraction: 1,
    private: false,
    status: "out",
    anchored_ms: null,
    created_ms: at,
  }) satisfies Signal;
const provider = { pubkey: "p", name: "P", timeframe_s: 3600, tiers: [] } as unknown as Provider;
const candles: Candle[] = Array.from({ length: 30 }, (_, i) => ({
  t: T0 + i * HOUR,
  o: 10,
  h: 12,
  l: 9,
  c: 11,
  v: 5,
}));

describe("ProviderChart on a background refresh", () => {
  test("replay: a new signal that moves the candle window keeps the chart mounted, dimmed while loading", async () => {
    const gates: (() => void)[] = [];
    globalThis.fetch = (() =>
      new Promise<Response>((resolve) => {
        gates.push(() => resolve(new Response(JSON.stringify(candles))));
      })) as unknown as typeof fetch;
    const { createElement, act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { ProviderChart } = await import("../src/components/ProviderChart");
    FakeChart.reset();
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const show = (signals: Signal[]) =>
      act(async () => root.render(createElement(ProviderChart, { provider, signals, live: false })));
    const settle = () =>
      act(async () => {
        for (const g of gates.splice(0)) g();
        await new Promise((r) => setTimeout(r, 0));
      });

    await show([sig(1, T0)]);
    await settle();
    expect(FakeChart.all).toHaveLength(1);
    expect(host.querySelector(".mchart-badge")).toBeNull();

    await show([sig(1, T0), sig(2, T0 + 10 * HOUR)]); // newer signal: endMs, hence the key, changes
    expect(host.querySelector('[role="status"]')?.textContent).toBe("Loading…"); // the chart's badge
    expect(host.querySelector(".mchart-loading")).not.toBeNull();
    expect(FakeChart.all).toHaveLength(1);
    expect(FakeChart.all[0]?.removeCount).toBe(0);

    await settle();
    expect(FakeChart.all).toHaveLength(1);
    expect(FakeChart.all[0]?.removeCount).toBe(0);
    expect(host.querySelector(".mchart-loading")).toBeNull();
    await act(async () => root.unmount());
  });
});
