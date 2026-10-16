import { afterAll, beforeAll, describe, expect, jest, spyOn, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { buildWatcher, checkBuild, observeBuild } from "../src/lib/build";

function memory() {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
}

describe("buildWatcher", () => {
  test("reloads once when the served build differs, never when it matches", () => {
    const reload = jest.fn();
    const seen = buildWatcher({ current: "old", reload, storage: memory() });
    expect(seen("old")).toBe(false);
    expect(seen(null)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    expect(seen("new")).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  test("does not loop when the reload lands on the same old build", () => {
    const reload = jest.fn();
    const storage = memory();
    // first page life reloads for "new"; the next page life (same storage) still runs "old"
    buildWatcher({ current: "old", reload, storage })("new");
    buildWatcher({ current: "old", reload, storage })("new");
    buildWatcher({ current: "old", reload, storage })("new");
    expect(reload).toHaveBeenCalledTimes(1);
    // a further, different build is still followed
    buildWatcher({ current: "old", reload, storage })("newer");
    expect(reload).toHaveBeenCalledTimes(2);
  });

  test("does nothing without a current id, and never reloads with unusable storage", () => {
    const reload = jest.fn();
    buildWatcher({ current: null, reload, storage: memory() })("new");
    buildWatcher({ current: "old", reload, storage: null })("new");
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {},
    };
    buildWatcher({ current: "old", reload, storage: broken })("new");
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("in a page", () => {
  beforeAll(() => GlobalRegistrator.register());
  afterAll(() => GlobalRegistrator.unregister());

  const stamp = (id: string | null) => {
    document.head.innerHTML = id ? `<meta name="sa-build" content="${id}">` : "";
    window.sessionStorage.clear();
  };
  const res = (id?: string) => new Response("", { headers: id ? { "X-SA-Build": id } : {} });

  test("observeBuild reads the meta tag and the header", () => {
    const reload = spyOn(window.location, "reload").mockImplementation(() => {});
    stamp("old");
    observeBuild(res("old"));
    observeBuild(res());
    expect(reload).not.toHaveBeenCalled();
    observeBuild(res("new"));
    observeBuild(res("new"));
    expect(reload).toHaveBeenCalledTimes(1);
    stamp(null); // dev server: no id, no checks
    observeBuild(res("other"));
    expect(reload).toHaveBeenCalledTimes(1);
    reload.mockRestore();
  });

  test("checkBuild sends a HEAD to the page, and swallows network errors", async () => {
    const reload = spyOn(window.location, "reload").mockImplementation(() => {});
    stamp("old");
    const calls: [string, RequestInit | undefined][] = [];
    const f = (async (u: string, init?: RequestInit) => {
      calls.push([u, init]);
      return res("new");
    }) as unknown as typeof fetch;
    await checkBuild(f);
    expect(calls[0]?.[0]).toBe("/");
    expect(calls[0]?.[1]?.method).toBe("HEAD");
    expect(reload).toHaveBeenCalledTimes(1);
    await checkBuild((async () => {
      throw new Error("offline");
    }) as unknown as typeof fetch);
    reload.mockRestore();
  });

  test("getJson and a crashed ErrorBoundary both lead to a reload on a newer build", async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const reload = spyOn(window.location, "reload").mockImplementation(() => {});
    const { getJson } = await import("../src/api/client");
    stamp("old");
    await getJson("/api/x", async () => new Response("{}", { headers: { "x-sa-build": "v2" } }));
    expect(reload).toHaveBeenCalledTimes(1);

    stamp("old");
    const { createElement, act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { ErrorBoundary } = await import("../src/components/ErrorBoundary");
    const err = spyOn(console, "error").mockImplementation(() => {});
    const realFetch = globalThis.fetch;
    let heads = 0;
    globalThis.fetch = (async (_u: string, init?: RequestInit) => {
      if (init?.method === "HEAD") heads++;
      return res("v3");
    }) as unknown as typeof fetch;
    const Bomb = () => {
      throw new Error("e.release is not iterable");
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(createElement(ErrorBoundary, { label: "This page" }, createElement(Bomb)));
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(host.textContent).toContain("failed to display");
    expect(heads).toBe(1);
    expect(reload).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
    globalThis.fetch = realFetch;
    err.mockRestore();
    reload.mockRestore();
  });
});
