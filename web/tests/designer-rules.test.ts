import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import { type Json, type Path, collectIds, getAt, isObject, pathToId, serialize } from "../src/lib/designer/doc";
import { EXAMPLES, NEW_STRATEGY } from "../src/lib/designer/examples";
import { buildGraph } from "../src/lib/designer/graph";
import { type Action, actionsFor, applyAction, itemLabel, propLabel, setField } from "../src/lib/designer/rules";
import {
  KEY_ORDER,
  TRANSPARENT_DEFS,
  defaultFor,
  propsOf,
  resolve,
  rootSchema,
  schemaAt,
  singular,
} from "../src/lib/designer/schema";
import { structureIssues } from "../src/lib/designer/structure";

const ex = (name: string): Json => EXAMPLES.find((e) => e.file === name)?.doc as Json;
const labels = (doc: Json, path: Path) => actionsFor(doc, path).map((a) => a.label);

describe("schema", () => {
  test("KEY_ORDER lists exactly the schema's properties for every model", () => {
    const defs = rootSchema.$defs ?? {};
    for (const [name, node] of Object.entries({ Strategy: rootSchema, ...defs })) {
      if (!node.properties) continue;
      expect([...(KEY_ORDER[name] ?? [])].sort(), `model ${name}`).toEqual(Object.keys(node.properties).sort());
    }
  });

  test("resolve follows $ref and unwraps nullable", () => {
    const stop = resolve(rootSchema.$defs?.Manage?.properties?.stop as never);
    expect(stop.def).toBe("PhasedLevel");
    expect(stop.nullable).toBe(true);
    expect(stop.description).toContain("stop loss");
    expect(() => resolve({ $ref: "#/$defs/Nope" })).toThrow(/does not point into \$defs/);
    expect(() => resolve({ anyOf: [{ type: "null" }] })).toThrow(/only null/);
  });

  test("schemaAt walks properties and list items", () => {
    expect(schemaAt(["stages", "open", "orders", 0])?.def).toBe("OrderSpec");
    expect(schemaAt(["stages", "open", "orders", 0, "rate"])?.schema["x-dsl"]).toBe("number");
    expect(schemaAt(["stages", "open", "when", "any", 0, "all", 1])?.schema["x-dsl"]).toBe("boolean");
    expect(schemaAt(["stages", "nope"])).toBeNull();
    expect(schemaAt(["stages", 0])).toBeNull();
    expect(schemaAt([])?.def).toBeUndefined();
  });

  test("defaults are derived from required fields, with DSL seeds", () => {
    const event = defaultFor(schemaAt(["stages", "open", "when"]) as never);
    expect(event).toEqual({ any: [{ name: "new situation", all: ["true"] }] });
    expect(defaultFor(schemaAt(["stages", "open", "orders", 0, "rate"]) as never)).toBe("close");
    expect(defaultFor(schemaAt(["stages", "manage", "rules", 0]) as never)).toEqual({ id: "", when: { any: [{ name: "new situation", all: ["true"] }] }, action: "cancel_pending" });
    expect(defaultFor(schemaAt(["schema_version"]) as never)).toBe(2);
    expect(defaultFor(schemaAt(["timeframe"]) as never)).toBe("1m");
    expect(defaultFor(schemaAt(["name"]) as never)).toBe("");
    expect(defaultFor(schemaAt(["stages", "open", "orders", 0, "rearm"]) as never)).toBe(false);
    expect(defaultFor(schemaAt(["stages", "open", "orders", 0, "offset_pct"]) as never)).toBe(0);
    expect(defaultFor({ schema: { type: "number", exclusiveMinimum: 0 }, nullable: false })).toBe(1);
    expect(defaultFor({ schema: { type: "number" }, nullable: false })).toBe(0);
    expect(defaultFor({ schema: {}, nullable: false })).toBeNull();
    expect(defaultFor({ schema: { type: "array" }, nullable: false })).toEqual([]);
    expect(TRANSPARENT_DEFS.has("Stages")).toBe(true);
    expect(singular("orders")).toBe("order");
    expect(singular("any")).toBe("situation");
    expect(singular("all")).toBe("condition");
    expect(singular("goto")).toBe("goto");
    expect(singular("foo")).toBe("foo");
    expect(propsOf(resolve(rootSchema)).map((p) => p.key)).toEqual(KEY_ORDER.Strategy as string[]);
  });
});

