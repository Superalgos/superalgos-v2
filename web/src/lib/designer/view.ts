/** Node positions live in a view file (or localStorage), keyed by strategy name, never inside the strategy document (SEP-5.10). */
import { type Path, parsePath, pathToId } from "./doc";
import type { Positions } from "./graph";
import { type RadialSetting, type RadialSettings, sanitizeRadial } from "./radial";
import type { ViewOp } from "./rules";

export interface ViewFile {
  view_version: 2;
  /** `name` of the strategy this layout belongs to. */
  strategy: string;
  /** Cards positions, keyed by node id (JSON path). */
  positions: Positions;
  /** Radial settings, keyed by stable node key (see radial.ts). Empty when parsed from a v1 file. */
  radial: RadialSettings;
}

export const VIEW_PREFIX = "sa-designer-view:";

export function viewKey(strategyName: string): string {
  return `${VIEW_PREFIX}${strategyName}`;
}

export function serializeView(v: {
  strategy: string;
  positions: Positions;
  radial?: RadialSettings;
}): string {
  const positions: Positions = {};
  for (const k of Object.keys(v.positions).sort()) {
    const p = v.positions[k] as { x: number; y: number };
    positions[k] = { x: Math.round(p.x), y: Math.round(p.y) };
  }
  const radial: RadialSettings = {};
  for (const k of Object.keys(v.radial ?? {}).sort()) {
    const s = (v.radial as RadialSettings)[k] as RadialSetting;
    const o: RadialSetting = {};
    if (s.spread !== undefined) o.spread = s.spread;
    if (s.distance !== undefined) o.distance = s.distance;
    if (s.pin)
      o.pin = {
        angle: Math.round(s.pin.angle * 10) / 10,
        distance: Math.round(s.pin.distance),
      };
    radial[k] = o;
  }
  return `${JSON.stringify({ view_version: 2, strategy: v.strategy, positions, radial }, null, 2)}\n`;
}

/** Reads a view file, version 1 (positions only) or 2; null when it is neither. */
export function parseView(text: string): ViewFile | null {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof v !== "object" || v === null) return null;
  const o = v as Record<string, unknown>;
  if ((o.view_version !== 1 && o.view_version !== 2) || typeof o.strategy !== "string") return null;
  if (typeof o.positions !== "object" || o.positions === null) return null;
  const positions: Positions = {};
  for (const [k, p] of Object.entries(o.positions)) {
    const q = p as { x?: unknown; y?: unknown };
    if (typeof q?.x === "number" && typeof q.y === "number" && Number.isFinite(q.x + q.y)) {
      positions[k] = { x: q.x, y: q.y };
    }
  }
  const radial = o.view_version === 2 ? sanitizeRadial(o.radial) : {};
  return { view_version: 2, strategy: o.strategy, positions, radial };
}

export interface KeyValueStore {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

/** Everything saved for a strategy in the store; empty when nothing is there or storage is blocked. */
export function loadViewFile(
  store: KeyValueStore,
  strategyName: string,
): { positions: Positions; radial: RadialSettings } {
  try {
    const raw = store.getItem(viewKey(strategyName));
    const v = raw ? parseView(raw) : null;
    return { positions: v?.positions ?? {}, radial: v?.radial ?? {} };
  } catch {
    return { positions: {}, radial: {} }; // storage blocked: the automatic layout still works
  }
}

export function loadView(store: KeyValueStore, strategyName: string): Positions {
  return loadViewFile(store, strategyName).positions;
}

export function saveView(
  store: KeyValueStore,
  strategyName: string,
  positions: Positions,
  radial: RadialSettings = {},
): void {
  try {
    store.setItem(
      viewKey(strategyName),
      serializeView({ strategy: strategyName, positions, radial }),
    );
  } catch {
    // storage full or blocked: positions are kept for this session only
  }
}

function startsWith(p: Path, prefix: Path): boolean {
  return prefix.length <= p.length && prefix.every((s, i) => s === p[i]);
}

/** Moves saved positions along with an edit, so a reordered or removed list item keeps (or loses) its own position. */
export function remapPositions(positions: Positions, op: ViewOp): Positions {
  const out: Positions = {};
  for (const [id, pos] of Object.entries(positions)) {
    const p = parsePath(id);
    if (!p) continue;
    if (op.kind === "drop") {
      if (!startsWith(p, op.prefix)) out[id] = pos;
      continue;
    }
    if (!startsWith(p, op.array) || typeof p[op.array.length] !== "number") {
      out[id] = pos;
      continue;
    }
    const at = op.array.length;
    const i = p[at] as number;
    let j: number | null = i;
    if (op.kind === "insert") j = i >= op.index ? i + 1 : i;
    else if (op.kind === "remove") j = i === op.index ? null : i > op.index ? i - 1 : i;
    else if (i === op.from) j = op.to;
    else if (op.from < op.to && i > op.from && i <= op.to) j = i - 1;
    else if (op.from > op.to && i >= op.to && i < op.from) j = i + 1;
    if (j === null) continue;
    const q = p.slice();
    q[at] = j;
    out[pathToId(q)] = pos;
  }
  return out;
}
