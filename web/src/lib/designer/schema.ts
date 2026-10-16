/** Reading the strategy JSON Schema (`packages/sa-engine/schema/strategy.schema.json`): the one source of what the editor may offer. */
import schemaJson from "../../../../packages/sa-engine/schema/strategy.schema.json";
import type { Json, JsonObject } from "./doc";

export interface SchemaNode {
  $ref?: string;
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, SchemaNode>;
  required?: string[];
  items?: SchemaNode;
  anyOf?: SchemaNode[];
  enum?: Json[];
  const?: Json;
  default?: Json;
  minItems?: number;
  maxItems?: number;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  "x-dsl"?: "boolean" | "number";
  $defs?: Record<string, SchemaNode>;
}

export const rootSchema = schemaJson as unknown as SchemaNode;

/** A schema node with `$ref` followed and `anyOf [X, null]` unwrapped. */
export interface Resolved {
  schema: SchemaNode;
  /** Name of the `$defs` entry it came from, if any (the document root has none). */
  def?: string;
  /** The schema allows null (the property is optional and may be absent). */
  nullable: boolean;
  description?: string;
}

export function resolve(node: SchemaNode, root: SchemaNode = rootSchema): Resolved {
  let nullable = false;
  let cur = node;
  let description = node.description;
  if (cur.anyOf) {
    const rest = cur.anyOf.filter((n) => n.type !== "null");
    nullable = rest.length !== cur.anyOf.length;
    const first = rest[0];
    if (!first)
      throw new Error(`schema node anyOf has only null: ${JSON.stringify(node).slice(0, 80)}`);
    cur = first;
    description = description ?? cur.description;
  }
  let def: string | undefined;
  if (cur.$ref) {
    const m = /^#\/\$defs\/(.+)$/.exec(cur.$ref);
    const name = m?.[1];
    const target = name ? root.$defs?.[name] : undefined;
    if (!name || !target)
      throw new Error(`schema $ref ${cur.$ref} does not point into $defs; regenerate the schema`);
    def = name;
    description = description ?? cur.description ?? target.description;
    cur = target;
  }
  return { schema: cur, def, nullable, description };
}

export type Kind = "object" | "array" | "dsl" | "scalar";

export function kindOf(r: Resolved): Kind {
  if (r.schema["x-dsl"]) return "dsl";
  if (r.schema.properties) return "object";
  if (r.schema.type === "array") return "array";
  return "scalar";
}

export interface PropSpec {
  key: string;
  resolved: Resolved;
  kind: Kind;
  required: boolean;
}

/** Defs that are not drawn as their own node: their properties hang from the parent node (the `stages` wrapper). */
export const TRANSPARENT_DEFS: ReadonlySet<string> = new Set(["Stages"]);

/**
 * Declaration order of each model's fields, which is the order the engine writes. The exported JSON Schema sorts its
 * properties alphabetically, so this one fact is not derivable from it; a test checks each list holds exactly the
 * schema's properties, so a new field makes that test fail until it is placed here.
 */
export const KEY_ORDER: Readonly<Record<string, readonly string[]>> = {
  Strategy: ["schema_version", "name", "description", "timeframe", "stages"],
  Stages: ["trigger", "open", "manage", "close"],
  Trigger: ["on", "off"],
  OpenStage: ["when", "orders"],
  OrderSpec: [
    "id",
    "side",
    "type",
    "size_fraction",
    "size",
    "offset_pct",
    "rate",
    "create",
    "cancel",
    "rearm",
    "take_profit_pct",
    "stop_loss_pct",
  ],
  Manage: ["rules", "stop", "target"],
  ManageRule: ["id", "when", "action", "size_fraction", "once"],
  PhasedLevel: ["phases"],
  Phase: ["id", "level", "next", "goto"],
  Goto: ["when", "to"],
  CloseStage: ["when"],
  Event: ["any"],
  Situation: ["name", "all"],
};