describe("child rules come from the schema", () => {
  test("the root offers the optional stages that are missing, never the required ones", () => {
    const doc = ex("ema_crossover.json");
    expect(labels(doc, [])).toEqual(["Add manage stage"]);
    expect(labels(NEW_STRATEGY, [])).toEqual(["Add manage stage", "Add close stage"]);
  });

  test("an order offers the optional events and formulas it lacks", () => {
    const doc = ex("ema_crossover.json");
    const l = labels(doc, ["stages", "open", "orders", 0]);
    expect(l).toEqual(["Add size formula", "Add rate formula", "Add create event", "Add cancel event", "Duplicate order"]);
    const ladder = ex("ladder_rearm.json");
    expect(labels(ladder, ["stages", "open", "orders", 0])).toEqual(["Add size formula", "Remove order", "Move order down", "Duplicate order"]);
  });

  test("lists grow until maxItems and shrink to minItems", () => {
    const doc = ex("ema_crossover.json");
    const ev = ["stages", "open", "when"];
    expect(labels(doc, ev)).toContain("Add situation");
    // a single situation can not be removed (minItems 1), a second one can
    expect(labels(doc, [...ev, "any", 0])).not.toContain("Remove situation");
    const two = applyAction(doc, actionsFor(doc, ev).find((a) => a.label === "Add situation") as Action).doc;
    expect(labels(two, [...ev, "any", 0])).toContain("Remove situation");
    expect(labels(two, [...ev, "any", 1])).toContain("Move situation up");
    expect(labels(two, [...ev, "any", 1])).not.toContain("Move situation down");
    // the event's only orders list can not drop to zero
    expect(labels(doc, ["stages", "open", "orders", 0])).not.toContain("Remove order");
    // 20 situations is the cap
    let full = doc;
    for (let i = 0; i < 25; i++) {
      const add = actionsFor(full, ev).find((a) => a.label === "Add situation");
      if (!add) break;
      full = applyAction(full, add).doc;
    }
    expect(getAt(full, [...ev, "any"])).toHaveLength(20);
    expect(labels(full, ev)).not.toContain("Add situation");
    expect(labels(full, [...ev, "any", 3])).not.toContain("Duplicate situation");
  });

  test("required properties can not be removed, optional ones can", () => {
    const doc = ex("trend_phases.json");
    expect(labels(doc, ["stages", "open"])).not.toContain("Remove open stage");
    expect(labels(doc, ["stages", "close"])).toContain("Remove close stage");
    expect(labels(doc, ["stages", "manage", "stop", "phases", 0, "level"])).toEqual([]);
    expect(labels(doc, ["stages", "manage", "stop", "phases", 0, "next"])).toContain("Remove next-phase event");
    expect(labels(doc, ["stages", "manage"])).toEqual(["Add manage rule", "Remove manage stage"]);
    expect(labels(doc, [])).toEqual(["Add manage stage"].filter(() => false));
  });

  test("a phase offers goto moves; a new goto aims at the first phase", () => {
    const doc = ex("trend_phases.json");
    const p = ["stages", "manage", "stop", "phases", 1];
    const add = actionsFor(doc, p).find((a) => a.type === "add-item" && a.key === "goto") as Action;
    expect(add.label).toBe("Add move-to-phase event");
    const r = applyAction(doc, add);
    expect(getAt(r.doc, [...p, "goto", 0, "to"])).toBe("initial");
    expect(Object.keys(getAt(r.doc, [...p, "goto", 0]) as object)).toEqual(["when", "to"]);
    expect(pathToId(r.select)).toBe("$.stages.manage.stop.phases[1].goto[0]");
  });

  test("labels have fallbacks", () => {
    expect(propLabel("zzz")).toBe("zzz");
    expect(itemLabel("zzz")).toBe("zzz");
    expect(itemLabel("orders")).toBe("order");
  });

  test("a path outside the document has no actions", () => {
    expect(actionsFor(NEW_STRATEGY, ["stages", "close"])).toEqual([]);
    expect(actionsFor(NEW_STRATEGY, ["stages", "close", "nope"])).toEqual([]);
  });
});

