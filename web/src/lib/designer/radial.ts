/**
 * Per-node Radial layout settings (prototype), modelled on Superalgos v1: a node's place is
 * defined relative to its parent (an angle offset from the parent's outward direction, plus a
 * distance), so moving a node carries its whole subtree. Pure: no React, no DOM.
 *
 * The automatic layout (`radialLayout`) is converted to relative form once; settings are then
 * overrides applied on top, top-down:
 *  - spread: the node's children are fanned evenly over 360/180/90/45 degrees, centred on the
 *    node's outward direction (children that are pinned are left out of the fan);
 *  - distance: factor on the node's own distance to its parent (ignored while pinned);
 *  - pin: the node sits at a fixed angle offset and distance from its parent.
 * Settings live in the view file / localStorage only, never in the strategy (SEP-5.10), and are
 * keyed by `stableKeys` so they survive inserting or removing a sibling above.
 */
import { getAt, isObject, type Json } from "./doc";
import type { Graph, Positions } from "./graph";

export type Spread = 360 | 180 | 90 | 45;
export const SPREADS: readonly Spread[] = [360, 180, 90, 45];
/** Multipliers of the default distance to the parent (v1's choices). */
export const DISTANCES: readonly number[] = [0.25, 0.5, 1, 1.5, 2];

export const MIN_PIN_DISTANCE = 40;
export const MAX_PIN_DISTANCE = 5000;
const MIN_FACTOR = 0.1;
const MAX_FACTOR = 5;
/** Smallest centre-to-centre distance wanted between fanned siblings (same as layouts' MIN_ARC). */
const FAN_MIN_ARC = 150;
/** Direction "away from the parent" of the root: straight up. */
const UP = -Math.PI / 2;

export interface RadialPin {
  /** Degrees, clockwise (screen coordinates), relative to the parent's outward direction. */
  angle: number;
  /** Pixels from the parent's centre. */
  distance: number;
}

export interface RadialSetting {
  /** Absent = automatic. */
  spread?: Spread;
  /** Absent = 1. */
  distance?: number;
  pin?: RadialPin;
}

export type RadialSettings = Record<string, RadialSetting>;

export interface RadialPatch {
  spread?: Spread | null;
  distance?: number | null;
  pin?: RadialPin | null;
}

const rad = (deg: number) => (deg * Math.PI) / 180;
const deg = (r: number) => (r * 180) / Math.PI;

/** Angle in radians folded into (-PI, PI]. */
function fold(a: number): number {
  let x = a % (2 * Math.PI);
  if (x > Math.PI) x -= 2 * Math.PI;
  else if (x <= -Math.PI) x += 2 * Math.PI;
  return x;
}

/** Degrees folded into (-180, 180]. */
function foldDeg(a: number): number {
  let x = a % 360;
  if (x > 180) x -= 360;
  else if (x <= -180) x += 360;
  return x;
}

// ---- stable keys ------------------------------------------------------------------------

function segment(doc: Json, prefix: (string | number)[], step: string | number): string {
  if (typeof step === "string") return `.${step}`;
  const arr = getAt(doc, prefix);
  const item = Array.isArray(arr) ? arr[step] : undefined;
  if (Array.isArray(arr) && isObject(item)) {
    for (const field of ["id", "name"] as const) {
      const v = item[field];
      if (typeof v !== "string" || v === "") continue;
      const same = arr.filter((o) => isObject(o) && o[field] === v).length;
      if (same === 1) return `[${field}=${JSON.stringify(v)}]`;
    }
  }
  return `[${step}]`;
}

/**
 * Key of every node, used to store its settings. List items are identified by their own `id`
 * (orders, rules, phases) or `name` (situations) when that is unique among their siblings, and by
 * position otherwise, so inserting an item above does not hand its settings to its neighbour.
 */
export function stableKeys(doc: Json, graph: Graph): Map<string, string> {
  const out = new Map<string, string>();
  for (const n of graph.nodes) {
    let key = "$";
    for (let i = 0; i < n.path.length; i++) {
      key += segment(doc, n.path.slice(0, i), n.path[i] as string | number);
    }
    out.set(n.id, key);
  }
  return out;
}

