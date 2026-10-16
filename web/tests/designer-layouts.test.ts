import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import { V1_ICONS } from "../src/assets/v1-icons";
import { type Json, setAt } from "../src/lib/designer/doc";
import { EXAMPLES } from "../src/lib/designer/examples";
import { buildGraph, type Graph, type NodeKind } from "../src/lib/designer/graph";
import {
  BIG_GRAPH,
  createLayoutCache,
  defaultCollapsed,
  descendantCounts,
  expandTo,
  iconFor,
  isLayoutMode,
  layoutFor,
  radialLayout,
  structureKey,
  toggled,
  visibleGraph,
} from "../src/lib/designer/layouts";
import { bigStrategy } from "./support/generate";

const ex = (name: string): Graph =>
  buildGraph(EXAMPLES.find((e) => e.file === name)?.doc as Json);
const finite = (p: { x: number; y: number }) => Number.isFinite(p.x) && Number.isFinite(p.y);

function minDistance(ps: { x: number; y: number }[]): number {
  let m = Number.POSITIVE_INFINITY;
  for (let i = 0; i < ps.length; i++)
    for (let j = i + 1; j < ps.length; j++)
      m = Math.min(m, Math.hypot((ps[i]?.x ?? 0) - (ps[j]?.x ?? 0), (ps[i]?.y ?? 0) - (ps[j]?.y ?? 0)));
  return m;
}

describe("collapse", () => {
  test("small strategies start fully expanded, big ones collapse the situations", () => {
    expect(defaultCollapsed(ex("ema_crossover.json")).size).toBe(0);
    expect(defaultCollapsed(ex("trend_phases.json")).size).toBe(0); // 37 nodes
    const big = ex("ladder_rearm.json"); // 93 nodes
    const c = defaultCollapsed(big);
    expect(c.size).toBeGreaterThan(0);
    for (const id of c) expect(big.byId.get(id)?.kind).toBe("situation");
    // stages stay open
    for (const n of big.nodes) if (n.kind === "stage") expect(c.has(n.id)).toBe(false);
    expect(defaultCollapsed(ex("trend_phases.json"), 10).size).toBeGreaterThan(0);
    expect(BIG_GRAPH).toBe(40);
  });

  test("visibleGraph hides the subtree of a collapsed node and keeps the original intact", () => {
    const g = ex("ema_crossover.json");
    const sit = g.nodes.find((n) => n.kind === "situation" && n.children.length > 0);
    expect(sit).toBeDefined();
    const id = sit?.id as string;
    const v = visibleGraph(g, new Set([id]));
    expect(v.nodes.length).toBe(g.nodes.length - (descendantCounts(g).get(id) ?? 0));
    expect(v.byId.get(id)?.children).toEqual([]);
    expect(g.byId.get(id)?.children.length).toBeGreaterThan(0);
    expect(v.edges.every((e) => v.byId.has(e.source) && v.byId.has(e.target))).toBe(true);
    expect(v.edges.length).toBe(v.nodes.length - 1);
    expect(visibleGraph(g, new Set()).nodes.length).toBe(g.nodes.length);
  });

  test("descendantCounts: root counts every other node, leaves count zero", () => {
    const g = ex("grid_10_level.json");
    const c = descendantCounts(g);
    expect(c.get("$")).toBe(g.nodes.length - 1);
    for (const n of g.nodes) if (n.children.length === 0) expect(c.get(n.id)).toBe(0);
  });

  test("toggled and expandTo", () => {
    const a = new Set(["x"]);
    expect([...toggled(a, "x")]).toEqual([]);
    expect([...toggled(a, "y")].sort()).toEqual(["x", "y"]);
    expect(a.size).toBe(1); // input is not mutated
    const g = ex("ladder_rearm.json");
    const all = new Set(g.nodes.map((n) => n.id));
    const leaf = g.nodes.find((n) => n.kind === "condition") as { id: string; parent: string | null };
    const opened = expandTo(g, all, leaf.id);
    expect(opened.has(leaf.parent as string)).toBe(false);
    expect(opened.has("$")).toBe(false);
    expect(opened.has(leaf.id)).toBe(true);
    // every node on the way to the leaf is visible
    expect(visibleGraph(g, opened).byId.has(leaf.id)).toBe(true);
  });

  test("an empty graph is handled", () => {
    const empty: Graph = { nodes: [], edges: [], byId: new Map() };
    expect(visibleGraph(empty, new Set()).nodes).toEqual([]);
    expect(radialLayout(empty)).toEqual({});
    expect(defaultCollapsed(empty).size).toBe(0);
  });
});