describe("applying edits", () => {
  test("adding keeps the engine's key order", () => {
    let doc = ex("ema_crossover.json");
    const a = (path: Path, label: string) => {
      const act = actionsFor(doc, path).find((x) => x.label === label) as Action;
      expect(act, label).toBeDefined();
      doc = applyAction(doc, act).doc;
    };
    a([], "Add manage stage");
    expect(Object.keys(getAt(doc, ["stages"]) as object)).toEqual(["trigger", "open", "manage", "close"]);
    a(["stages", "manage"], "Add stop (phases)");
    a(["stages", "manage"], "Add target (phases)");
    a(["stages", "manage"], "Add manage rule");
    expect(Object.keys(getAt(doc, ["stages", "manage"]) as object)).toEqual(["rules", "stop", "target"]);
    a(["stages", "open", "orders", 0], "Add cancel event");
    a(["stages", "open", "orders", 0], "Add rate formula");
    a(["stages", "open", "orders", 0], "Add create event");
    expect(Object.keys(getAt(doc, ["stages", "open", "orders", 0]) as object)).toEqual(["id", "type", "size_fraction", "rate", "create", "cancel", "stop_loss_pct"]);
    expect(structureIssues(doc)).toEqual([]);
    expect(getAt(doc, ["stages", "manage", "rules", 0, "id"])).toBe("rule_1");
    expect(getAt(doc, ["stages", "manage", "stop", "phases", 0, "id"])).toBe("phase_1");
    expect(getAt(doc, ["stages", "manage", "target", "phases", 0, "id"])).toBe("phase_2");
  });

  test("duplicate renames ids and every nested id", () => {
    const doc = ex("trend_phases.json");
    const act = actionsFor(doc, ["stages", "manage", "stop", "phases", 0]).find((a) => a.type === "duplicate") as Action;
    const r = applyAction(doc, act);
    expect(getAt(r.doc, ["stages", "manage", "stop", "phases", 1, "id"])).toBe("initial_2");
    expect(r.view).toEqual({ kind: "insert", array: ["stages", "manage", "stop", "phases"], index: 1 });
    const ids = [...collectIds(r.doc)];
    expect(new Set(ids).size).toBe(ids.length);
  });

  test("remove an optional property drops it and moves the selection up", () => {
    const doc = ex("trend_phases.json");
    const act = actionsFor(doc, ["stages", "close"]).find((a) => a.type === "remove") as Action;
    const r = applyAction(doc, act);
    expect(getAt(r.doc, ["stages", "close"])).toBeUndefined();
    expect(r.select).toEqual(["stages"]);
    expect(r.view).toEqual({ kind: "drop", prefix: ["stages", "close"] });
  });

  test("setField inserts in canonical order and clearing removes the key", () => {
    const doc = ex("ema_crossover.json");
    const p = ["stages", "open", "orders", 0];
    const a = setField(doc, p, "take_profit_pct", 12);
    expect(Object.keys(getAt(a, p) as object)).toEqual(["id", "type", "size_fraction", "take_profit_pct", "stop_loss_pct"]);
    const b = setField(a, p, "size_fraction", 0.5);
    expect(getAt(b, [...p, "size_fraction"])).toBe(0.5);
    const c = setField(b, p, "stop_loss_pct", undefined);
    expect(isObject(getAt(c, p)) && "stop_loss_pct" in (getAt(c, p) as object)).toBe(false);
    expect(() => setField(doc, ["stages", "nope"], "x", 1)).toThrow(/nothing at/);
  });

  test("invalid actions explain themselves", () => {
    const doc = ex("ema_crossover.json");
    expect(() => applyAction(doc, { type: "add-prop", label: "", owner: ["stages", "zzz"], key: "rate" })).toThrow(/cannot add rate/);
    expect(() => applyAction(doc, { type: "add-item", label: "", owner: ["stages", "zzz"], key: "orders" })).toThrow(/cannot add to orders/);
    expect(() => applyAction(doc, { type: "remove", label: "", path: ["stages", "zzz"] })).toThrow(/cannot remove/);
    expect(() => applyAction(doc, { type: "move", label: "", path: ["name"], delta: 1 })).toThrow(/not a list item/);
    expect(() => applyAction(doc, { type: "duplicate", label: "", path: ["name"] })).toThrow(/not a list item/);
  });
});

describe("any sequence of editor actions stays inside the schema and never writes layout (SEP-5.10)", () => {
  const arbSteps = fc.array(fc.tuple(fc.nat(500), fc.nat(500)), { minLength: 1, maxLength: 25 });
  test("property", () => {
    fc.assert(
      fc.property(fc.nat(EXAMPLES.length - 1), arbSteps, (exIdx, steps) => {
        let doc = (EXAMPLES[exIdx] as { doc: Json }).doc;
        for (const [ni, ai] of steps) {
          const nodes = buildGraph(doc).nodes;
          const node = nodes[ni % nodes.length];
          if (!node) continue;
          const acts = actionsFor(doc, node.path);
          const act = acts[ai % Math.max(acts.length, 1)];
          if (!act) continue;
          doc = applyAction(doc, act).doc;
          if (structureIssues(doc).length) return false;
        }
        const text = serialize(doc);
        const ids = [...collectIds(doc)];
        return (
          structureIssues(doc).length === 0 &&
          !/"(position|positions|x|y|view|layout)"/.test(text) &&
          JSON.stringify(JSON.parse(text)) === JSON.stringify(doc) &&
          ids.length >= 1
        );
      }),
      { numRuns: 150 },
    );
  });
});