// ---- editing the settings (immutable) ---------------------------------------------------

export function isEmptySetting(s: RadialSetting): boolean {
  return s.spread === undefined && (s.distance === undefined || s.distance === 1) && !s.pin;
}

/** Returns new settings with `patch` applied to `key`; `null` clears a field, empty entries vanish. */
export function patchSetting(
  settings: RadialSettings,
  key: string,
  patch: RadialPatch,
): RadialSettings {
  const cur: RadialSetting = { ...(settings[key] ?? {}) };
  if (patch.spread !== undefined) {
    if (patch.spread === null) delete cur.spread;
    else cur.spread = patch.spread;
  }
  if (patch.distance !== undefined) {
    if (patch.distance === null || patch.distance === 1) delete cur.distance;
    else cur.distance = patch.distance;
  }
  if (patch.pin !== undefined) {
    if (patch.pin === null) delete cur.pin;
    else cur.pin = patch.pin;
  }
  const next = { ...settings };
  if (isEmptySetting(cur)) delete next[key];
  else next[key] = cur;
  return next;
}

/** Settings without the node `id` and everything below it (the full graph, collapsed nodes included). */
export function clearBranch(
  settings: RadialSettings,
  graph: Graph,
  keys: ReadonlyMap<string, string>,
  id: string,
): RadialSettings {
  const next = { ...settings };
  const walk = (nid: string) => {
    const k = keys.get(nid);
    if (k !== undefined) delete next[k];
    for (const c of graph.byId.get(nid)?.children ?? []) walk(c);
  };
  walk(id);
  return next;
}

/** Keeps only settings whose key belongs to a node that exists. */
export function pruneRadial(settings: RadialSettings, live: ReadonlySet<string>): RadialSettings {
  return Object.fromEntries(Object.entries(settings).filter(([k]) => live.has(k)));
}

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Reads settings from untrusted JSON: bad entries are dropped, never thrown on. */
export function sanitizeRadial(raw: unknown): RadialSettings {
  const out: RadialSettings = {};
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return out;
  for (const [key, v] of Object.entries(raw)) {
    if (typeof v !== "object" || v === null) continue;
    const o = v as Record<string, unknown>;
    const s: RadialSetting = {};
    if (SPREADS.includes(o.spread as Spread)) s.spread = o.spread as Spread;
    if (finite(o.distance) && o.distance >= MIN_FACTOR && o.distance <= MAX_FACTOR)
      s.distance = o.distance;
    const p = o.pin as Record<string, unknown> | null | undefined;
    if (p && finite(p.angle) && finite(p.distance)) {
      s.pin = {
        angle: foldDeg(p.angle),
        distance: Math.min(MAX_PIN_DISTANCE, Math.max(MIN_PIN_DISTANCE, p.distance)),
      };
    }
    if (!isEmptySetting(s)) out[key] = s;
  }
  return out;
}

/** Settings of the nodes in `visible`, keyed by node id (what `applyRadial` takes). */
export function resolveSettings(
  settings: RadialSettings,
  keys: ReadonlyMap<string, string>,
  visible: Graph,
): Record<string, RadialSetting> {
  const out: Record<string, RadialSetting> = {};
  for (const n of visible.nodes) {
    const k = keys.get(n.id);
    const s = k === undefined ? undefined : settings[k];
    if (s) out[n.id] = s;
  }
  return out;
}

// ---- applying ---------------------------------------------------------------------------

function heading(positions: Positions, graph: Graph, id: string): number {
  const n = graph.byId.get(id);
  const p = n?.parent ? positions[n.parent] : undefined;
  const q = positions[id];
  if (!n || !p || !q) return UP;
  const dx = q.x - p.x;
  const dy = q.y - p.y;
  return Math.hypot(dx, dy) < 1e-9 ? UP : Math.atan2(dy, dx);
}

