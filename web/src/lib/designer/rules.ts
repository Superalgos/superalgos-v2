/** What the editor may add, remove or reorder at a node, read from the JSON Schema, and how each edit changes the document. */
import {
  collectIds,
  getAt,
  insertIndex,
  insertKey,
  isObject,
  type Json,
  type JsonObject,
  moveIndex,
  type Path,
  removeIndex,
  removeKey,
  setAt,
} from "./doc";
import {
  defaultFor,
  defName,
  itemsOf,
  keyOrder,
  kindOf,
  propsOf,
  type Resolved,
  schemaAt,
  singular,
  TRANSPARENT_DEFS,
} from "./schema";

export type Action =
  | { type: "add-prop"; label: string; owner: Path; key: string }
  | { type: "add-item"; label: string; owner: Path; key: string }
  | { type: "remove"; label: string; path: Path }
  | { type: "move"; label: string; path: Path; delta: -1 | 1 }
  | { type: "duplicate"; label: string; path: Path };

/** How an edit shifts the node positions saved in the view file (see view.ts). */
export type ViewOp =
  | { kind: "drop"; prefix: Path }
  | { kind: "insert"; array: Path; index: number }
  | { kind: "remove"; array: Path; index: number }
  | { kind: "move"; array: Path; from: number; to: number };

const PROP_LABEL: Record<string, string> = {
  trigger: "trigger",
  on: "trigger-on event",
  off: "trigger-off event",
  open: "open stage",
  when: "event",
  manage: "manage stage",
  close: "close stage",
  create: "create event",
  cancel: "cancel event",
  next: "next-phase event",
  stop: "stop (phases)",
  target: "target (phases)",
  rate: "rate formula",
  size: "size formula",
  level: "level formula",
};

const ITEM_LABEL: Record<string, string> = {
  orders: "order",
  rules: "manage rule",
  phases: "phase",
  goto: "move-to-phase event",
  any: "situation",
  all: "condition",
};

export function propLabel(key: string): string {
  return PROP_LABEL[key] ?? key;
}

export function itemLabel(key: string): string {
  return ITEM_LABEL[key] ?? singular(key);
}

/** The objects whose properties a node can grow: itself, plus the transparent `stages` wrapper under the root. */
function owners(doc: Json, path: Path): { path: Path; r: Resolved }[] {
  const r = schemaAt(path);
  if (!r || !r.schema.properties) return [];
  const out = [{ path, r }];
  for (const p of propsOf(r)) {
    if (p.kind === "object" && p.resolved.def && TRANSPARENT_DEFS.has(p.resolved.def)) {
      if (isObject(getAt(doc, [...path, p.key])))
        out.push({ path: [...path, p.key], r: p.resolved });
    }
  }
  return out;
}

export function actionsFor(doc: Json, path: Path): Action[] {
  const out: Action[] = [];
  const value = getAt(doc, path);
  if (value === undefined) return out;
  for (const o of owners(doc, path)) {
    const ov = getAt(doc, o.path);
    if (!isObject(ov)) continue;
    for (const p of propsOf(o.r)) {
      if (p.resolved.def && TRANSPARENT_DEFS.has(p.resolved.def)) continue;
      if (p.kind === "object" || p.kind === "dsl") {
        if (ov[p.key] === undefined || ov[p.key] === null) {
          out.push({
            type: "add-prop",
            label: `Add ${propLabel(p.key)}`,
            owner: o.path,
            key: p.key,
          });
        }
      } else if (p.kind === "array") {
        const item = itemsOf(p.resolved);
        const itemKind = item ? kindOf(item) : "scalar";
        if (itemKind !== "object" && itemKind !== "dsl") continue;
        const cur = ov[p.key];
        const len = Array.isArray(cur) ? cur.length : 0;
        if (len < (p.resolved.schema.maxItems ?? 1000)) {
          out.push({
            type: "add-item",
            label: `Add ${itemLabel(p.key)}`,
            owner: o.path,
            key: p.key,
          });
        }
      }
    }
  }
  // remove / reorder / duplicate: about the node itself
  const last = path[path.length - 1];
  const parentPath = path.slice(0, -1);
  const parent = getAt(doc, parentPath);
  if (last === undefined) return out;
  if (typeof last === "number" && Array.isArray(parent)) {
    const arrKey = parentPath[parentPath.length - 1];
    const ar = schemaAt(parentPath);
    const min = ar?.schema.minItems ?? 0;
    const max = ar?.schema.maxItems ?? 1000;
    const what = itemLabel(String(arrKey));
    if (parent.length > min) out.push({ type: "remove", label: `Remove ${what}`, path });
    if (last > 0) out.push({ type: "move", label: `Move ${what} up`, path, delta: -1 });
    if (last < parent.length - 1)
      out.push({ type: "move", label: `Move ${what} down`, path, delta: 1 });
    if (parent.length < max && typeof value !== "undefined") {
      out.push({ type: "duplicate", label: `Duplicate ${what}`, path });
    }
  } else if (typeof last === "string" && isObject(parent)) {
    const pr = schemaAt(parentPath);
    const required = pr?.schema.required?.includes(last) ?? true;
    if (!required) out.push({ type: "remove", label: `Remove ${propLabel(last)}`, path });
  }
  return out;
}

