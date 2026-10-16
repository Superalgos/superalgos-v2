import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import { type Json, type JsonObject, serialize, setAt } from "../src/lib/designer/doc";
import { EXAMPLES } from "../src/lib/designer/examples";
import { buildGraph, type Graph } from "../src/lib/designer/graph";
import { createLayoutCache, radialLayout, visibleGraph } from "../src/lib/designer/layouts";
import {
  applyRadial,
  clearBranch,
  DISTANCES,
  fanDistance,
  fanOffset,
  MAX_PIN_DISTANCE,
  MIN_PIN_DISTANCE,
  patchSetting,
  pinFromCentre,
  pruneRadial,
  type RadialSetting,
  type RadialSettings,
  resolveSettings,
  SPREADS,
  sanitizeRadial,
  stableKeys,
} from "../src/lib/designer/radial";
import { initState, reduce } from "../src/lib/designer/store";
import { loadViewFile, parseView, saveView, serializeView, viewKey } from "../src/lib/designer/view";
import { bigStrategy } from "./support/generate";

const ex = (name: string): Graph => buildGraph(EXAMPLES.find((e) => e.file === name)?.doc as Json);
const dist = (a: { x: number; y: number }, b: { x: number; y: number }) =>
  Math.hypot(a.x - b.x, a.y - b.y);
const heading = (g: Graph, p: Record<string, { x: number; y: number }>, id: string) => {
  const par = g.byId.get(id)?.parent;
  const a = p[id] as { x: number; y: number };
  const b = p[par as string] as { x: number; y: number };
  return Math.atan2(a.y - b.y, a.x - b.x);
};
const near = (a: number, b: number) => Math.abs(a - b) < 1e-6;
const subtree = (g: Graph, id: string): string[] => [
  id,
  ...(g.byId.get(id)?.children ?? []).flatMap((c) => subtree(g, c)),
];

