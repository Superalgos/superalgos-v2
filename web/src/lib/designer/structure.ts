/** A local structural check against the JSON Schema (keys, required fields, list sizes, enums). Used by the mock API and by tests; the real API's validator is the authority. */
import { isObject, type Json, type Path, pathToId } from "./doc";
import type { Issue } from "./errors";
import { itemsOf, kindOf, propsOf, type Resolved, resolve, rootSchema } from "./schema";

export function structureIssues(doc: Json): string[] {
  return structureProblems(doc).map((p) => `${p.path}: ${p.message}`);
}

export function structureProblems(doc: Json): Issue[] {
  const out: Issue[] = [];
  const bad = (path: Path, message: string) =>
    out.push({ path: pathToId(path), column: null, message });
  const walk = (v: Json, r: Resolved, path: Path) => {
    const kind = kindOf(r);
    if (kind === "object") {
      if (!isObject(v)) return bad(path, "expected an object");
      const props = propsOf(r);
      const known = new Set(props.map((p) => p.key));
      for (const k of Object.keys(v)) if (!known.has(k)) bad([...path, k], `unknown key ${k}`);
      for (const p of props) {
        const child = v[p.key];
        if (child === undefined || child === null) {
          if (p.required) bad([...path, p.key], `missing required field ${p.key}`);
          continue;
        }
        walk(child, p.resolved, [...path, p.key]);
      }
    } else if (kind === "array") {
      if (!Array.isArray(v)) return bad(path, "expected a list");
      const { minItems = 0, maxItems = Number.POSITIVE_INFINITY } = r.schema;
      if (v.length < minItems || v.length > maxItems)
        bad(path, `${v.length} items, allowed ${minItems}..${maxItems}`);
      const item = itemsOf(r);
      if (item) v.forEach((x, i) => walk(x, item, [...path, i]));
    } else if (kind === "dsl") {
      if (typeof v !== "string" || v.length === 0) bad(path, "expected a non-empty expression");
    } else if (r.schema.enum && !r.schema.enum.includes(v)) {
      bad(path, `${JSON.stringify(v)} is not one of ${r.schema.enum.join(", ")}`);
    }
  };
  walk(doc, resolve(rootSchema), []);
  return out;
}