/** Angle offset (degrees) of child `i` of `k` fanned over `spread` degrees, centred on 0. */
export function fanOffset(spread: Spread, i: number, k: number): number {
  return -spread / 2 + (spread * (i + 0.5)) / k;
}

/** Distance at which `k` children fanned over `spread` degrees are at least FAN_MIN_ARC apart. */
export function fanDistance(spread: Spread, k: number): number {
  if (k < 2) return 0;
  return FAN_MIN_ARC / (2 * Math.sin(rad(spread / k) / 2));
}

function clampDistance(d: number, fallback: number): number {
  return Number.isFinite(d) ? Math.max(MIN_PIN_DISTANCE, Math.min(MAX_PIN_DISTANCE, d)) : fallback;
}

/**
 * Positions after applying `settings` (by node id) to the automatic layout `base` of `visible`.
 * With no settings the result is `base` itself.
 */
export function applyRadial(
  visible: Graph,
  base: Positions,
  settings: Readonly<Record<string, RadialSetting>>,
): Positions {
  const root = visible.nodes[0];
  if (!root || Object.keys(settings).length === 0) return base;

  const out: Positions = { [root.id]: { x: 0, y: 0 } };
  const head = new Map<string, number>([[root.id, UP]]);
  const baseHead = new Map<string, number>([[root.id, UP]]);

  for (const n of visible.nodes) {
    const here = out[n.id];
    const h = head.get(n.id);
    const bh = baseHead.get(n.id);
    const b = base[n.id];
    if (!here || h === undefined || bh === undefined || !b) continue;
    const free = n.children.filter((c) => !settings[c]?.pin);
    const spread = settings[n.id]?.spread;
    const fanAt = spread ? Math.max(0, fanDistance(spread, free.length)) : 0;
    for (const c of n.children) {
      const cb = base[c];
      if (!cb) continue;
      const dx = cb.x - b.x;
      const dy = cb.y - b.y;
      const baseDist = Math.hypot(dx, dy);
      const degenerate = !(baseDist > 1e-9);
      const cbh = degenerate ? bh : Math.atan2(dy, dx);
      baseHead.set(c, cbh);
      const s = settings[c];
      let offset = degenerate ? 0 : fold(cbh - bh);
      let dist = degenerate ? MIN_PIN_DISTANCE : baseDist;
      if (s?.pin) {
        offset = rad(s.pin.angle);
        dist = clampDistance(s.pin.distance, dist);
      } else {
        if (spread) {
          offset = rad(fanOffset(spread, free.indexOf(c), free.length));
          dist = Math.max(dist, fanAt);
        }
        const f = s?.distance;
        if (f !== undefined && Number.isFinite(f))
          dist *= Math.min(MAX_FACTOR, Math.max(MIN_FACTOR, f));
      }
      const ch = h + offset;
      head.set(c, ch);
      out[c] = { x: here.x + dist * Math.cos(ch), y: here.y + dist * Math.sin(ch) };
    }
  }
  return out;
}

/**
 * The pin that would put node `id` with its centre at `centre`, given where everything is now:
 * angle relative to the parent's outward direction (snapped to whole degrees) and distance
 * (whole pixels, never closer than MIN_PIN_DISTANCE). Null for the root, which cannot move.
 */
export function pinFromCentre(
  positions: Positions,
  visible: Graph,
  id: string,
  centre: { x: number; y: number },
): RadialPin | null {
  const n = visible.byId.get(id);
  const parent = n?.parent ? positions[n.parent] : undefined;
  if (!n?.parent || !parent) return null;
  const dx = centre.x - parent.x;
  const dy = centre.y - parent.y;
  if (!Number.isFinite(dx + dy)) return null;
  const outward = heading(positions, visible, n.parent);
  const angle = Math.round(
    deg(fold((Math.hypot(dx, dy) < 1e-9 ? outward : Math.atan2(dy, dx)) - outward)),
  );
  return {
    angle: angle === -180 ? 180 : angle,
    distance: Math.round(clampDistance(Math.hypot(dx, dy), MIN_PIN_DISTANCE)),
  };
}