describe("applyRadial", () => {
  const g = ex("ladder_rearm.json");
  const base = radialLayout(g);

  test("no settings gives the automatic layout, untouched", () => {
    expect(applyRadial(g, base, {})).toBe(base);
    // an empty-handed relative rebuild reproduces it, too (settings that change nothing)
    const same = applyRadial(g, base, { [g.nodes[1]?.id as string]: { distance: 1 } });
    for (const n of g.nodes) {
      expect(near((same[n.id] as { x: number }).x, (base[n.id] as { x: number }).x)).toBe(true);
      expect(near((same[n.id] as { y: number }).y, (base[n.id] as { y: number }).y)).toBe(true);
    }
  });

  test("a distance factor scales the node's distance to its parent and carries its subtree", () => {
    const id = g.nodes.find((n) => n.depth === 2 && n.children.length > 0)?.id as string;
    const parent = g.byId.get(id)?.parent as string;
    const p = applyRadial(g, base, { [id]: { distance: 2 } });
    expect(near(dist(p[id] as never, p[parent] as never), 2 * dist(base[id] as never, base[parent] as never))).toBe(true);
    // direction unchanged, descendants keep their distance to the node
    expect(near(heading(g, p, id), heading(g, base, id))).toBe(true);
    for (const c of g.byId.get(id)?.children ?? [])
      expect(near(dist(p[c] as never, p[id] as never), dist(base[c] as never, base[id] as never))).toBe(true);
  });

  test("a pin puts the node at that angle from the parent's outward direction", () => {
    const id = g.nodes.find((n) => n.depth === 2)?.id as string;
    const parent = g.byId.get(id)?.parent as string;
    const p = applyRadial(g, base, { [id]: { pin: { angle: 60, distance: 300 } } });
    expect(near(dist(p[id] as never, p[parent] as never), 300)).toBe(true);
    const out = heading(g, p, parent);
    const diff = Math.atan2(Math.sin(heading(g, p, id) - out), Math.cos(heading(g, p, id) - out));
    expect(near(diff, (60 * Math.PI) / 180)).toBe(true);
  });

  test("rotating a parent rotates its pinned and free children with it", () => {
    const parent = g.nodes.find((n) => n.depth === 1 && n.children.length > 1)?.id as string;
    const a = applyRadial(g, base, { [parent]: { pin: { angle: 0, distance: 400 } } });
    const b = applyRadial(g, base, { [parent]: { pin: { angle: 90, distance: 400 } } });
    const kids = g.byId.get(parent)?.children ?? [];
    for (const k of kids) {
      expect(near(dist(a[k] as never, a[parent] as never), dist(b[k] as never, b[parent] as never))).toBe(true);
    }
  });

  test("spread fans the children evenly around the node's outward direction", () => {
    const parent = g.nodes.find((n) => n.depth === 1 && n.children.length >= 3)?.id as string;
    const kids = g.byId.get(parent)?.children ?? [];
    const p = applyRadial(g, base, { [parent]: { spread: 90 } });
    const out = heading(g, p, parent);
    const offs = kids.map((k) => {
      const d = heading(g, p, k) - out;
      return Math.atan2(Math.sin(d), Math.cos(d)) * (180 / Math.PI);
    });
    for (let i = 0; i < kids.length; i++)
      expect(Math.abs((offs[i] as number) - fanOffset(90, i, kids.length))).toBeLessThan(1e-6);
    expect(Math.max(...offs) - Math.min(...offs)).toBeLessThanOrEqual(90);
    // fanned siblings are never closer than the minimum arc
    for (let i = 1; i < kids.length; i++)
      expect(dist(p[kids[i] as string] as never, p[kids[i - 1] as string] as never)).toBeGreaterThan(149);
  });

  test("a pinned sibling is left out of the fan; the rest share the arc", () => {
    const parent = g.nodes.find((n) => n.depth === 1 && n.children.length >= 3)?.id as string;
    const kids = g.byId.get(parent)?.children ?? [];
    const p = applyRadial(g, base, {
      [parent]: { spread: 180 },
      [kids[0] as string]: { pin: { angle: 170, distance: 500 } },
    });
    const out = heading(g, p, parent);
    const rest = kids.slice(1);
    rest.forEach((k, i) => {
      const d = heading(g, p, k) - out;
      const deg = Math.atan2(Math.sin(d), Math.cos(d)) * (180 / Math.PI);
      expect(Math.abs(deg - fanOffset(180, i, rest.length))).toBeLessThan(1e-6);
    });
  });

  test("the root cannot move; fan helpers", () => {
    const p = applyRadial(g, base, { $: { spread: 45 } });
    expect(p.$).toEqual({ x: 0, y: 0 });
    expect(fanOffset(360, 0, 1)).toBe(0);
    expect(fanDistance(360, 1)).toBe(0);
    expect(fanDistance(45, 4)).toBeGreaterThan(fanDistance(360, 4));
  });

  test("empty graph and out-of-range numbers do not produce NaN", () => {
    const empty: Graph = { nodes: [], edges: [], byId: new Map() };
    expect(applyRadial(empty, {}, { x: { spread: 90 } })).toEqual({});
    const id = g.nodes[1]?.id as string;
    const p = applyRadial(g, base, { [id]: { distance: Number.NaN, pin: { angle: 1e9, distance: 1e9 } } });
    for (const n of g.nodes) {
      const q = p[n.id] as { x: number; y: number };
      expect(Number.isFinite(q.x + q.y)).toBe(true);
    }
    expect(dist(p[id] as never, p.$ as never)).toBeLessThanOrEqual(MAX_PIN_DISTANCE + 1e-6);
  });

  test("property: random settings never give NaN; a pinned subtree moves rigidly with its root", () => {
    const settingArb: fc.Arbitrary<RadialSetting> = fc.record(
      {
        spread: fc.constantFrom(...SPREADS),
        distance: fc.constantFrom(...DISTANCES),
        pin: fc.record({
          angle: fc.double({ min: -360, max: 360, noNaN: true }),
          distance: fc.double({ min: 0, max: 8000, noNaN: true }),
        }),
      },
      { requiredKeys: [] },
    );
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 8 }),
        fc.array(fc.tuple(fc.nat(500), settingArb), { maxLength: 25 }),
        fc.nat(500),
        fc.double({ min: -180, max: 180, noNaN: true }),
        fc.double({ min: 40, max: 1500, noNaN: true }),
        (orders, picks, rootPick, angle, distance) => {
          const gr = buildGraph(bigStrategy(orders));
          const b = radialLayout(gr);
          const byId: Record<string, RadialSetting> = {};
          for (const [i, s] of picks) byId[(gr.nodes[i % gr.nodes.length] as { id: string }).id] = s;
          const p1 = applyRadial(gr, b, byId);
          for (const n of gr.nodes) {
            const q = p1[n.id] as { x: number; y: number };
            expect(Number.isFinite(q.x) && Number.isFinite(q.y)).toBe(true);
          }
          const movable = gr.nodes.filter((n) => n.parent !== null);
          const root = (movable[rootPick % movable.length] as { id: string }).id;
          const a = applyRadial(gr, b, { ...byId, [root]: { ...byId[root], pin: { angle: 10, distance: 200 } } });
          const c = applyRadial(gr, b, { ...byId, [root]: { ...byId[root], pin: { angle, distance } } });
          const ids = subtree(gr, root);
          for (const i of ids)
            for (const j of ids) {
              const da = dist(a[i] as never, a[j] as never);
              const dc = dist(c[i] as never, c[j] as never);
              expect(Math.abs(da - dc)).toBeLessThan(1e-6 * Math.max(1, da));
            }
        },
      ),
      { numRuns: 60 },
    );
  });
});

