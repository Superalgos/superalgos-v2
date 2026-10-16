import { describe, expect, test } from "bun:test";
import { type Json, type JsonObject, getAt, serialize } from "../src/lib/designer/doc";
import { EXAMPLES } from "../src/lib/designer/examples";
import { actionsFor } from "../src/lib/designer/rules";
import { COALESCE_MS, type DesignerEvent, initState, reduce } from "../src/lib/designer/store";

const doc = EXAMPLES[0]?.doc as JsonObject;
const run = (events: DesignerEvent[]) => events.reduce(reduce, initState(doc, "ema_crossover.json"));
const COND = ["stages", "open", "when", "any", 0, "all", 0];

describe("designer state", () => {
  test("editing a condition changes only that string and marks the document unsaved (SEP-5.9)", () => {
    const s = run([{ type: "text", path: COND, value: "close > ema(close, 20)", now: 0 }]);
    expect(getAt(s.history.present, COND)).toBe("close > ema(close, 20)");
    expect(serialize(s.history.present)).not.toBe(s.baseline);
    const back = reduce(s, { type: "text", path: COND, value: getAt(doc, COND) as string, now: 5 });
    expect(serialize(back.history.present)).toBe(s.baseline);
  });

  test("typing in one field is one undo step, a pause or another field starts a new one", () => {
    const s = run([
      { type: "text", path: COND, value: "a", now: 0 },
      { type: "text", path: COND, value: "ab", now: 100 },
      { type: "text", path: COND, value: "abc", now: 200 },
    ]);
    expect(s.history.past).toHaveLength(1);
    const later = reduce(s, { type: "text", path: COND, value: "abcd", now: 200 + COALESCE_MS + 1 });
    expect(later.history.past).toHaveLength(2);
    const other = reduce(s, { type: "field", path: ["stages", "open", "orders", 0], key: "size_fraction", value: 0.5, now: 300 });
    expect(other.history.past).toHaveLength(2);
    expect(reduce(other, { type: "undo" }).history.present).toBe(s.history.present);
  });

  test("actions select the new node and keep positions with their nodes", () => {
    let s = initState(doc, "x");
    s = reduce(s, { type: "positions", positions: { "$.stages.open.when.any[0]": { x: 1, y: 2 } } });
    const add = actionsFor(s.history.present, ["stages", "open", "when"]).find((a) => a.label === "Add situation");
    if (!add) throw new Error("expected an Add situation action");
    s = reduce(s, { type: "action", action: add });
    expect(s.selected).toBe("$.stages.open.when.any[1]");
    expect(s.positions["$.stages.open.when.any[0]"]).toEqual({ x: 1, y: 2 });
    // moving the new one up pushes the saved position down with its node
    const up = actionsFor(s.history.present, ["stages", "open", "when", "any", 1]).find((a) => a.type === "move");
    if (!up) throw new Error("expected a move action");
    s = reduce(s, { type: "action", action: up });
    expect(s.positions["$.stages.open.when.any[1]"]).toEqual({ x: 1, y: 2 });
    expect(s.selected).toBe("$.stages.open.when.any[0]");
  });

  test("undo drops a selection that no longer exists", () => {
    let s = initState(doc, "x");
    const add = actionsFor(doc, ["stages", "open", "when"]).find((a) => a.label === "Add situation") as never;
    s = reduce(s, { type: "action", action: add });
    expect(s.selected).toBe("$.stages.open.when.any[1]");
    s = reduce(s, { type: "undo" });
    expect(s.selected).toBe("$");
    s = reduce(s, { type: "redo" });
    expect(getAt(s.history.present, ["stages", "open", "when", "any", 1])).toBeDefined();
  });

  test("load, select, reset layout and save", () => {
    let s = run([{ type: "select", id: "$.stages.open" }, { type: "positions", positions: { a: { x: 1, y: 1 } } }]);
    expect(s.selected).toBe("$.stages.open");
    s = reduce(s, { type: "resetLayout" });
    expect(s.positions).toEqual({});
    s = reduce(s, { type: "field", path: [], key: "description", value: "changed", now: 1 });
    expect(serialize(s.history.present)).not.toBe(s.baseline);
    s = reduce(s, { type: "saved", fileName: "y.json" });
    expect(s.baseline).toBe(serialize(s.history.present));
    expect(s.fileName).toBe("y.json");
    const loaded = reduce(s, { type: "load", doc: EXAMPLES[1]?.doc as JsonObject, fileName: "z.json", positions: { q: { x: 0, y: 0 } }, text: "TEXT" });
    expect(loaded).toMatchObject({ fileName: "z.json", baseline: "TEXT", selected: "$", positions: { q: { x: 0, y: 0 } } });
  });

  test("clearing a field removes the key", () => {
    const s = run([{ type: "field", path: ["stages", "open", "orders", 0], key: "stop_loss_pct", value: undefined, now: 0 }]);
    expect(serialize(s.history.present as Json)).not.toContain("stop_loss_pct");
  });
});

test("the designer has a route and a nav entry", async () => {
  const { parseHash, NAV } = await import("../src/lib/route");
  expect(parseHash("#/designer")).toEqual({ page: "designer" });
  expect(NAV.some((n) => n.page === "designer" && n.href === "#/designer")).toBe(true);
});
