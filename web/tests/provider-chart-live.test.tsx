import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, jest, mock, test } from "bun:test";
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

const HOUR = 3_600_000;
const SIGNAL_AT = Date.UTC(2026, 0, 1, 10, 30);
const signal = {
  id: "s1",
  seq: 1,
  instrument: "BTCUSDT",
  side: "buy",
  action: "open",
  size_fraction: 1,
  private: false,
  status: "out",
  anchored_ms: null,
  created_ms: SIGNAL_AT,
} satisfies Signal;
const provider = { pubkey: "p", name: "P", timeframe_s: 3600, tiers: [] } as unknown as Provider;

let clock = Date.UTC(2026, 9, 13, 16, 35);
let asked: URL[] = [];
let served: Candle[] = [];
const candlesUpTo = (end: number): Candle[] =>
  Array.from({ length: 5 }, (_, i) => ({
    t: Math.floor(end / HOUR) * HOUR - (4 - i) * HOUR,
    o: 10,
    h: 12,
    l: 9,
    c: 11 + i,
    v: 5,
  }));

beforeEach(() => {
  jest.useFakeTimers();
  asked = [];
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  globalThis.fetch = (async (input: string) => {
    const u = new URL(input, "http://x");
    asked.push(u);
    served = candlesUpTo(Number(u.searchParams.get("end_ms")));
    return new Response(JSON.stringify(served));
  }) as unknown as typeof fetch;
});
afterEach(() => jest.useRealTimers());

async function render(live: boolean) {
  const { createElement, act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { ProviderChart } = await import("../src/components/ProviderChart");
  FakeChart.reset();
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const flush = () => act(async () => void (await Promise.resolve()));
  await act(async () =>
    root.render(createElement(ProviderChart, { provider, signals: [signal], live, now: () => clock })),
  );
  await flush();
  await flush();
  return {
    host,
    flush,
    tick: (ms: number) => act(async () => void jest.advanceTimersByTime(ms)),
    unmount: () => act(async () => root.unmount()),
  };
}

describe("ProviderChart, live", () => {
  test("asks for candles up to the clock, not up to the last signal plus bars", async () => {
    const p = await render(true);
    expect(asked).toHaveLength(1);
    expect(Number(asked[0]?.searchParams.get("end_ms"))).toBe(clock);
    const last = served[served.length - 1]?.t ?? 0;
    expect(clock - last).toBeLessThan(HOUR); // the newest candle is the one forming now
    await p.unmount();
  });

  test("the page's 15 s polling re-reads the candles up to the new time, the chart stays mounted", async () => {
    const p = await render(true);
    expect(FakeChart.all).toHaveLength(1);
    clock += 2 * HOUR;
    await p.tick(15_000);
    await p.flush();
    await p.flush();
    expect(asked).toHaveLength(2);
    expect(Number(asked[1]?.searchParams.get("end_ms"))).toBe(clock);
    expect(FakeChart.all).toHaveLength(1);
    expect(FakeChart.all[0]?.removeCount).toBe(0);
    const series = [...(FakeChart.all[0]?.live.values() ?? [])].find((s) => s.kind === "candle");
    expect(series?.data.length).toBe(5);
    expect((series?.data.at(-1) as { time: number }).time).toBe(Math.floor(clock / HOUR) * 3600);
    await p.unmount();
  });
});

describe("ProviderChart, replay (time-scaled demo)", () => {
  test("keeps the window around the last signal and does not follow the clock", async () => {
    const p = await render(false);
    const end = Number(asked[0]?.searchParams.get("end_ms"));
    expect(end).toBe(Math.floor(SIGNAL_AT / HOUR) * HOUR + 20 * HOUR);
    await p.unmount();
  });
});
