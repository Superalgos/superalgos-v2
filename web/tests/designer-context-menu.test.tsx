import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";

beforeAll(() => GlobalRegistrator.register());
afterAll(() => GlobalRegistrator.unregister());

/** happy-dom does no layout, so the menu's measured size is supplied here. */
function withSize<T>(width: number, height: number, run: () => Promise<T>): Promise<T> {
  const proto = HTMLElement.prototype;
  const ow = Object.getOwnPropertyDescriptor(proto, "offsetWidth");
  const os = Object.getOwnPropertyDescriptor(proto, "scrollHeight");
  Object.defineProperty(proto, "offsetWidth", { configurable: true, get: () => width });
  Object.defineProperty(proto, "scrollHeight", { configurable: true, get: () => height });
  return run().finally(() => {
    if (ow) Object.defineProperty(proto, "offsetWidth", ow);
    else Reflect.deleteProperty(proto, "offsetWidth");
    if (os) Object.defineProperty(proto, "scrollHeight", os);
    else Reflect.deleteProperty(proto, "scrollHeight");
  });
}

async function show(x: number, y: number) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const { createElement, act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { ContextMenu } = await import("../src/components/designer/ContextMenu");
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(
      createElement(ContextMenu, {
        x,
        y,
        title: "Open",
        actions: [],
        extras: [{ label: "Spread: auto", onPick: () => {} }],
        extraTitle: "Radial layout",
        onPick: () => {},
        onClose: () => {},
      }),
    );
  });
  const el = host.querySelector(".cm") as HTMLElement;
  const out = { left: el.style.left, top: el.style.top, maxHeight: el.style.maxHeight, overflowY: el.style.overflowY };
  await act(async () => root.unmount());
  host.remove();
  return out;
}

describe("ContextMenu stays inside the viewport (SEP-5.17)", () => {
  test("opened with room: at the click point", async () => {
    const { innerWidth: vw, innerHeight: vh } = window;
    const r = await withSize(200, 300, () => show(50, 60));
    expect(r.left).toBe("50px");
    expect(r.top).toBe("60px");
    expect(r.maxHeight).toBe("");
  });

  test("opened low on the screen: flips up and ends at the click point", async () => {
    const { innerWidth: vw, innerHeight: vh } = window;
    const r = await withSize(200, 300, () => show(50, vh - 20));
    expect(r.top).toBe(`${vh - 20 - 300}px`);
  });

  test("opened at the right edge: flips left", async () => {
    const { innerWidth: vw, innerHeight: vh } = window;
    const r = await withSize(200, 300, () => show(vw - 10, 60));
    expect(r.left).toBe(`${vw - 10 - 200}px`);
  });

  test("taller than the viewport: pinned to the top and scrolls inside", async () => {
    const { innerWidth: vw, innerHeight: vh } = window;
    const r = await withSize(200, vh + 400, () => show(50, 300));
    expect(r.top).toBe("8px");
    expect(r.maxHeight).toBe(`${vh - 16}px`);
    expect(r.overflowY).toBe("auto");
  });
});
