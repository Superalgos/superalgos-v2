/**
 * Designer layouts: the radial tree (the default view) and the collapse logic it uses. Pure: no
 * React, no DOM. Positions are node *centres*; they are never saved
 * in the document (SEP-5.10) and never mixed with the user's dragged positions.
 */
import { hierarchy, tree } from "d3-hierarchy";
import type { DNode, Graph, NodeKind, Positions } from "./graph";
import { applyRadial, type RadialSetting } from "./radial";

export type LayoutMode = "cards" | "radial";

export const LAYOUT_MODES: readonly LayoutMode[] = ["cards", "radial"];

/** Above this many nodes, everything below the situations starts collapsed. */
export const BIG_GRAPH = 40;

/** Smallest centre-to-centre distance wanted on a ring, and between two rings. */
export const MIN_ARC = 150;
export const RING_MIN = 170;

export function isLayoutMode(v: unknown): v is LayoutMode {
  return v === "cards" || v === "radial";
}

// ---- collapse ---------------------------------------------------------------------------

/** Total number of nodes below each node (not counting the node itself). */
export function descendantCounts(graph: Graph): Map<string, number> {
  const out = new Map<string, number>();
  // nodes are in pre-order, so walking backwards sees every child before its parent
  for (let i = graph.nodes.length - 1; i >= 0; i--) {
    const n = graph.nodes[i] as DNode;
    let c = 0;
    for (const k of n.children) c += 1 + (out.get(k) ?? 0);
    out.set(n.id, c);
  }
  return out;
}

/** Stages start expanded. In a big strategy everything below a situation starts collapsed. */
export function defaultCollapsed(graph: Graph, threshold: number = BIG_GRAPH): Set<string> {
  const out = new Set<string>();
  if (graph.nodes.length <= threshold) return out;
  for (const n of graph.nodes) if (n.kind === "situation" && n.children.length > 0) out.add(n.id);
  return out;
}

/** The graph a layout sees: collapsed nodes keep their place but lose their subtree. */
export function visibleGraph(graph: Graph, collapsed: ReadonlySet<string>): Graph {
  const nodes: DNode[] = [];
  const byId = new Map<string, DNode>();
  const edges: Graph["edges"] = [];
  const root = graph.nodes[0];
  if (!root) return { nodes, edges, byId };
  const walk = (n: DNode) => {
    const hide = collapsed.has(n.id);
    const copy: DNode = { ...n, children: hide ? [] : [...n.children] };
    nodes.push(copy);
    byId.set(copy.id, copy);
    for (const k of copy.children) {
      const child = graph.byId.get(k);
      if (!child) continue;
      edges.push({ id: `${n.id}->${k}`, source: n.id, target: k });
      walk(child);
    }
  };
  walk(root);
  return { nodes, edges, byId };
}

/** Collapse state after revealing `id`: its ancestors are expanded (the node itself is untouched). */
export function expandTo(graph: Graph, collapsed: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(collapsed);
  let cur = graph.byId.get(id)?.parent ?? null;
  while (cur) {
    next.delete(cur);
    cur = graph.byId.get(cur)?.parent ?? null;
  }
  return next;
}

