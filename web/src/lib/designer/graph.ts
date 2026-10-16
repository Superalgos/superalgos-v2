/** The strategy document as a tree of nodes and edges, with a tidy left-to-right layout. Pure. */
import { isObject, type Json, type Path, pathToId } from "./doc";
import {
  defName,
  itemsOf,
  keyOrder,
  kindOf,
  propsOf,
  type Resolved,
  resolve,
  rootSchema,
  TRANSPARENT_DEFS,
} from "./schema";

export type NodeKind =
  | "strategy"
  | "stage"
  | "event"
  | "situation"
  | "condition"
  | "order"
  | "formula"
  | "rule"
  | "level"
  | "phase"
  | "goto";

export interface DNode {
  id: string;
  path: Path;
  /** The `$defs` name ("Strategy" for the root), or "condition"/"formula" for DSL strings. */
  def: string;
  kind: NodeKind;
  title: string;
  summary: string;
  parent: string | null;
  children: string[];
  depth: number;
}

export interface Edge {
  id: string;
  source: string;
  target: string;
}

export interface Graph {
  nodes: DNode[];
  edges: Edge[];
  byId: Map<string, DNode>;
}

const KIND_OF_DEF: Record<string, NodeKind> = {
  Strategy: "strategy",
  Trigger: "stage",
  OpenStage: "stage",
  Manage: "stage",
  CloseStage: "stage",
  Event: "event",
  Situation: "situation",
  OrderSpec: "order",
  ManageRule: "rule",
  PhasedLevel: "level",
  Phase: "phase",
  Goto: "goto",
};

const STAGE_TITLE: Record<string, string> = {
  Trigger: "Trigger",
  OpenStage: "Open",
  Manage: "Manage",
  CloseStage: "Close",
};

/** Title of an event node from the model and property that hold it. */
const EVENT_TITLE: Record<string, string> = {
  "Trigger.on": "Trigger on",
  "Trigger.off": "Trigger off",
  "OpenStage.when": "Take position when",
  "CloseStage.when": "Close when",
  "ManageRule.when": "Rule fires when",
  "Goto.when": "Move to phase when",
  "OrderSpec.create": "Create order when",
  "OrderSpec.cancel": "Cancel order when",
  "Phase.next": "Next phase when",
};

const FORMULA_TITLE: Record<string, string> = { rate: "Rate", size: "Size", level: "Level" };

function str(v: Json | undefined): string {
  return typeof v === "string" ? v : "";
}

function clip(s: string, n = 60): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function pct(v: Json | undefined): string {
  return typeof v === "number" ? `${Number((v * 100).toFixed(2))}%` : "";
}

function summarize(def: string, v: Json): string {
  if (!isObject(v)) return typeof v === "string" ? clip(v, 80) : "";
  switch (def) {
    case "Strategy":
      return `${str(v.timeframe)} · schema ${String(v.schema_version ?? "")}`;
    case "OrderSpec": {
      const size = typeof v.size === "string" ? `size ${clip(v.size, 24)}` : pct(v.size_fraction);
      const rate =
        typeof v.rate === "string"
          ? `rate ${clip(v.rate, 24)}`
          : typeof v.offset_pct === "number" && v.offset_pct !== 0
            ? `${v.offset_pct}%`
            : "";
      return [str(v.type) || "market", size, rate, v.rearm === true ? "re-arm" : ""]
        .filter(Boolean)
        .join(" · ");
    }
    case "ManageRule":
      return [str(v.action), typeof v.size_fraction === "number" ? pct(v.size_fraction) : ""]
        .filter(Boolean)
        .join(" · ");
    case "Phase":
      return clip(str(v.level), 60);
    case "PhasedLevel":
      return `${Array.isArray(v.phases) ? v.phases.length : 0} phases`;
    case "Goto":
      return `to ${str(v.to) || "?"}`;
    case "Event":
      return `OR of ${Array.isArray(v.any) ? v.any.length : 0}`;
    case "Situation":
      return `AND of ${Array.isArray(v.all) ? v.all.length : 0}`;
    case "OpenStage":
      return `${Array.isArray(v.orders) ? v.orders.length : 0} orders`;
    case "Manage":
      return `${Array.isArray(v.rules) ? v.rules.length : 0} rules`;
    default:
      return "";
  }
}

