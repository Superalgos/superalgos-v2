import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import { type Json, getAt } from "../src/lib/designer/doc";
import { caretLine, indexIssues, mapIssues } from "../src/lib/designer/errors";
import { EXAMPLES } from "../src/lib/designer/examples";
import { NODE_H, NODE_W, buildGraph, layout } from "../src/lib/designer/graph";
import { bigStrategy } from "./support/generate";

const ex = (name: string): Json => EXAMPLES.find((e) => e.file === name)?.doc as Json;

describe("graph", () => {
  test("trend_phases shows the stages, phases, events, situations and conditions", () => {
    const g = buildGraph(ex("trend_phases.json"));
    const titles = g.nodes.map((n) => n.title);
    expect(titles[0]).toBe("trend-phases");
    for (const t of ["Trigger", "Open", "Manage", "Close", "Stop (phases)", "Target (phases)", "Trigger on", "Take position when", "Next phase when", "Close when", "initial", "break_even", "trail", "entry", "Level"]) {
      expect(titles, t).toContain(t);
    }
    // stages hang from the strategy (the schema's `stages` wrapper is not a node)
    expect(g.byId.get("$.stages.open")?.parent).toBe("$");
    expect(g.byId.get("$.stages.open.orders[0]")?.summary).toBe("market · 95%");
    expect(g.byId.get("$.stages.manage.stop.phases[0].level")?.kind).toBe("formula");
    expect(g.byId.get("$.stages.manage.stop.phases[0].next.any[0].all[0]")?.summary).toBe("close > entry_price + 1.5 * atr(14)");
    expect(g.byId.get("$.stages.manage.stop.phases[2].next")).toBeUndefined();
  });

  test("ladder: orders carry create and cancel events", () => {
    const g = buildGraph(ex("ladder_rearm.json"));
    const o = g.byId.get("$.stages.open.orders[3]");
    expect(o?.children.map((c) => g.byId.get(c)?.title)).toEqual(["Rate", "Create order when", "Cancel order when"]);
    expect(o?.summary).toContain("limit");
    expect(o?.summary).toContain("re-arm");
  });

  test("every node has one parent, one edge, and a path that exists in the document", () => {
    for (const e of EXAMPLES) {
      const g = buildGraph(e.doc);
      expect(g.edges).toHaveLength(g.nodes.length - 1);
      for (const n of g.nodes) {
        expect(getAt(e.doc, n.path)).not.toBeUndefined();
        if (n.parent) expect(g.byId.get(n.parent)?.children).toContain(n.id);
      }
    }
  });

  test("layout: no two nodes overlap and parents sit left of their children", () => {
    fc.assert(
      fc.property(fc.integer({ min: 1, max: 12 }), (orders) => {
        const g = buildGraph(bigStrategy(orders));
        const pos = layout(g);
        const boxes = g.nodes.map((n) => pos[n.id] as { x: number; y: number });
        for (let i = 0; i < boxes.length; i++) {
          for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i] as { x: number; y: number };
            const b = boxes[j] as { x: number; y: number };
            if (Math.abs(a.x - b.x) < NODE_W && Math.abs(a.y - b.y) < NODE_H) return false;
          }
        }
        return g.nodes.every((n) => !n.parent || (pos[n.id]?.x ?? 0) > (pos[n.parent]?.x ?? 0));
      }),
      { numRuns: 25 },
    );
  });

  test("saved positions override the automatic ones, unknown ids are ignored", () => {
    const g = buildGraph(ex("ema_crossover.json"));
    const pos = layout(g, { "$.stages.open": { x: 5, y: 6 }, "$.gone": { x: 1, y: 1 } });
    expect(pos["$.stages.open"]).toEqual({ x: 5, y: 6 });
    expect(pos["$.gone"]).toBeUndefined();
  });

  test("an empty or odd document does not crash", () => {
    expect(buildGraph({}).nodes).toHaveLength(1);
    expect(buildGraph({ stages: { open: { when: { any: [] }, orders: [] } } }).nodes.length).toBeGreaterThan(1);
    expect(layout(buildGraph({}))["$"]).toEqual({ x: 0, y: 0 });
  });

  test("the generated 200-node document has the size the brief asks for", () => {
    const n = buildGraph(bigStrategy(14)).nodes.length;
    expect(n).toBeGreaterThanOrEqual(200);
  });
});

describe("error mapping (SEP-5.8)", () => {
  const g = buildGraph(ex("trend_phases.json"));

  test("a formula error lands on its formula node with its column", () => {
    const [m] = mapIssues([{ path: "$.stages.manage.stop.phases[0].level", column: 7, message: "bad" }], g);
    expect(m).toMatchObject({ nodeId: "$.stages.manage.stop.phases[0].level", field: null, column: 7 });
  });

  test("a condition error lands on its condition node", () => {
    const [m] = mapIssues([{ path: "$.stages.open.when.any[0].all[1]", column: 8, message: "x" }], g);
    expect(m?.nodeId).toBe("$.stages.open.when.any[0].all[1]");
  });

  test("a scalar field error lands on the owning node with the field name", () => {
    const [a, b, c] = mapIssues(
      [
        { path: "$.stages.open.orders[0].size_fraction", column: null, message: "m" },
        { path: "$.stages.open.orders[0].take_profit_pct", column: null, message: "m" },
        { path: "$.stages.manage.stop.phases[1].goto[2].to", column: null, message: "m" },
      ],
      g,
    );
    expect(a).toMatchObject({ nodeId: "$.stages.open.orders[0]", field: "size_fraction" });
    expect(b?.field).toBe("take_profit_pct");
    // goto[2] does not exist in the document: falls back to the phase, keeping the rest of the path
    expect(c).toMatchObject({ nodeId: "$.stages.manage.stop.phases[1]", field: "goto[2].to" });
  });

  test("document-level problems go to the root", () => {
    const [a, b, c] = mapIssues(
      [
        { path: "$", column: null, message: "not an object" },
        { path: "$.timeframe", column: null, message: "bad" },
        { path: "garbage", column: null, message: "?" },
      ],
      g,
    );
    expect(a).toMatchObject({ nodeId: "$", field: null });
    expect(b).toMatchObject({ nodeId: "$", field: "timeframe" });
    expect(c).toMatchObject({ nodeId: "$", field: "garbage" });
    expect(mapIssues([{ path: "$.x", column: null, message: "" }], { nodes: [], edges: [], byId: new Map() })[0]?.nodeId).toBe("$");
  });

  test("ancestors learn that a problem is inside them", () => {
    const mapped = mapIssues([{ path: "$.stages.manage.stop.phases[0].level", column: 1, message: "m" }], g);
    const idx = indexIssues(mapped, g);
    expect(idx.own.get("$.stages.manage.stop.phases[0].level")).toHaveLength(1);
    expect([...idx.below].sort()).toEqual(["$", "$.stages.manage", "$.stages.manage.stop", "$.stages.manage.stop.phases[0]"]);
    const two = indexIssues(mapIssues([{ path: "$.stages.manage.stop.phases[0].level", column: 1, message: "a" }, { path: "$.stages.manage.stop.phases[0].level", column: 2, message: "b" }], g), g);
    expect(two.own.get("$.stages.manage.stop.phases[0].level")).toHaveLength(2);
  });

  test("caret line", () => {
    expect(caretLine("close > ", 8)).toBe("        ^");
    expect(caretLine("ab", 99)).toBe("  ^");
    expect(caretLine("ab", null)).toBeNull();
    expect(caretLine("ab", -1)).toBeNull();
  });
});