describe("pinFromCentre", () => {
  const g = ex("ema_crossover.json");
  const base = radialLayout(g);

  test("round trip: pinning a node where it is leaves the layout (nearly) unchanged", () => {
    const id = g.nodes.find((n) => n.depth === 2)?.id as string;
    const pin = pinFromCentre(base, g, id, base[id] as never);
    expect(pin).not.toBeNull();
    const p = applyRadial(g, base, { [id]: { pin: pin as never } });
    expect(dist(p[id] as never, base[id] as never)).toBeLessThan(5); // snapped to whole degrees / pixels
  });

  test("snaps to whole degrees and pixels, keeps a minimum distance, refuses the root", () => {
    const id = g.nodes.find((n) => n.depth === 1)?.id as string;
    const pin = pinFromCentre(base, g, id, { x: 0.2, y: -123.4 });
    expect(Number.isInteger(pin?.angle)).toBe(true);
    expect(Number.isInteger(pin?.distance)).toBe(true);
    expect(pin?.angle).toBe(0); // straight up is the root's outward direction
    expect(pinFromCentre(base, g, id, { x: 0, y: 0 })?.distance).toBe(MIN_PIN_DISTANCE);
    expect(pinFromCentre(base, g, "$", { x: 5, y: 5 })).toBeNull();
    expect(pinFromCentre(base, g, "nope", { x: 5, y: 5 })).toBeNull();
    expect(pinFromCentre(base, g, id, { x: Number.NaN, y: 1 })).toBeNull();
  });
});

describe("stable keys", () => {
  const doc = EXAMPLES.find((e) => e.file === "ladder_rearm.json")?.doc as JsonObject;

  test("inserting an order above a configured one keeps its settings on the same order", () => {
    const g1 = buildGraph(doc);
    const k1 = stableKeys(doc, g1);
    const orders = ((doc.stages as JsonObject).open as JsonObject).orders as Json[];
    const target = orders[1] as JsonObject;
    const n1 = g1.nodes.find((n) => n.path.join(".") === ["stages", "open", "orders", 1].join("."));
    expect(n1).toBeDefined();
    const key = k1.get(n1?.id as string) as string;
    expect(key).toContain(JSON.stringify(target.id));

    const fresh = { ...(orders[0] as JsonObject), id: "brand_new" };
    const doc2 = setAt(doc, ["stages", "open", "orders"], [orders[0], fresh, ...orders.slice(1)] as Json) as JsonObject;
    const g2 = buildGraph(doc2);
    const k2 = stableKeys(doc2, g2);
    const moved = g2.nodes.find((n) => n.path.join(".") === ["stages", "open", "orders", 2].join("."));
    expect(moved?.id).not.toBe(n1?.id); // the path id shifted ...
    expect(k2.get(moved?.id as string)).toBe(key); // ... the key did not

    // so the same settings land on the same order, and its children
    const settings: RadialSettings = { [key]: { distance: 2, spread: 90 } };
    expect(resolveSettings(settings, k1, g1)[n1?.id as string]).toEqual({ distance: 2, spread: 90 });
    expect(resolveSettings(settings, k2, g2)[moved?.id as string]).toEqual({ distance: 2, spread: 90 });
    expect(Object.keys(resolveSettings(settings, k2, g2))).toEqual([moved?.id as string]);
  });

  test("situations use their name; duplicate names fall back to the position", () => {
    const mk = (a: string, b: string): JsonObject => {
      const d = structuredClone(doc) as JsonObject;
      const ev = ((d.stages as JsonObject).open as JsonObject).when as JsonObject;
      const list = ev.any as JsonObject[];
      list.length = 0;
      list.push({ name: a, all: ["true"] }, { name: b, all: ["true"] });
      return d;
    };
    const keysOf = (d: JsonObject) => [...stableKeys(d, buildGraph(d)).values()].filter((k) => k.startsWith("$.stages.open.when.any"));
    expect(keysOf(mk("a", "b")).some((k) => k.includes('[name="a"]'))).toBe(true);
    const dup = keysOf(mk("same", "same"));
    expect(dup.some((k) => k.includes("name="))).toBe(false);
    expect(dup.some((k) => k.endsWith("any[1]"))).toBe(true);
  });

  test("keys are unique across the graph for every example", () => {
    for (const e of EXAMPLES) {
      const d = e.doc as JsonObject;
      const keys = [...stableKeys(d, buildGraph(d)).values()];
      expect(new Set(keys).size).toBe(keys.length);
    }
  });
});

