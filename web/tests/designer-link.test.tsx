import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { createDesignerMock } from "../mock/designer";

beforeAll(() => GlobalRegistrator.register());
afterAll(() => GlobalRegistrator.unregister());

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("designer backtest link", () => {
  test("a normal click on 'Open on chart' navigates to the chart route", async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const { createElement, act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { App } = await import("../src/App");
    const mock = createDesignerMock({ jobMs: 30 });
    const real = globalThis.fetch;
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      const r = mock(init?.method ?? "GET", String(url), body);
      return Response.json(r?.body ?? {}, { status: r?.status ?? 404 });
    }) as typeof fetch;
    try {
      window.location.hash = "#/designer";
      const host = document.createElement("div");
      document.body.appendChild(host);
      const root = createRoot(host);
      await act(async () => {
        root.render(createElement(App));
      });
      const settle = (ms: number) =>
        act(async () => {
          await tick(ms);
        });
      await settle(80);
      const btn = (re: RegExp) =>
        [...host.querySelectorAll("button")].find((b) => re.test(b.textContent ?? ""));
      await act(async () => {
        btn(/^Backtest$/)?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
      await act(async () => {
        btn(/Run backtest/)?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
      await settle(1600);
      const link = [...host.querySelectorAll("a")].find((a) => a.textContent === "Open on chart");
      expect(link).toBeDefined();
      // on its own line: a link wrapped inside running text has a bounding box whose centre is other text, so a click there misses it
      expect(link?.closest("p")?.textContent?.trim()).toBe("Open on chart");
      const href = link?.getAttribute("href") ?? "";
      await act(async () => {
        link?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
      });
      await settle(50);
      expect(window.location.hash).toBe(href);
      expect(window.location.hash).toMatch(/^#\/chart\?instrument=BTCUSDT&tf=.*&bt=/);
    } finally {
      globalThis.fetch = real;
    }
  });
});
