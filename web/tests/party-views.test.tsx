import { afterAll, beforeAll, beforeEach, describe, expect, mock, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { route } from "../mock/routes";
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

let urls: string[] = [];
/** The mock API as `fetch`, with the network answer overridable (dev views on or off, time scale). */
function serve(network: { dev_views: boolean; time_scale?: number }) {
  urls = [];
  globalThis.fetch = (async (input: string) => {
    urls.push(input);
    const u = new URL(input, "http://x");
    const r = route(u.pathname, u.searchParams);
    if (u.pathname === "/api/network") {
      return new Response(JSON.stringify({ ...(r.body as object), ...network }));
    }
    return new Response(JSON.stringify(r.body), { status: r.status });
  }) as unknown as typeof fetch;
}

async function mount(element: () => Promise<React.ReactElement>) {
  const { act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  FakeChart.reset();
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(await element()));
  for (let i = 0; i < 6; i++) await act(async () => void (await new Promise((r) => setTimeout(r, 0))));
  return {
    host,
    unmount: () => act(async () => root.unmount()),
  };
}

beforeEach(() => {
  document.body.innerHTML = "";
});

const pubkey = () => route("/api/providers", new URLSearchParams()).body as { pubkey: string }[];

describe("Provider page (the provider's view)", () => {
  test("every signal has one plain status, and no per-tier or per-follower data is shown", async () => {
    serve({ dev_views: false });
    const { createElement } = await import("react");
    const { ProviderPage } = await import("../src/pages/Provider");
    const pk = pubkey()[0]?.pubkey ?? "";
    const page = await mount(async () => createElement(ProviderPage, { pubkey: pk }));
    const text = page.host.textContent ?? "";
    expect(text).toContain("Signals");
    expect(text).toContain("Out");
    expect(text).toContain("Waiting for time-stamp");
    expect(text).not.toContain("Signal timeline by tier");
    expect(text).not.toContain("Tier ");
    expect(page.host.querySelector(".timeline")).toBeNull();
    expect(urls.some((u) => u.includes("/api/dev/"))).toBe(false);
    await page.unmount();
  });

  test("the chart is live when the relay runs in real time: its candle request ends at the clock", async () => {
    serve({ dev_views: false, time_scale: 1 });
    const { createElement } = await import("react");
    const { ProviderPage } = await import("../src/pages/Provider");
    const pk = pubkey()[0]?.pubkey ?? "";
    const before = Date.now();
    const page = await mount(async () => createElement(ProviderPage, { pubkey: pk }));
    const candleUrls = urls.filter((u) => u.startsWith("/api/market/candles"));
    expect(candleUrls.length).toBeGreaterThan(0);
    const end = Number(new URL(candleUrls[0] ?? "", "http://x").searchParams.get("end_ms"));
    expect(end).toBeGreaterThanOrEqual(before);
    expect(end).toBeLessThanOrEqual(Date.now());
    await page.unmount();
  });
});

describe("dev views flag", () => {
  test("off: the Dev page says so, asks for no timeline data, and the nav does not offer it", async () => {
    serve({ dev_views: false });
    const { createElement } = await import("react");
    const { DevPage } = await import("../src/pages/Dev");
    const { App } = await import("../src/App");
    const dev = await mount(async () => createElement(DevPage));
    expect(dev.host.textContent).toContain("Dev views are off");
    expect(dev.host.querySelector(".timeline")).toBeNull();
    expect(urls.some((u) => u.includes("/api/dev/"))).toBe(false);
    await dev.unmount();
    const app = await mount(async () => createElement(App));
    expect(app.host.querySelector('nav a[href="#/dev"]')).toBeNull();
    await app.unmount();
  });

  test("on: the nav offers the clearly labelled Dev page, which shows the tier timeline", async () => {
    serve({ dev_views: true });
    const { createElement } = await import("react");
    const { DevPage } = await import("../src/pages/Dev");
    const { App } = await import("../src/App");
    const app = await mount(async () => createElement(App));
    expect(app.host.querySelector('nav a[href="#/dev"]')?.textContent).toBe("Dev views");
    await app.unmount();
    const dev = await mount(async () => createElement(DevPage));
    const text = dev.host.textContent ?? "";
    expect(text).toContain("Development and testing only");
    expect(text).toContain("Signal timeline by tier");
    expect(dev.host.querySelector(".timeline")).not.toBeNull();
    expect(urls.some((u) => u.includes("/api/dev/providers/"))).toBe(true);
    await dev.unmount();
  });
});

describe("My signals (the follower's view)", () => {
  test("shows the follower's own tier delay, executed and rejected with the reason", async () => {
    serve({ dev_views: false });
    const { createElement } = await import("react");
    const { PowerPage } = await import("../src/pages/Power");
    const page = await mount(async () => createElement(PowerPage));
    const text = page.host.textContent ?? "";
    expect(text).toContain("My signals");
    expect(text).toContain("Executed: working order");
    expect(text).toContain("Rejected: not enough cash for the minimum quantity");
    expect(text).toContain("Rejected: gap: seq 6 does not follow 4");
    expect(text).toMatch(/Tier 2, delay \d+s/);
    const rows = page.host.querySelectorAll("table")[1]?.querySelectorAll("tbody tr") ?? [];
    expect(rows.length).toBe(5);
    expect(urls.filter((u) => u.startsWith("/api/me/signals")).length).toBe(1);
    expect(urls.some((u) => u.includes("/api/dev/"))).toBe(false);
    await page.unmount();
  });

  test("says so when nothing has been received", async () => {
    serve({ dev_views: false });
    const base = globalThis.fetch;
    globalThis.fetch = (async (input: string) =>
      input.startsWith("/api/me/signals") ? new Response("[]") : base(input)) as typeof fetch;
    const { createElement } = await import("react");
    const { PowerPage } = await import("../src/pages/Power");
    const page = await mount(async () => createElement(PowerPage));
    expect(page.host.textContent).toContain("No signals received yet.");
    await page.unmount();
  });
});
