import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { createDesignerMock } from "../mock/designer";
import { type Json, type JsonObject, getAt, serialize } from "../src/lib/designer/doc";
import { EXAMPLES } from "../src/lib/designer/examples";
import { buildGraph } from "../src/lib/designer/graph";
import { bigStrategy } from "./support/generate";

beforeAll(() => GlobalRegistrator.register());
afterAll(() => GlobalRegistrator.unregister());

const COND = "$.stages.open.when.any[0].all[0]";
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

async function setup(
  props: Record<string, unknown> = {},
  mockOpts: Parameters<typeof createDesignerMock>[0] = {},
  /** Stored layout choice; the older tests exercise the Cards view. `null` = nothing stored. */
  layout: string | null = "cards",
) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const { createElement, act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { DesignerPage } = await import("../src/pages/Designer");
  const mock = createDesignerMock(mockOpts);
  const calls: string[] = [];
  const fetcher = async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? "GET"} ${url}`);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    const r = mock(init?.method ?? "GET", url, body);
    return Response.json(r?.body ?? {}, { status: r?.status ?? 404 });
  };
  const mem = new Map<string, string>();
  if (layout !== null) mem.set("sa-designer-layout", layout);
  const storage = {
    getItem: (k: string) => mem.get(k) ?? null,
    setItem: (k: string, v: string) => void mem.set(k, v),
  };
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(createElement(DesignerPage, { fetcher, storage, debounceMs: 5, pollMs: 5, ...props }));
  });
  const settle = async (ms = 40) => {
    await act(async () => {
      await tick(ms);
    });
  };
  const click = async (el: Element | null | undefined) => {
    if (!el) throw new Error("element to click not found");
    await act(async () => {
      el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
  };
  const type = async (el: HTMLTextAreaElement | HTMLInputElement | null, value: string) => {
    if (!el) throw new Error("field to type into not found");
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    await act(async () => {
      Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const node = (id: string) => host.querySelector(`.react-flow__node[data-id="${id}"]`);
  const button = (text: RegExp) =>
    [...host.querySelectorAll("button")].find((b) => text.test(b.textContent ?? ""));
  return { host, act, root, calls, mem, settle, click, type, node, button, badge: () => host.querySelector(".tb .badge")?.textContent };
}

/** Captures what Save downloads: the file name and the text. */
function captureDownloads() {
  const saved: { name: string; text: Promise<string> }[] = [];
  const orig = URL.createObjectURL;
  const origClick = HTMLAnchorElement.prototype.click;
  let lastBlob: Blob | null = null;
  URL.createObjectURL = ((b: Blob) => {
    lastBlob = b;
    return "blob:test";
  }) as typeof URL.createObjectURL;
  HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
    saved.push({ name: this.download, text: (lastBlob as unknown as Blob).text() });
  };
  return {
    saved,
    restore: () => {
      URL.createObjectURL = orig;
      HTMLAnchorElement.prototype.click = origClick;
    },
  };
}

describe("designer page", () => {
  test("opens an example as a graph and validates it on the server", async () => {
    const t = await setup();
    await t.settle();
    expect(t.node("$")).not.toBeNull();
    expect(t.node("$.stages.open.orders[0]")).not.toBeNull();
    expect(t.badge()).toBe("valid");
    expect(t.calls).toContain("POST /api/strategy/validate");
  });

  test("edit a condition, see the error at its node and column, fix it, save a valid document (SEP-5.9, SEP-5.8)", async () => {
    const dl = captureDownloads();
    try {
      const t = await setup();
      await t.settle();
      await t.click(t.node(COND));
      const area = () => t.host.querySelector("textarea") as HTMLTextAreaElement;
      expect(area().value).toBe("cross_above(ema(close, 20), ema(close, 50))");
      await t.type(area(), "cross_above(ema(close, 20), ");
      await t.settle();
      expect(t.badge()).toBe("1 problem");
      expect(t.node(COND)?.querySelector(".dn-bad")).not.toBeNull();
      // the ancestors are marked "problem inside"
      expect(t.node("$.stages.open")?.querySelector(".dn-below")).not.toBeNull();
      const errors = t.host.querySelector(".fe-errors");
      expect(errors?.textContent).toContain("Column 11");
      expect(errors?.textContent).toContain("unclosed '('");
      expect(t.host.querySelector(".fe-caret")?.textContent).toBe(
        "cross_above(ema(close, 20), \n           ^",
      );

      await t.type(area(), "cross_above(ema(close, 20), ema(close, 60))");
      await t.settle();
      expect(t.badge()).toBe("valid");
      await t.click(t.button(/^Save$/));
      await t.settle();
      expect(dl.saved).toHaveLength(1);
      const file = dl.saved[0];
      expect(file?.name).toBe("ema-crossover.json");
      const text = await file?.text;
      const saved = JSON.parse(text ?? "") as Json;
      expect(getAt(saved, ["stages", "open", "when", "any", 0, "all", 0])).toBe(
        "cross_above(ema(close, 20), ema(close, 60))",
      );
      expect(text).toBe(serialize(saved));
      expect(createDesignerMock()("POST", "/api/strategy/validate", saved)?.body).toEqual({ ok: true, errors: [] });
      expect(text).not.toMatch(/"(position|x|y)"/);
    } finally {
      dl.restore();
    }
  });

  test("saving an invalid document needs a confirmation and is marked in the file name", async () => {
    const dl = captureDownloads();
    try {
      const t = await setup();
      await t.settle();
      await t.click(t.node(COND));
      await t.type(t.host.querySelector("textarea"), "close >");
      await t.settle();
      await t.click(t.button(/^Save$/));
      await t.settle();
      expect(dl.saved).toHaveLength(0);
      const dialog = t.host.querySelector('[role="alertdialog"]');
      expect(dialog?.textContent).toContain("1 problem");
      expect(dialog?.textContent).toContain("ema-crossover.invalid.json");
      await t.click(t.button(/Save anyway/));
      expect(dl.saved.map((s) => s.name)).toEqual(["ema-crossover.invalid.json"]);
    } finally {
      dl.restore();
    }
  });

  test("an unreachable validator shows 'not validated' and still asks before saving", async () => {
    const t = await setup({
      fetcher: async () => {
        throw new Error("ECONNREFUSED");
      },
    });
    await t.settle();
    expect(t.badge()).toBe("not validated");
    await t.click(t.button(/^Save$/));
    await t.settle();
    expect(t.host.querySelector('[role="alertdialog"]')?.textContent).toContain("could not be validated");
  });

  test("the context menu offers only what the schema allows, and adds a node", async () => {
    const t = await setup();
    await t.settle();
    const before = t.host.querySelectorAll(".react-flow__node").length;
    await t.act(async () => {
      t.node("$.stages.open.when")?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    });
    const items = [...t.host.querySelectorAll('[role="menuitem"]')].map((b) => b.textContent);
    expect(items).toEqual(["Add situation"]);
    await t.click(t.host.querySelector('[role="menuitem"]'));
    await t.settle();
    expect(t.host.querySelectorAll(".react-flow__node").length).toBe(before + 2); // situation + its condition
    expect(t.host.querySelector('[role="menu"]')).toBeNull();
    expect(t.badge()).toBe("valid");
  });

  test("layout is stored in the view store keyed by strategy name, not in the document", async () => {
    const t = await setup();
    await t.settle();
    // an action that moves nodes (add a situation) writes a view entry for this strategy only
    await t.click(t.node("$.stages.open.when"));
    await t.click(t.button(/^Add situation$/));
    await t.settle(350);
    const keys = [...t.mem.keys()].filter((k) => k !== "sa-designer-layout");
    expect(keys).toEqual(["sa-designer-view:ema-crossover"]);
    expect(t.mem.get(keys[0] as string)).not.toContain("schema_version");
  });

  test("scalar fields are edited from the schema: enum, number, boolean, goto target", async () => {
    const trend = EXAMPLES.find((e) => e.file === "trend_phases.json")?.doc as JsonObject;
    const t = await setup({ initial: { doc: trend, fileName: "trend_phases.json" } });
    await t.settle();
    const order = "$.stages.open.orders[0]";
    await t.click(t.node(order));
    const field = (label: string) =>
      t.host.querySelector(`#f-stages-open-orders-0-${label}`) as HTMLInputElement | HTMLSelectElement;
    await t.act(async () => {
      const sel = field("type") as HTMLSelectElement;
      sel.value = "limit";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(t.node(order)?.textContent).toContain("limit");
    await t.type(field("size_fraction"), "0.5");
    expect(t.node(order)?.textContent).toContain("50%");
    await t.type(field("size_fraction"), "");
    expect(t.node(order)?.textContent).not.toContain("50%");
    await t.click(field("rearm"));
    expect(t.node(order)?.textContent).toContain("re-arm");
    await t.type(field("take_profit_pct"), "2");
    expect((field("take_profit_pct") as HTMLInputElement).value).toBe("2");
    await t.type(field("id"), "entry_a");
    expect(t.node(order)?.textContent).toContain("entry_a");

    // a move-to-phase event: its target is chosen among the phases of the same list
    const phase = "$.stages.manage.stop.phases[1]";
    await t.click(t.node(phase));
    await t.click(t.button(/^Add move-to-phase event$/));
    const goto = `${phase}.goto[0]`;
    expect(t.node(goto)).not.toBeNull();
    const to = t.host.querySelector("#f-stages-manage-stop-phases-1-goto-0-to") as HTMLSelectElement;
    expect([...to.options].map((o) => o.value)).toEqual(["initial", "break_even", "trail"]);
    await t.act(async () => {
      to.value = "trail";
      to.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(t.node(goto)?.textContent).toContain("to trail");
    // Delete removes the selected node when the schema allows it
    await t.act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" }));
    });
    expect(t.node(goto)).toBeNull();
    await t.act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "z", metaKey: true }));
    });
    expect(t.node(goto)).not.toBeNull();
  });

  test("backtest: run, poll, link to the chart", async () => {
    const t = await setup({}, { jobMs: 30 });
    await t.settle();
    await t.click(t.button(/^Backtest$/));
    await t.click(t.button(/Run backtest/));
    await t.settle(120);
    const status = t.host.querySelector('[role="status"]');
    expect(status?.textContent).toContain("done");
    const link = [...t.host.querySelectorAll("a")].find((a) => a.textContent === "Open on chart");
    expect(link?.getAttribute("href")).toMatch(/^#\/chart\?instrument=BTCUSDT&tf=4h&bt=/);
  });

  test("backtest: the 403 reason is shown", async () => {
    const reason = "backtests are allowed only on a loopback-bound API: the API is bound to 0.0.0.0, not loopback";
    const t = await setup({
      fetcher: async (url: string, init?: RequestInit) =>
        url === "/api/backtests/run"
          ? Response.json({ error: reason }, { status: 403 })
          : Response.json({ ok: true, errors: [] }),
    });
    await t.settle();
    await t.click(t.button(/^Backtest$/));
    await t.click(t.button(/Run backtest/));
    await t.settle();
    const err = t.host.querySelector(".error");
    expect(err?.textContent).toContain("Not allowed (403)");
    expect(err?.textContent).toContain(reason);
  });

  test("a bad date window disables Run with the reason", async () => {
    const t = await setup();
    await t.click(t.button(/^Backtest$/));
    await t.type(t.host.querySelector("#bt-end"), "2024-01-01");
    expect(t.host.textContent).toContain("end date must be after the start");
    expect((t.button(/Run backtest/) as HTMLButtonElement).disabled).toBe(true);
    await t.type(t.host.querySelector("#bt-end"), "2030-01-01");
    expect(t.host.textContent).toContain("at most 400");
  });
});