describe("radial layout", () => {
  test("strategy at the centre, rings grow with depth, every position finite", () => {
    for (const e of EXAMPLES) {
      const g = buildGraph(e.doc as Json);
      const p = radialLayout(g);
      expect(Object.keys(p).length).toBe(g.nodes.length);
      expect(p.$).toEqual({ x: 0, y: 0 });
      let prev = 0;
      const byDepth = new Map<number, number>();
      for (const n of g.nodes) {
        const pos = p[n.id] as { x: number; y: number };
        expect(finite(pos)).toBe(true);
        byDepth.set(n.depth, Math.hypot(pos.x, pos.y));
      }
      for (const [d, r] of [...byDepth].sort((a, b) => a[0] - b[0])) {
        if (d > 0) expect(r).toBeGreaterThan(prev);
        prev = r;
      }
    }
  });

  test("a lone strategy node and a single chain do not produce NaN", () => {
    const g = visibleGraph(ex("ema_crossover.json"), new Set(["$"]));
    expect(radialLayout(g)).toEqual({ $: { x: 0, y: 0 } });
  });

  test("property: nodes never sit on top of each other, for any collapse state", () => {
    fc.assert(
      fc.property(fc.integer({ min: 1, max: 14 }), fc.array(fc.nat(300), { maxLength: 40 }), (orders, picks) => {
        const g = buildGraph(bigStrategy(orders));
        const withKids = g.nodes.filter((n) => n.children.length > 0 && n.parent !== null);
        const collapsed = new Set(picks.map((i) => withKids[i % withKids.length]?.id as string));
        const v = visibleGraph(g, collapsed);
        const p = radialLayout(v);
        const pts = v.nodes.map((n) => p[n.id] as { x: number; y: number });
        expect(pts.every(finite)).toBe(true);
        expect(Object.keys(p).length).toBe(v.nodes.length);
        // same-ring neighbours are at least ~MIN_ARC along a chord; use a looser, safe bound
        expect(minDistance(pts)).toBeGreaterThan(90);
      }),
      { numRuns: 40 },
    );
  });
});

describe("layout cache (typing guard)", () => {
  const doc = EXAMPLES.find((e) => e.file === "ladder_rearm.json")?.doc as Json;

  test("editing a condition's text does not recompute the layout", () => {
    const cache = createLayoutCache();
    const g1 = buildGraph(doc);
    const cond = g1.nodes.find((n) => n.kind === "condition") as { path: (string | number)[] };
    const p1 = cache.get("radial", visibleGraph(g1, new Set()));
    let text = "close > 1";
    for (let i = 0; i < 20; i++) {
      text += " ";
      const g2 = buildGraph(setAt(doc, cond.path, text));
      expect(cache.get("radial", visibleGraph(g2, new Set()))).toBe(p1);
    }
    expect(cache.computed()).toBe(1);
  });

  test("a structure, mode or collapse change recomputes", () => {
    const cache = createLayoutCache();
    const g = buildGraph(doc);
    const full = visibleGraph(g, new Set());
    const a = cache.get("cards", full);
    expect(cache.get("radial", full)).not.toBe(a);
    const first = g.nodes.find((n) => n.kind === "situation") as { id: string };
    cache.get("radial", visibleGraph(g, new Set([first.id])));
    expect(cache.computed()).toBe(3);
    expect(structureKey("radial", full)).not.toBe(structureKey("cards", full));
  });

  test("layoutFor: cards has no positions of its own", () => {
    const g = ex("ema_crossover.json");
    expect(layoutFor("cards", g)).toEqual({});
    expect(Object.keys(layoutFor("radial", g)).length).toBe(g.nodes.length);
    expect(isLayoutMode("lanes")).toBe(false);
    expect(isLayoutMode("radial")).toBe(true);
    expect(isLayoutMode("grid")).toBe(false);
  });
});

describe("icons", () => {
  test("every node of every example maps to a bundled icon", () => {
    const seen = new Set<NodeKind>();
    for (const e of EXAMPLES)
      for (const n of buildGraph(e.doc as Json).nodes) {
        seen.add(n.kind);
        const file = iconFor(n.kind, n.title);
        expect(V1_ICONS[file], `${n.kind} ${n.title} -> ${file}`).toBeTruthy();
      }
    expect(seen.size).toBeGreaterThanOrEqual(9);
  });

  test("stage and level icons depend on the title", () => {
    expect(iconFor("stage", "Open")).toBe("open-stage.png");
    expect(iconFor("stage", "Close")).toBe("close-stage.png");
    expect(iconFor("level", "Stop (phases)")).toBe("stop-loss.png");
    expect(iconFor("level", "Target (phases)")).toBe("take-profit.png");
    expect(iconFor("event", "Something new")).toBe("execution-started-event.png");
  });
});