export function toggled(collapsed: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(collapsed);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

// ---- radial -----------------------------------------------------------------------------

function asHierarchy(graph: Graph, rootNode: DNode) {
  return hierarchy<DNode>(rootNode, (d) =>
    d.children.flatMap((k) => {
      const c = graph.byId.get(k);
      return c ? [c] : [];
    }),
  );
}

/**
 * Radial tree: strategy at the centre, one ring per depth. d3's `tree().size([span, 1])` gives
 * the angles (separation shrinks with depth, so arcs stay even); each ring's radius is then the
 * smallest that keeps neighbours at least MIN_ARC apart, and at least RING_MIN beyond the ring
 * before it.
 */
export function radialLayout(graph: Graph): Positions {
  const rootNode = graph.nodes[0];
  if (!rootNode) return {};
  const h = asHierarchy(graph, rootNode);
  const leaves = h.leaves().length;
  const span = (2 * Math.PI * leaves) / (leaves + 1);
  const placed = tree<DNode>()
    .size([span, 1])
    .separation((a, b) => (a.parent === b.parent ? 1 : 2) / Math.max(a.depth, 1))(h);

  const rings = new Map<number, number[]>();
  for (const d of placed.descendants()) {
    const list = rings.get(d.depth) ?? [];
    list.push(d.x);
    rings.set(d.depth, list);
  }
  const radius = new Map<number, number>([[0, 0]]);
  const maxDepth = Math.max(...rings.keys());
  for (let depth = 1; depth <= maxDepth; depth++) {
    const xs = [...(rings.get(depth) ?? [])].sort((a, b) => a - b);
    let minGap = Number.POSITIVE_INFINITY;
    for (let i = 1; i < xs.length; i++)
      minGap = Math.min(minGap, (xs[i] as number) - (xs[i - 1] as number));
    if (xs.length > 1)
      minGap = Math.min(minGap, 2 * Math.PI - ((xs[xs.length - 1] as number) - (xs[0] as number)));
    const need = Number.isFinite(minGap) && minGap > 0 ? MIN_ARC / minGap : 0;
    radius.set(depth, Math.max((radius.get(depth - 1) as number) + RING_MIN, need));
  }

  const out: Positions = {};
  for (const d of placed.descendants()) {
    const r = radius.get(d.depth) as number;
    const a = d.x - Math.PI / 2;
    out[d.data.id] = d.depth === 0 ? { x: 0, y: 0 } : { x: r * Math.cos(a), y: r * Math.sin(a) };
  }
  return out;
}

export function layoutFor(mode: LayoutMode, graph: Graph): Positions {
  if (mode === "radial") return radialLayout(graph);
  return {};
}

// ---- memoisation on structure -----------------------------------------------------------

/** Identity of the layout inputs: which nodes are shown, under which parent, in which order. */
export function structureKey(mode: LayoutMode, visible: Graph): string {
  let s = mode;
  for (const n of visible.nodes) s += `|${n.id}>${n.parent ?? ""}`;
  return s;
}

export interface LayoutCache {
  /**
   * Positions for the visible graph; the same object while the structure (and, in radial mode,
   * the settings of the visible nodes) are unchanged. `radial` is keyed by node id.
   */
  get(
    mode: LayoutMode,
    visible: Graph,
    radial?: Readonly<Record<string, RadialSetting>>,
  ): Positions;
  /** How many times the automatic layout was actually computed. */
  computed(): number;
  /** How many times radial settings were applied on top of it. */
  applied(): number;
}

function settingsSignature(
  visible: Graph,
  radial: Readonly<Record<string, RadialSetting>>,
): string {
  let s = "";
  for (const n of visible.nodes) {
    const r = radial[n.id];
    if (r) s += `|${n.id}:${JSON.stringify(r)}`;
  }
  return s;
}

/**
 * Remembers the last layout. Editing a condition changes text, not structure, so the key stays
 * equal and the O(n log n) layout is skipped on every keystroke. Radial settings are applied on
 * top of the remembered automatic layout and are part of their own key.
 */
export function createLayoutCache(): LayoutCache {
  let key: string | null = null;
  let base: Positions = {};
  let sig: string | null = null;
  let final: Positions = {};
  let runs = 0;
  let applies = 0;
  return {
    get(mode, visible, radial) {
      const k = structureKey(mode, visible);
      if (k !== key) {
        base = layoutFor(mode, visible);
        key = k;
        sig = null;
        runs++;
      }
      if (mode !== "radial" || !radial) return base;
      const s = settingsSignature(visible, radial);
      if (s === "") return base;
      if (s !== sig) {
        final = applyRadial(visible, base, radial);
        sig = s;
        applies++;
      }
      return final;
    },
    computed: () => runs,
    applied: () => applies,
  };
}

// ---- icons ------------------------------------------------------------------------------

/** File name (in `assets/v1-icons`) of the v1 icon that stands for a node. */
export function iconFor(kind: NodeKind, title: string): string {
  switch (kind) {
    case "strategy":
      return "strategy.png";
    case "stage":
      return title === "Trigger"
        ? "trigger-stage.png"
        : title === "Open"
          ? "open-stage.png"
          : title === "Manage"
            ? "manage-stage.png"
            : "close-stage.png";
    case "event":
      if (title.startsWith("Trigger on")) return "trigger-on-event.png";
      if (title.startsWith("Trigger off")) return "trigger-off-event.png";
      if (title.startsWith("Take position")) return "take-position-event.png";
      if (title.startsWith("Close")) return "close-stage-event.png";
      if (title.startsWith("Create order")) return "create-order-event.png";
      if (title.startsWith("Cancel order")) return "cancel-order-event.png";
      if (title.startsWith("Next phase")) return "next-phase-event.png";
      if (title.startsWith("Move to phase")) return "move-to-phase-event.png";
      return "execution-started-event.png";
    case "situation":
      return "situation.png";
    case "condition":
      return "condition.png";
    case "order":
      return "limit-order.png";
    case "formula":
      return "formula.png";
    case "rule":
      return "managed-tasks.png";
    case "level":
      return title.startsWith("Stop") ? "stop-loss.png" : "take-profit.png";
    case "phase":
      return "phase.png";
    case "goto":
      return "move-to-phase.png";
  }
}