describe("performance (SEP-5.11, happy-dom proxy)", () => {
  test("a 200-node strategy renders and takes one edit within 100 ms each", async () => {
    const doc = bigStrategy(14) as JsonObject;
    expect(buildGraph(doc).nodes.length).toBeGreaterThanOrEqual(200);
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const { createElement, act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { DesignerPage } = await import("../src/pages/Designer");
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const storage = { getItem: (k: string) => (k === "sa-designer-layout" ? "cards" : null), setItem: () => {} };
    const fetcher = async () => Response.json({ ok: true, errors: [] });
    const t0 = performance.now();
    await act(async () => {
      root.render(createElement(DesignerPage, { initial: { doc, fileName: "big.json" }, storage, fetcher, debounceMs: 5000 }));
    });
    const renderMs = performance.now() - t0;
    expect(host.querySelectorAll(".react-flow__node").length).toBeGreaterThanOrEqual(200);
    const id = "$.stages.open.orders[3].create.any[0].all[0]";
    await act(async () => {
      host.querySelector(`.react-flow__node[data-id="${id}"]`)?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const area = host.querySelector("textarea") as HTMLTextAreaElement;
    const t1 = performance.now();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(area, "rsi(close, 14) > 31");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const editMs = performance.now() - t1;
    expect(host.querySelector(`.react-flow__node[data-id="${id}"]`)?.textContent).toContain("rsi(close, 14) > 31");
    console.log(`200-node proxy: render ${renderMs.toFixed(1)} ms, edit ${editMs.toFixed(1)} ms`);
    // SA_PERF_SCALE widens the budget on slow shared CI runners (default 1 = 100 ms); see ci.yml
    const scale = Number(process.env.SA_PERF_SCALE ?? "1");
    if (!(scale >= 1)) throw new Error(`SA_PERF_SCALE=${process.env.SA_PERF_SCALE}: want a number >= 1; fix or unset it`);
    expect(renderMs).toBeLessThan(100 * scale);
    expect(editMs).toBeLessThan(100 * scale);
  });
});

describe("typing is decoupled from the canvas render", () => {
  test("a keystroke updates the text box at once; the 200-node canvas catches up afterwards", async () => {
    const doc = bigStrategy(14) as JsonObject;
    const t = await setup({ initial: { doc, fileName: "big.json" }, debounceMs: 5000 });
    const id = "$.stages.open.orders[3].create.any[0].all[0]";
    await t.click(t.node(id));
    const area = t.host.querySelector("textarea") as HTMLTextAreaElement;
    const before = t.node(id)?.textContent ?? "";
    expect(before).not.toContain("rsi(close, 14) > 31");
    // outside act(): React renders as it would in the browser, urgent work first
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
    try {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(area, "rsi(close, 14) > 31");
      area.dispatchEvent(new Event("input", { bubbles: true }));
      // the keystroke's own render is done: the text box has the new text, the canvas does not yet
      expect((t.host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("rsi(close, 14) > 31");
      expect(t.node(id)?.textContent).toBe(before);
    } finally {
      (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    }
    // the canvas catches up once React gets to the deferred work; any later update inside act()
    // drives it (other test files replace the globals the scheduler captured, so it may not tick by itself)
    await t.settle(100);
    await t.click(t.node(id));
    await t.settle(100);
    expect(t.node(id)?.textContent).toContain("rsi(close, 14) > 31");
  });
});

describe("designer layouts", () => {
  test("Radial is the default; Cards is the other choice; icons, collapse and expand work", async () => {
    const t = await setup({}, {}, null);
    await t.settle();
    const seg = () => [...t.host.querySelectorAll(".tb-seg button")] as HTMLButtonElement[];
    expect(seg().map((b) => b.textContent)).toEqual(["Cards", "Radial"]);
    expect(seg().map((b) => b.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    expect(t.node("$")?.querySelector(".v1n")).not.toBeNull();
    expect(t.node("$")?.querySelector("img")).not.toBeNull();
    expect(t.host.querySelector(".dn")).toBeNull();
    const before = t.host.querySelectorAll(".react-flow__node").length;
    await t.click(t.node("$.stages.open")?.querySelector(".v1-toggle"));
    await t.settle();
    expect(t.node("$.stages.open")?.querySelector(".v1-toggle-closed")?.textContent).toMatch(/^\+\d+$/);
    expect(t.host.querySelectorAll(".react-flow__node").length).toBeLessThan(before);
    await t.click(t.node("$.stages.open")?.querySelector(".v1-toggle"));
    await t.settle();
    expect(t.host.querySelectorAll(".react-flow__node").length).toBe(before);

    await t.click(seg().find((b) => b.textContent === "Cards"));
    await t.settle();
    expect(t.mem.get("sa-designer-layout")).toBe("cards");
    expect(t.host.querySelector(".v1n")).toBeNull();
    expect(t.node("$.stages.open.orders[0]")?.querySelector(".dn")).not.toBeNull();
    await t.click(seg().find((b) => b.textContent === "Radial"));
    await t.settle();
    expect(t.mem.get("sa-designer-layout")).toBe("radial");
    expect(t.node("$")?.querySelector(".v1n")).not.toBeNull();
  });

  test("a remembered Cards choice is used on load", async () => {
    const t = await setup({}, {}, "cards");
    await t.settle();
    expect(t.host.querySelector(".v1n")).toBeNull();
    expect(t.host.querySelector(".dn")).not.toBeNull();
  });

  test("the retired \"lanes\" preference falls back to Radial", async () => {
    const t = await setup({}, {}, "lanes");
    await t.settle();
    expect(t.node("$")?.querySelector(".v1n")).not.toBeNull();
    const pressed = [...t.host.querySelectorAll(".tb-seg button")].map((b) => b.getAttribute("aria-pressed"));
    expect(pressed).toEqual(["false", "true"]);
  });
});

describe("designer radial wiring (SEP-5.13, SEP-5.14)", () => {
  const OPEN = "$.stages.open";
  const ORDER = '$.stages.open.orders[0]';

  async function openMenu(t: Awaited<ReturnType<typeof setup>>, id: string, x = 10, y = 10) {
    await t.act(async () => {
      t.node(id)?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: x, clientY: y }));
    });
  }
  const closeMenu = async (t: Awaited<ReturnType<typeof setup>>) => {
    await t.act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(t.host.querySelector('[role="menu"]')).toBeNull();
  };
  const radialEntries = (t: Awaited<ReturnType<typeof setup>>) =>
    [...t.host.querySelectorAll('[role="menuitemradio"]')] as HTMLElement[];
  const pick = async (t: Awaited<ReturnType<typeof setup>>, id: string, label: string) => {
    await openMenu(t, id);
    const item = [...t.host.querySelectorAll('[role="menu"] button')].find((b) => b.textContent?.replace(/^✓ /, "") === label);
    if (!item) throw new Error(`menu item "${label}" not found`);
    await t.click(item);
  };
  /** The radial settings the page auto-saved for this strategy. */
  const stored = async (t: Awaited<ReturnType<typeof setup>>) => {
    await t.settle(350);
    const raw = t.mem.get("sa-designer-view:ema-crossover");
    return raw ? (JSON.parse(raw) as { positions: object; radial?: Record<string, Record<string, unknown>> }) : null;
  };
  const text = (t: Awaited<ReturnType<typeof setup>>) => (t.host.querySelector(".tb-name")?.textContent ?? "");

  test("the right-click menu has a Radial layout section; Spread, Distance, Pin, Unpin, Reset branch update the settings", async () => {
    const t = await setup({}, {}, "radial");
    await t.settle();
    await openMenu(t, OPEN);
    expect(t.host.textContent).toContain("Radial layout");
    const labels = radialEntries(t).map((b) => b.textContent?.replace(/^✓ /, ""));
    expect(labels).toContain("Spread: auto");
    expect(labels).toContain("Spread: 90°");
    expect(labels).toContain("Distance: ×1.5");
    expect(labels).toContain("Reset layout of this branch");
    expect(radialEntries(t).find((b) => b.textContent === "✓ Spread: auto")).toBeTruthy();
    await closeMenu(t);

    await pick(t, OPEN, "Spread: 90°");
    await pick(t, OPEN, "Distance: ×1.5");
    let v = await stored(t);
    const keys = Object.keys(v?.radial ?? {});
    expect(keys).toHaveLength(1);
    expect(v?.radial?.[keys[0] as string]).toEqual({ spread: 90, distance: 1.5 });
    // the menu now shows the current choices
    await openMenu(t, OPEN);
    expect(radialEntries(t).filter((b) => b.textContent?.startsWith("✓ ")).map((b) => b.textContent)).toEqual([
      "✓ Spread: 90°",
      "✓ Distance: ×1.5",
    ]);
    await closeMenu(t);

    await pick(t, OPEN, "Pin here");
    v = await stored(t);
    const pin = v?.radial?.[keys[0] as string]?.pin as { angle: number; distance: number };
    expect(Number.isFinite(pin.angle)).toBe(true);
    expect(pin.distance).toBeGreaterThanOrEqual(40);
    await openMenu(t, OPEN);
    expect(t.host.textContent).not.toContain("Pin here");
    await closeMenu(t);
    await pick(t, OPEN, "Unpin");
    v = await stored(t);
    expect(v?.radial?.[keys[0] as string]?.pin).toBeUndefined();
    expect(v?.radial?.[keys[0] as string]?.spread).toBe(90);

    await pick(t, OPEN, "Spread: auto");
    v = await stored(t);
    expect(v?.radial?.[keys[0] as string]?.spread).toBeUndefined();

    await pick(t, OPEN, "Spread: 45°");
    await pick(t, ORDER, "Distance: ×2");
    v = await stored(t);
    expect(Object.keys(v?.radial ?? {})).toHaveLength(2);
    await pick(t, OPEN, "Reset layout of this branch");
    v = await stored(t);
    expect(v?.radial ?? {}).toEqual({});
  });

  test("the strategy root offers Spread but no Distance or Pin; leaves offer no Spread", async () => {
    const t = await setup({}, {}, "radial");
    await t.settle();
    await openMenu(t, "$");
    let labels = radialEntries(t).map((b) => b.textContent?.replace(/^✓ /, "") ?? "");
    expect(labels.some((l) => l.startsWith("Spread"))).toBe(true);
    expect(labels.some((l) => l.startsWith("Distance"))).toBe(false);
    expect(t.host.textContent).not.toContain("Pin here");
    await closeMenu(t);
    await openMenu(t, COND);
    labels = radialEntries(t).map((b) => b.textContent?.replace(/^✓ /, "") ?? "");
    expect(labels.some((l) => l.startsWith("Spread"))).toBe(false);
    expect(labels.some((l) => l.startsWith("Distance"))).toBe(true);
  });

  test("Cards view shows no Radial layout section and no Reset radial layout button", async () => {
    const t = await setup({}, {}, "cards");
    await t.settle();
    await openMenu(t, OPEN);
    expect(t.host.textContent).not.toContain("Radial layout");
    expect(t.button(/Reset radial layout/)).toBeUndefined();
  });

  test("the toolbar Reset radial layout clears every setting", async () => {
    const t = await setup({}, {}, "radial");
    await t.settle();
    await pick(t, OPEN, "Spread: 180°");
    await pick(t, ORDER, "Distance: ×0.5");
    expect(Object.keys((await stored(t))?.radial ?? {})).toHaveLength(2);
    await t.click(t.button(/^Reset radial layout$/));
    expect((await stored(t))?.radial ?? {}).toEqual({});
  });

  test("radial edits leave the strategy untouched and Undo empty (SEP-5.14)", async () => {
    const t = await setup({}, {}, "radial");
    await t.settle();
    const undo = () => t.button(/^Undo$/) as HTMLButtonElement;
    const redo = () => t.button(/^Redo$/) as HTMLButtonElement;
    expect(undo().disabled).toBe(true);
    await pick(t, OPEN, "Spread: 90°");
    await pick(t, ORDER, "Distance: ×1.5");
    await pick(t, OPEN, "Pin here");
    await pick(t, OPEN, "Unpin");
    await pick(t, OPEN, "Reset layout of this branch");
    await t.click(t.button(/^Reset radial layout$/));
    expect(undo().disabled).toBe(true);
    expect(redo().disabled).toBe(true);
    expect(text(t)).not.toContain("●"); // not dirty: the document is unchanged
    const raw = JSON.stringify((await stored(t)) ?? {});
    expect(raw).not.toContain("schema_version");
  });

  test("Save view then Load view restores the radial settings (SEP-5.13)", async () => {
    const dl = captureDownloads();
    try {
      const t = await setup({}, {}, "radial");
      await t.settle();
      await pick(t, OPEN, "Spread: 90°");
      await pick(t, ORDER, "Distance: ×1.5");
      const before = (await stored(t))?.radial;
      expect(Object.keys(before ?? {})).toHaveLength(2);
      await t.click(t.button(/^Save view$/));
      expect(dl.saved).toHaveLength(1);
      const file = await (dl.saved[0] as { text: Promise<string> }).text;
      const parsed = JSON.parse(file);
      expect(parsed.view_version).toBe(2);
      expect(parsed.radial).toEqual(before);
      expect(file).not.toContain("schema_version");

      await t.click(t.button(/^Reset radial layout$/));
      expect((await stored(t))?.radial ?? {}).toEqual({});

      const input = t.host.querySelector('input[aria-label="Open a view (layout) file"]') as HTMLInputElement;
      const f = new File([file], "ema-crossover.view.json", { type: "application/json" });
      await t.act(async () => {
        Object.defineProperty(input, "files", { value: [f], configurable: true });
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
      await t.settle(400);
      expect((await stored(t))?.radial).toEqual(before);
      expect(t.host.textContent).toContain("Loaded the layout from ema-crossover.view.json");
      expect((t.button(/^Undo$/) as HTMLButtonElement).disabled).toBe(true);
    } finally {
      dl.restore();
    }
  });

  test("a file that is not a view file is refused with the expected shape", async () => {
    const t = await setup({}, {}, "radial");
    await t.settle();
    const input = t.host.querySelector('input[aria-label="Open a view (layout) file"]') as HTMLInputElement;
    const f = new File(["{}"], "nope.json", { type: "application/json" });
    await t.act(async () => {
      Object.defineProperty(input, "files", { value: [f], configurable: true });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await t.settle();
    expect(t.host.textContent).toContain("nope.json is not a designer view file");
  });
});
