import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, jest, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Remote } from "../src/useApi";

beforeAll(() => {
  GlobalRegistrator.register();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterAll(() => GlobalRegistrator.unregister());

const { useApi } = await import("../src/useApi");

let visibility: "visible" | "hidden" = "visible";
function setVisibility(v: "visible" | "hidden") {
  visibility = v;
  document.dispatchEvent(new Event("visibilitychange"));
}

interface Req {
  resolve(v: string): void;
  reject(e: Error): void;
}
let reqs: Req[] = [];
const load = (): Promise<string> =>
  new Promise((resolve, reject) => {
    reqs.push({ resolve, reject });
  });

let seen: Remote<string>[] = [];
let root: Root;
let host: HTMLDivElement;

function Probe({ k, refreshMs }: { k: string; refreshMs?: number }) {
  const r = useApi(load, k, refreshMs === undefined ? undefined : { refreshMs });
  seen.push(r);
  return createElement("p", null, r.state === "ok" ? r.data : r.state);
}
const mount = (k = "a", refreshMs: number | null = 1000) =>
  act(async () => root.render(createElement(Probe, refreshMs === null ? { k } : { k, refreshMs })));
const text = () => host.textContent;
const tick = (ms: number) => act(async () => void jest.advanceTimersByTime(ms));
const settle = (i: number, v: string) => act(async () => reqs[i]?.resolve(v));

beforeEach(() => {
  jest.useFakeTimers();
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  reqs = [];
  seen = [];
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  jest.useRealTimers();
});

describe("useApi refresh", () => {
  test("without refreshMs it loads once and never polls", async () => {
    await mount("a", null);
    await settle(0, "v1");
    await tick(60_000);
    expect(reqs.length).toBe(1);
    expect(text()).toBe("v1");
  });

  test("refetches on the interval and shows the new data", async () => {
    await mount();
    await settle(0, "v1");
    await tick(1000);
    expect(reqs.length).toBe(2);
    await settle(1, "v2");
    expect(text()).toBe("v2");
  });

  test("does not refetch while hidden", async () => {
    await mount();
    await settle(0, "v1");
    setVisibility("hidden");
    await tick(5000);
    expect(reqs.length).toBe(1);
  });

  test("refetches immediately when the tab becomes visible again", async () => {
    await mount();
    await settle(0, "v1");
    setVisibility("hidden");
    await tick(3000);
    expect(reqs.length).toBe(1);
    await act(async () => setVisibility("visible"));
    expect(reqs.length).toBe(2);
    await settle(1, "v2");
    expect(text()).toBe("v2");
  });

  test("a refresh error keeps the last good data", async () => {
    await mount();
    await settle(0, "v1");
    await tick(1000);
    await act(async () => reqs[1]?.reject(new Error("boom")));
    expect(text()).toBe("v1");
    expect(seen.some((r) => r.state === "error")).toBe(false);
  });

  test("an initial-load error is shown, and a later refresh can recover", async () => {
    await mount();
    await act(async () => reqs[0]?.reject(new Error("down")));
    expect(seen[seen.length - 1]).toEqual({ state: "error", message: "down" });
    await tick(1000);
    await settle(1, "back");
    expect(text()).toBe("back");
  });

  test("a refresh never flips back to loading", async () => {
    await mount();
    await settle(0, "v1");
    const before = seen.length;
    await tick(1000);
    await settle(1, "v2");
    expect(seen.slice(before).every((r) => r.state === "ok")).toBe(true);
  });

  test("unchanged refresh data keeps the same Remote reference", async () => {
    await mount();
    await settle(0, "v1");
    const first = seen[seen.length - 1];
    await tick(1000);
    await settle(1, "v1");
    expect(seen[seen.length - 1]).toBe(first);
  });

  test("a refresh response that lands after the key changed is discarded", async () => {
    await mount("a");
    await settle(0, "a1");
    await tick(1000); // refresh for key a in flight (reqs[1])
    await mount("b"); // key change: loads b (reqs[2])
    expect(text()).toBe("loading");
    await settle(1, "stale-a");
    expect(text()).toBe("loading");
    await settle(2, "b1");
    expect(text()).toBe("b1");
  });

  test("the old key's result is never returned for the new key, even in the first render", async () => {
    await mount("a");
    await settle(0, "a1");
    seen = [];
    await mount("b");
    expect(seen[0]).toEqual({ state: "loading" });
  });

  test("does not start a refresh while one is in flight", async () => {
    await mount();
    await settle(0, "v1");
    await tick(1000);
    await tick(5000);
    expect(reqs.length).toBe(2);
    await settle(1, "v2");
    await tick(1000);
    expect(reqs.length).toBe(3);
  });

  test("does not refresh while the initial load is still in flight", async () => {
    await mount();
    await tick(5000);
    expect(reqs.length).toBe(1);
  });

  test("stops polling on unmount", async () => {
    await mount();
    await settle(0, "v1");
    await act(async () => root.unmount());
    root = createRoot(host);
    await tick(5000);
    expect(reqs.length).toBe(1);
  });
});