describe("settings editing", () => {
  test("patchSetting sets, clears and drops empty entries without mutating", () => {
    const a: RadialSettings = {};
    const b = patchSetting(a, "k", { spread: 90, distance: 2 });
    expect(a).toEqual({});
    expect(b.k).toEqual({ spread: 90, distance: 2 });
    const c = patchSetting(b, "k", { spread: null, pin: { angle: 5, distance: 100 } });
    expect(c.k).toEqual({ distance: 2, pin: { angle: 5, distance: 100 } });
    expect(patchSetting(c, "k", { distance: 1, pin: null })).toEqual({});
    expect(patchSetting({}, "k", { distance: null })).toEqual({});
  });

  test("clearBranch removes a node and everything below it, collapsed or not", () => {
    const d = EXAMPLES.find((e) => e.file === "ladder_rearm.json")?.doc as JsonObject;
    const g = buildGraph(d);
    const keys = stableKeys(d, g);
    const open = g.nodes.find((n) => n.title === "Open")?.id as string;
    const all: RadialSettings = {};
    let s = all;
    for (const n of g.nodes) s = patchSetting(s, keys.get(n.id) as string, { distance: 2 });
    const cleared = clearBranch(s, g, keys, open);
    const below = new Set(subtree(g, open).map((i) => keys.get(i)));
    for (const [k] of Object.entries(cleared)) expect(below.has(k)).toBe(false);
    expect(Object.keys(cleared).length).toBe(g.nodes.length - below.size);
  });

  test("pruneRadial keeps only live keys", () => {
    expect(Object.keys(pruneRadial({ a: { spread: 90 }, b: { spread: 45 } }, new Set(["b"])))).toEqual(["b"]);
  });

  test("sanitizeRadial drops what it cannot trust", () => {
    expect(sanitizeRadial(null)).toEqual({});
    expect(sanitizeRadial([1])).toEqual({});
    const out = sanitizeRadial({
      a: { spread: 90, distance: 1.5, pin: { angle: 190, distance: 100 } },
      b: { spread: 77, distance: 99, pin: { angle: "x", distance: 1 } },
      c: 3,
      d: { distance: 1 },
      e: { pin: { angle: 0, distance: 1 } },
    });
    expect(out.a).toEqual({ spread: 90, distance: 1.5, pin: { angle: -170, distance: 100 } });
    expect(out.b).toBeUndefined();
    expect(out.c).toBeUndefined();
    expect(out.d).toBeUndefined();
    expect(out.e?.pin?.distance).toBe(MIN_PIN_DISTANCE);
  });
});

