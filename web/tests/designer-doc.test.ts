import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import fc from "fast-check";
import {
  type Json,
  collectIds,
  getAt,
  insertIndex,
  insertKey,
  moveIndex,
  parseDoc,
  parsePath,
  pathToId,
  removeIndex,
  serialize,
  setAt,
} from "../src/lib/designer/doc";
import { EXAMPLES, NEW_STRATEGY } from "../src/lib/designer/examples";
import { fileNameFor } from "../src/lib/designer/download";

const DIR = join(import.meta.dir, "../../packages/sa-engine/examples");
const files = readdirSync(DIR).filter((f) => f.endsWith(".json")).sort();

describe("round trip (SEP-5.6)", () => {
  test("the bundled list is every example the engine ships", () => {
    expect(EXAMPLES.map((e) => e.file).sort()).toEqual(files);
  });

  for (const f of files) {
    test(`${f}: open then save is byte-identical`, () => {
      const text = readFileSync(join(DIR, f), "utf8");
      const parsed = parseDoc(text, f);
      expect(parsed.ok).toBe(true);
      if (parsed.ok) expect(serialize(parsed.doc)).toBe(text);
      const bundled = EXAMPLES.find((e) => e.file === f);
      expect(serialize(bundled?.doc as Json)).toBe(text);
    });
  }

  test("a no-op edit through setAt keeps the bytes", () => {
    for (const f of files) {
      const text = readFileSync(join(DIR, f), "utf8");
      const doc = JSON.parse(text) as Json;
      expect(serialize(setAt(doc, ["name"], getAt(doc, ["name"]) as Json))).toBe(text);
    }
  });
});

describe("paths", () => {
  test("pathToId and parsePath are inverse", () => {
    fc.assert(
      fc.property(
        fc.array(fc.oneof(fc.nat(30), fc.constantFrom("stages", "open", "orders", "a_b", "x1")), { maxLength: 8 }),
        (p) => {
          expect(parsePath(pathToId(p))).toEqual(p);
        },
      ),
    );
    expect(pathToId(["stages", "open", "orders", 0, "id"])).toBe("$.stages.open.orders[0].id");
    expect(parsePath(pathToId(["a b", 1]))).toEqual(["a b", 1]);
  });

  test("bad paths give null", () => {
    for (const bad of ["", "stages", "$.", "$[", "$[x]", "$.1a", "$[\"x", "$[\"\\q\"]"]) {
      expect(parsePath(bad)).toBeNull();
    }
  });

  test("getAt / setAt are immutable and share the rest", () => {
    const doc = { a: { b: [1, { c: 2 }] }, z: { k: 1 } } as Json;
    const next = setAt(doc, ["a", "b", 1, "c"], 3);
    expect(getAt(next, ["a", "b", 1, "c"])).toBe(3);
    expect(getAt(doc, ["a", "b", 1, "c"])).toBe(2);
    expect((next as { z: unknown }).z).toBe((doc as { z: unknown }).z);
    expect(getAt(doc, ["a", "nope"])).toBeUndefined();
    expect(getAt(doc, ["a", "b", 7])).toBeUndefined();
    expect(getAt(doc, ["a", 0])).toBeUndefined();
    expect(setAt(doc, [], 5)).toBe(5);
    expect(() => setAt(doc, ["a", "b", "x", 1], 1)).toThrow(/expected an object/);
    expect(() => setAt(doc, ["a", 0], 1)).toThrow(/expected an array/);
  });

  test("insertKey keeps canonical order and leaves unknown keys last", () => {
    const o = insertKey({ id: "a", rearm: true, extra: 1 }, "size_fraction", 0.1, ["id", "size_fraction", "rearm"]);
    expect(Object.keys(o)).toEqual(["id", "size_fraction", "rearm", "extra"]);
    expect(Object.keys(insertKey({ a: 1 }, "b", 2, []))).toEqual(["a", "b"]);
  });

  test("list helpers", () => {
    expect(insertIndex([1, 2], 9, 3)).toEqual([1, 2, 3]);
    expect(removeIndex([1, 2, 3], 1)).toEqual([1, 3]);
    expect(moveIndex([1, 2, 3], 0, 2)).toEqual([2, 3, 1]);
    expect(moveIndex([1, 2, 3], 0, 5)).toEqual([1, 2, 3]);
  });
});

describe("files", () => {
  test("parseDoc says what is wrong", () => {
    const bad = parseDoc("{nope", "x.json");
    expect(!bad.ok && bad.error).toMatch(/x\.json is not valid JSON/);
    const arr = parseDoc("[1]", "y.json");
    expect(!arr.ok && arr.error).toMatch(/an array/);
    expect(parseDoc("3").ok).toBe(false);
  });

  test("ids are collected from the whole document", () => {
    const ex = EXAMPLES.find((e) => e.file === "trend_phases.json")?.doc as Json;
    const ids = collectIds(ex);
    expect([...ids].sort()).toEqual(["break_even", "entry", "extended", "first", "initial", "trail"]);
  });

  test("the file name flags an invalid document", () => {
    expect(fileNameFor("ema-crossover", true)).toBe("ema-crossover.json");
    expect(fileNameFor("a/b c", false)).toBe("a_b_c.invalid.json");
    expect(fileNameFor("///", true)).toBe("strategy.json");
  });

  test("the blank strategy is a stable, canonical document", () => {
    expect(JSON.parse(serialize(NEW_STRATEGY))).toEqual(NEW_STRATEGY);
  });
});