function titleOf(
  def: string,
  parentDef: string | null,
  key: string,
  index: number | null,
  v: Json,
): string {
  if (def === "Strategy") return str((v as Record<string, Json>).name) || "(unnamed strategy)";
  const stage = STAGE_TITLE[def];
  if (stage) return stage;
  if (def === "Event") return EVENT_TITLE[`${parentDef}.${key}`] ?? "Event";
  if (def === "PhasedLevel") return key === "stop" ? "Stop (phases)" : "Target (phases)";
  if (!isObject(v)) return key;
  if (def === "Situation") return str(v.name) || `Situation ${(index ?? 0) + 1}`;
  if (def === "OrderSpec" || def === "ManageRule" || def === "Phase")
    return str(v.id) || `${key}[${index}]`;
  if (def === "Goto") return `Goto ${(index ?? 0) + 1}`;
  return key;
}

/** Builds the node tree from the document, reading which properties are nodes from the schema. */
export function buildGraph(doc: Json): Graph {
  const nodes: DNode[] = [];
  const byId = new Map<string, DNode>();
  const add = (n: DNode, parent: DNode | null) => {
    nodes.push(n);
    byId.set(n.id, n);
    if (parent) parent.children.push(n.id);
  };

  const visit = (
    value: Json,
    r: Resolved,
    path: Path,
    parent: DNode | null,
    parentDef: string | null,
    key: string,
    index: number | null,
  ): void => {
    const kind = kindOf(r);
    const depth = parent ? parent.depth + 1 : 0;
    if (kind === "dsl") {
      const isCond = r.schema["x-dsl"] === "boolean";
      add(
        {
          id: pathToId(path),
          path,
          def: isCond ? "condition" : "formula",
          kind: isCond ? "condition" : "formula",
          title: isCond ? `Condition ${(index ?? 0) + 1}` : (FORMULA_TITLE[key] ?? key),
          summary: typeof value === "string" ? clip(value, 90) : "",
          parent: parent?.id ?? null,
          children: [],
          depth,
        },
        parent,
      );
      return;
    }
    if (kind !== "object" || !isObject(value)) return;
    const def = defName(r);
    let self = parent;
    if (!TRANSPARENT_DEFS.has(def)) {
      self = {
        id: pathToId(path),
        path,
        def,
        kind: KIND_OF_DEF[def] ?? "stage",
        title: titleOf(def, parentDef, key, index, value),
        summary: summarize(def, value),
        parent: parent?.id ?? null,
        children: [],
        depth,
      };
      add(self, parent);
    }
    const order = keyOrder(def);
    const present = propsOf(r).filter((p) => value[p.key] !== undefined && value[p.key] !== null);
    present.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
    for (const p of present) {
      const child = value[p.key] as Json;
      if (p.kind === "object" || p.kind === "dsl") {
        visit(child, p.resolved, [...path, p.key], self, def, p.key, null);
      } else if (p.kind === "array" && Array.isArray(child)) {
        const item = itemsOf(p.resolved);
        if (!item) continue;
        child.forEach((it, i) => {
          visit(it, item, [...path, p.key, i], self, def, p.key, i);
        });
      }
    }
  };

  visit(doc, resolve(rootSchema), [], null, null, "", null);
  const edges: Edge[] = nodes
    .filter((n) => n.parent)
    .map((n) => ({ id: `${n.parent}->${n.id}`, source: n.parent as string, target: n.id }));
  return { nodes, edges, byId };
}

export const NODE_W = 248;
export const NODE_H = 72;
export const GAP_X = 56;
export const GAP_Y = 22;

export type Positions = Record<string, { x: number; y: number }>;

/** Tidy tree: x by depth, y by leaf order with parents centred on their children. Saved positions win. */
export function layout(graph: Graph, saved: Positions = {}): Positions {
  const out: Positions = {};
  let slot = 0;
  const place = (id: string): number => {
    const n = graph.byId.get(id) as DNode;
    let y: number;
    if (n.children.length === 0) y = slot++ * (NODE_H + GAP_Y);
    else {
      const ys = n.children.map(place);
      y = ((ys[0] as number) + (ys[ys.length - 1] as number)) / 2;
    }
    out[id] = { x: n.depth * (NODE_W + GAP_X), y };
    return y;
  };
  const root = graph.nodes[0];
  if (root) place(root.id);
  for (const [id, p] of Object.entries(saved)) if (out[id]) out[id] = p;
  return out;
}