export function defName(r: Resolved): string {
  return r.def ?? "Strategy";
}

export function keyOrder(def: string): readonly string[] {
  return KEY_ORDER[def] ?? [];
}

/** Properties of an object schema in canonical order. */
export function propsOf(r: Resolved, root: SchemaNode = rootSchema): PropSpec[] {
  const props = r.schema.properties ?? {};
  const required = new Set(r.schema.required ?? []);
  const order = keyOrder(defName(r));
  const keys = Object.keys(props).sort((a, b) => {
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
  });
  return keys.map((key) => {
    const resolved = resolve(props[key] as SchemaNode, root);
    return { key, resolved, kind: kindOf(resolved), required: required.has(key) };
  });
}

export function itemsOf(r: Resolved, root: SchemaNode = rootSchema): Resolved | null {
  return r.schema.items ? resolve(r.schema.items, root) : null;
}

/** A plain-English name for the thing an array holds, from the array's key ("orders" -> "order"). */
export function singular(key: string): string {
  if (key === "any") return "situation";
  if (key === "all") return "condition";
  if (key === "goto") return "goto";
  return key.endsWith("s") ? key.slice(0, -1) : key;
}

/** Seed values for the few cases where the schema's minimum valid object is not a useful starting point. */
const SEEDS: Readonly<Record<string, () => Json>> = {
  Situation: () => ({ name: "new situation", all: ["true"] }),
  Trigger: () => ({ on: { any: [{ name: "always", all: ["true"] }] } }),
  OrderSpec: () => ({ type: "market", size_fraction: 0.1 }),
  Phase: () => ({ level: "close" }),
};

const DSL_SEED = { boolean: "true", number: "close" } as const;

/** A starting value for something the schema describes: required fields only, plus the seeds above. */
export function defaultFor(r: Resolved, root: SchemaNode = rootSchema): Json {
  const s = r.schema;
  const dsl = s["x-dsl"];
  if (dsl) return DSL_SEED[dsl];
  if (s.const !== undefined) return s.const;
  if (s.default !== undefined && !s.properties) return s.default;
  if (s.enum?.[0] !== undefined) return s.enum[0];
  if (s.properties) {
    const out: JsonObject = {};
    const seed = r.def ? SEEDS[r.def]?.() : undefined;
    const seeded = seed && typeof seed === "object" && !Array.isArray(seed) ? seed : {};
    for (const p of propsOf(r, root)) {
      if (Object.hasOwn(seeded, p.key)) out[p.key] = seeded[p.key] as Json;
      else if (p.required) out[p.key] = defaultFor(p.resolved, root);
    }
    // keep the seed's keys in canonical order
    const order = keyOrder(defName(r));
    const sorted: JsonObject = {};
    for (const k of Object.keys(out).sort((a, b) => order.indexOf(a) - order.indexOf(b)))
      sorted[k] = out[k] as Json;
    return sorted;
  }
  if (s.type === "array") {
    const item = itemsOf(r, root);
    const n = s.minItems ?? 0;
    return item ? Array.from({ length: n }, () => defaultFor(item, root)) : [];
  }
  if (s.type === "string") return "";
  if (s.type === "boolean") return false;
  if (s.type === "number" || s.type === "integer") {
    if (s.minimum !== undefined) return s.minimum;
    if (s.exclusiveMinimum !== undefined) return s.exclusiveMinimum + 1;
    return 0;
  }
  return null;
}

/** The schema node a document path points at, or null when the path leaves the schema. */
export function schemaAt(
  path: readonly (string | number)[],
  root: SchemaNode = rootSchema,
): Resolved | null {
  let cur = resolve(root, root);
  for (const seg of path) {
    if (typeof seg === "number") {
      const item = itemsOf(cur, root);
      if (!item) return null;
      cur = item;
    } else {
      const child = cur.schema.properties?.[seg];
      if (!child) return null;
      cur = resolve(child, root);
    }
  }
  return cur;
}