function nextId(base: string, used: Set<string>): string {
  const stem = base.replace(/[^a-z0-9_]/g, "_").replace(/^[^a-z]+/, "") || "item";
  for (let n = 1; ; n++) {
    const id = `${stem.slice(0, 28)}_${n}`;
    if (!used.has(id)) return id;
  }
}

/** Fills every empty `id` in a freshly built value with a unique one; ids are unique across the whole document. */
function assignIds(v: Json, base: string, used: Set<string>): Json {
  if (Array.isArray(v)) return v.map((x) => assignIds(x, base, used));
  if (!isObject(v)) return v;
  const out: JsonObject = {};
  for (const [k, x] of Object.entries(v)) {
    if (k === "id" && x === "") {
      const id = nextId(base, used);
      used.add(id);
      out[k] = id;
    } else out[k] = assignIds(x, k === "phases" ? "phase" : base, used);
  }
  return out;
}

/** `goto.to` must name a phase of the same list: aim the new one at the first phase. */
function aimGotos(v: Json, firstPhase: string): Json {
  if (Array.isArray(v)) return v.map((x) => aimGotos(x, firstPhase));
  if (!isObject(v)) return v;
  const out: JsonObject = {};
  for (const [k, x] of Object.entries(v))
    out[k] = k === "to" && x === "" ? firstPhase : aimGotos(x, firstPhase);
  return out;
}

export interface Applied {
  doc: Json;
  /** Path of the node to select after the edit. */
  select: Path;
  view?: ViewOp;
}

