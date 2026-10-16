/** The strategy document as plain JSON plus the path helpers every edit goes through. Pure; no React. */

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type Path = (string | number)[];

export function isObject(v: unknown): v is JsonObject {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** `$.stages.open.orders[0]`: the path spelling the validate endpoint uses, also the node id. */
export function pathToId(path: Path): string {
  let s = "$";
  for (const seg of path)
    s +=
      typeof seg === "number"
        ? `[${seg}]`
        : IDENT.test(seg)
          ? `.${seg}`
          : `[${JSON.stringify(seg)}]`;
  return s;
}

/** Inverse of `pathToId`; null when `s` is not a path of that shape. */
export function parsePath(s: string): Path | null {
  if (!s.startsWith("$")) return null;
  const out: Path = [];
  let i = 1;
  while (i < s.length) {
    const c = s[i];
    if (c === ".") {
      const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(s.slice(i + 1));
      if (!m) return null;
      out.push(m[0]);
      i += 1 + m[0].length;
    } else if (c === "[") {
      const end = s.indexOf("]", i);
      if (end < 0) return null;
      const inner = s.slice(i + 1, end);
      if (/^\d+$/.test(inner)) out.push(Number(inner));
      else if (inner.startsWith('"') && inner.endsWith('"') && inner.length >= 2) {
        try {
          out.push(JSON.parse(inner) as string);
        } catch {
          return null;
        }
      } else return null;
      i = end + 1;
    } else return null;
  }
  return out;
}

export function getAt(doc: Json, path: Path): Json | undefined {
  let cur: Json | undefined = doc;
  for (const seg of path) {
    if (typeof seg === "number") {
      if (!Array.isArray(cur)) return undefined;
      cur = cur[seg];
    } else {
      if (!isObject(cur)) return undefined;
      cur = Object.hasOwn(cur, seg) ? cur[seg] : undefined;
    }
    if (cur === undefined) return undefined;
  }
  return cur;
}

/** Copy of `doc` with the value at `path` replaced; the parents on the way are copied, the rest is shared. */
export function setAt(doc: Json, path: Path, value: Json): Json {
  if (path.length === 0) return value;
  const [head, ...rest] = path as [string | number, ...Path];
  if (typeof head === "number") {
    if (!Array.isArray(doc))
      throw new Error(
        `setAt: expected an array at [${head}] but found ${typeof doc}; path ${pathToId(path)}`,
      );
    const copy = doc.slice();
    copy[head] = setAt(copy[head] ?? null, rest, value);
    return copy;
  }
  if (!isObject(doc))
    throw new Error(
      `setAt: expected an object at .${head} but found ${typeof doc}; path ${pathToId(path)}`,
    );
  return { ...doc, [head]: setAt(doc[head] ?? null, rest, value) };
}

/** `Object.keys` order is the document's: `order` lists the canonical order of the known keys. */
export function insertKey(
  obj: JsonObject,
  key: string,
  value: Json,
  order: readonly string[],
): JsonObject {
  const rank = (k: string) => {
    const i = order.indexOf(k);
    return i < 0 ? Number.POSITIVE_INFINITY : i;
  };
  const mine = rank(key);
  const out: JsonObject = {};
  let placed = false;
  for (const [k, v] of Object.entries(obj)) {
    if (k === key) continue;
    if (!placed && rank(k) > mine) {
      out[key] = value;
      placed = true;
    }
    out[k] = v;
  }
  if (!placed) out[key] = value;
  return out;
}

export function removeKey(obj: JsonObject, key: string): JsonObject {
  const out: JsonObject = {};
  for (const [k, v] of Object.entries(obj)) if (k !== key) out[k] = v;
  return out;
}

export function insertIndex<T>(list: readonly T[], index: number, value: T): T[] {
  const i = Math.max(0, Math.min(index, list.length));
  return [...list.slice(0, i), value, ...list.slice(i)];
}

export function removeIndex<T>(list: readonly T[], index: number): T[] {
  return list.filter((_, i) => i !== index);
}

export function moveIndex<T>(list: readonly T[], from: number, to: number): T[] {
  if (from < 0 || from >= list.length || to < 0 || to >= list.length) return list.slice();
  const out = list.slice();
  const [item] = out.splice(from, 1);
  out.splice(to, 0, item as T);
  return out;
}

export function clone<T extends Json>(v: T): T {
  return structuredClone(v);
}

/** The engine's canonical form (`json.dumps(indent=2, ensure_ascii=False) + "\n"`): what the designer writes. */
export function serialize(doc: Json): string {
  return `${JSON.stringify(doc, null, 2)}\n`;
}

export type ParseResult = { ok: true; doc: JsonObject } | { ok: false; error: string };

export function parseDoc(text: string, source = "the file"): ParseResult {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch (e) {
    return {
      ok: false,
      error: `${source} is not valid JSON (${e instanceof Error ? e.message : String(e)}). Fix the JSON or open another file.`,
    };
  }
  if (!isObject(v)) {
    return {
      ok: false,
      error: `${source} holds ${Array.isArray(v) ? "an array" : typeof v}, a strategy document is a JSON object. Open a strategy file.`,
    };
  }
  return { ok: true, doc: v };
}

/** Every string stored under an `id` key anywhere in the document. */
export function collectIds(doc: Json, out = new Set<string>()): Set<string> {
  if (Array.isArray(doc)) for (const v of doc) collectIds(v, out);
  else if (isObject(doc)) {
    for (const [k, v] of Object.entries(doc)) {
      if (k === "id" && typeof v === "string") out.add(v);
      else collectIds(v, out);
    }
  }
  return out;
}
