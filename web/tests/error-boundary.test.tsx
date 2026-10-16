import { afterAll, beforeAll, describe, expect, spyOn, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";

beforeAll(() => GlobalRegistrator.register());
afterAll(() => GlobalRegistrator.unregister());

describe("ErrorBoundary", () => {
  test("shows a fallback with a retry when a child throws, and recovers on retry", async () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const { createElement } = await import("react");
    const { act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { ErrorBoundary } = await import("../src/components/ErrorBoundary");
    const err = spyOn(console, "error").mockImplementation(() => {});
    let broken = true;
    const Bomb = () => {
      if (broken) throw new Error("series exploded");
      return createElement("p", null, "all good");
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(createElement(ErrorBoundary, { label: "The chart" }, createElement(Bomb)));
    });
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    expect(host.textContent).toContain("The chart");
    expect(host.textContent).toContain("series exploded");
    const retry = [...host.querySelectorAll("button")].find((b) => /retry/i.test(b.textContent ?? ""));
    expect(retry).toBeDefined();
    broken = false;
    await act(async () => {
      retry?.click();
    });
    expect(host.textContent).toContain("all good");
    expect(host.querySelector('[role="alert"]')).toBeNull();
    await act(async () => root.unmount());
    err.mockRestore();
  });
});