/** Applies an action from `actionsFor`; throws with the reason when the document no longer fits the action. */
export function applyAction(doc: Json, action: Action): Applied {
  switch (action.type) {
    case "add-prop": {
      const owner = getAt(doc, action.owner);
      const r = schemaAt(action.owner);
      const spec = r ? propsOf(r).find((p) => p.key === action.key) : undefined;
      if (!isObject(owner) || !spec)
        throw new Error(
          `cannot add ${action.key}: no object with that property at the path ${action.owner.join(".")}; reselect the node`,
        );
      const used = collectIds(doc);
      const fresh = assignIds(defaultFor(spec.resolved), singular(action.key), used);
      const next = insertKey(owner, action.key, fresh, keyOrder(defName(r as Resolved)));
      return { doc: setAt(doc, action.owner, next), select: [...action.owner, action.key] };
    }
    case "add-item": {
      const owner = getAt(doc, action.owner);
      const r = schemaAt(action.owner);
      const spec = r ? propsOf(r).find((p) => p.key === action.key) : undefined;
      const item = spec ? itemsOf(spec.resolved) : null;
      if (!isObject(owner) || !spec || !item)
        throw new Error(
          `cannot add to ${action.key}: no list with that name at ${action.owner.join(".")}; reselect the node`,
        );
      const cur = owner[action.key];
      const list = Array.isArray(cur) ? cur : [];
      let fresh = assignIds(defaultFor(item), singular(action.key), collectIds(doc));
      if (action.key === "goto") {
        const phases = getAt(doc, action.owner.slice(0, -1));
        const first = Array.isArray(phases) && isObject(phases[0]) ? phases[0].id : undefined;
        fresh = aimGotos(fresh, typeof first === "string" ? first : "phase_1");
      }
      const arr = insertIndex(list, list.length, fresh);
      const next = Array.isArray(cur)
        ? { ...owner, [action.key]: arr }
        : insertKey(owner, action.key, arr, keyOrder(defName(r as Resolved)));
      return {
        doc: setAt(doc, action.owner, next),
        select: [...action.owner, action.key, arr.length - 1],
        view: { kind: "insert", array: [...action.owner, action.key], index: arr.length - 1 },
      };
    }
    case "remove": {
      const last = action.path[action.path.length - 1];
      const parentPath = action.path.slice(0, -1);
      const parent = getAt(doc, parentPath);
      if (typeof last === "number" && Array.isArray(parent)) {
        return {
          doc: setAt(doc, parentPath, removeIndex(parent, last)),
          select: parentPath.slice(0, -1),
          view: { kind: "remove", array: parentPath, index: last },
        };
      }
      if (typeof last === "string" && isObject(parent) && last in parent) {
        return {
          doc: setAt(doc, parentPath, removeKey(parent, last)),
          select: parentPath,
          view: { kind: "drop", prefix: action.path },
        };
      }
      throw new Error(
        `cannot remove ${action.path.join(".")}: not found in the document; reselect the node`,
      );
    }
    case "move": {
      const last = action.path[action.path.length - 1];
      const parentPath = action.path.slice(0, -1);
      const parent = getAt(doc, parentPath);
      if (typeof last !== "number" || !Array.isArray(parent))
        throw new Error(`cannot move ${action.path.join(".")}: it is not a list item`);
      const to = last + action.delta;
      return {
        doc: setAt(doc, parentPath, moveIndex(parent, last, to)),
        select: [...parentPath, to],
        view: { kind: "move", array: parentPath, from: last, to },
      };
    }
    case "duplicate": {
      const last = action.path[action.path.length - 1];
      const parentPath = action.path.slice(0, -1);
      const parent = getAt(doc, parentPath);
      const value = getAt(doc, action.path);
      if (typeof last !== "number" || !Array.isArray(parent) || value === undefined)
        throw new Error(`cannot duplicate ${action.path.join(".")}: it is not a list item`);
      const used = collectIds(doc);
      const copy = renameIds(structuredClone(value), used);
      return {
        doc: setAt(doc, parentPath, insertIndex(parent, last + 1, copy)),
        select: [...parentPath, last + 1],
        view: { kind: "insert", array: parentPath, index: last + 1 },
      };
    }
  }
}

/** Gives every id inside a copied value a fresh one (`entry` -> `entry_2`). */
function renameIds(v: Json, used: Set<string>): Json {
  if (Array.isArray(v)) return v.map((x) => renameIds(x, used));
  if (!isObject(v)) return v;
  const out: JsonObject = {};
  for (const [k, x] of Object.entries(v)) {
    if (k === "id" && typeof x === "string") {
      const stem = x.replace(/_\d+$/, "").slice(0, 28);
      let n = 2;
      while (used.has(`${stem}_${n}`)) n++;
      const id = `${stem}_${n}`;
      used.add(id);
      out[k] = id;
    } else out[k] = renameIds(x, used);
  }
  return out;
}

/** Sets or clears a scalar property of the object at `path` (clearing removes the key: the document stays minimal). */
export function setField(doc: Json, path: Path, key: string, value: Json | undefined): Json {
  const obj = getAt(doc, path);
  const r = schemaAt(path);
  if (!isObject(obj))
    throw new Error(`cannot edit ${key}: nothing at ${path.join(".")}; reselect the node`);
  if (value === undefined) return setAt(doc, path, removeKey(obj, key));
  if (Object.hasOwn(obj, key)) return setAt(doc, [...path, key], value);
  return setAt(doc, path, insertKey(obj, key, value, keyOrder(r ? defName(r) : "")));
}