describe("layout cache with settings", () => {
  const doc = EXAMPLES.find((e) => e.file === "ladder_rearm.json")?.doc as JsonObject;

  test("typing in a condition recomputes neither the layout nor the settings", () => {
    const cache = createLayoutCache();
    const g1 = buildGraph(doc);
    const cond = g1.nodes.find((n) => n.kind === "condition") as { path: (string | number)[] };
    const open = g1.nodes.find((n) => n.title === "Open")?.id as string;
    const settings: RadialSettings = { [stableKeys(doc, g1).get(open) as string]: { spread: 90 } };
    let text = "close > 1";
    let first: unknown = null;
    for (let i = 0; i < 20; i++) {
      text += " ";
      const d = setAt(doc, cond.path, text) as JsonObject;
      const g = buildGraph(d);
      const v = visibleGraph(g, new Set());
      const p = cache.get("radial", v, resolveSettings(settings, stableKeys(d, g), v));
      first ??= p;
      expect(p).toBe(first);
    }
    expect(cache.computed()).toBe(1);
    expect(cache.applied()).toBe(1);
  });

  test("changing a setting re-applies on the cached layout without recomputing it", () => {
    const cache = createLayoutCache();
    const v = visibleGraph(buildGraph(doc), new Set());
    const id = v.nodes[1]?.id as string;
    const a = cache.get("radial", v, { [id]: { distance: 2 } });
    const b = cache.get("radial", v, { [id]: { distance: 1.5 } });
    expect(a).not.toBe(b);
    expect(cache.computed()).toBe(1);
    expect(cache.applied()).toBe(2);
    // no settings: the automatic layout itself; other modes ignore settings
    expect(cache.get("radial", v, {})).toBe(cache.get("radial", v));
    const cards = cache.get("cards", v, { [id]: { distance: 2 } });
    expect(cards).toBe(cache.get("cards", v));
  });
});

describe("view file v2", () => {
  const radial: RadialSettings = {
    '$.stages.open[id="x"]': { spread: 90, distance: 1.5, pin: { angle: 12.34, distance: 250.6 } },
  };

  test("round trips through the file and storage", () => {
    const text = serializeView({ strategy: "s", positions: { a: { x: 1.2, y: 2 } }, radial });
    const back = parseView(text);
    expect(back?.view_version).toBe(2);
    expect(back?.radial).toEqual({
      '$.stages.open[id="x"]': { spread: 90, distance: 1.5, pin: { angle: 12.3, distance: 251 } },
    });
    expect(back?.positions).toEqual({ a: { x: 1, y: 2 } });
    const mem = new Map<string, string>();
    const store = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    saveView(store, "s", {}, radial);
    expect([...mem.keys()]).toEqual([viewKey("s")]);
    expect(loadViewFile(store, "s").radial['$.stages.open[id="x"]']?.spread).toBe(90);
    expect(loadViewFile(store, "none")).toEqual({ positions: {}, radial: {} });
  });

  test("version 1 files still load, with no radial settings", () => {
    const v1 = '{"view_version":1,"strategy":"s","positions":{"a":{"x":1,"y":2}}}';
    expect(parseView(v1)).toEqual({
      view_version: 2,
      strategy: "s",
      positions: { a: { x: 1, y: 2 } },
      radial: {},
    });
    // a v1 file that smuggles a radial key does not get it honoured
    expect(parseView('{"view_version":1,"strategy":"s","positions":{},"radial":{"k":{"spread":90}}}')?.radial).toEqual({});
    // a v2 file without the radial block is fine
    expect(parseView('{"view_version":2,"strategy":"s","positions":{}}')?.radial).toEqual({});
  });

  test("serializing without radial writes an empty block", () => {
    expect(JSON.parse(serializeView({ strategy: "s", positions: {} })).radial).toEqual({});
  });

  test("radial settings never touch the strategy document (SEP-5.10)", () => {
    const d = EXAMPLES[0]?.doc as JsonObject;
    const s0 = initState(structuredClone(d), "x.json");
    const before = serialize(s0.history.present);
    let s = reduce(s0, { type: "radial", radial });
    s = reduce(s, { type: "radial", radial: patchSetting(radial, "k", { distance: 2 }) });
    const mem = new Map<string, string>();
    saveView({ getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => void mem.set(k, v) }, "x", s.positions, s.radial);
    expect(serialize(s.history.present)).toBe(before);
    expect(s.baseline).toBe(s0.baseline);
    expect(s.history.past).toHaveLength(0);
    expect(serialize(s.history.present)).not.toContain("spread");
    expect(reduce(s, { type: "radial", radial: {} }).radial).toEqual({});
  });
});
