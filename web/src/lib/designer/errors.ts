/** Server validation errors `{path, column, message}` and the node each one belongs to (SEP-5.8). */
import { type Path, parsePath, pathToId } from "./doc";
import type { Graph } from "./graph";

export interface Issue {
  path: string;
  column: number | null;
  message: string;
}

export interface NodeIssue extends Issue {
  /** The node that owns the failing value: the longest node path that is a prefix of `path`. */
  nodeId: string;
  /** What is left of the path below that node (`size_fraction`, `rate`), or null when the node itself failed. */
  field: string | null;
}

/** Maps each issue to its owner node; paths outside the tree (or unreadable) land on the root. */
export function mapIssues(issues: readonly Issue[], graph: Graph): NodeIssue[] {
  const root = graph.nodes[0]?.id ?? "$";
  return issues.map((issue) => {
    const parsed = parsePath(issue.path);
    if (!parsed) return { ...issue, nodeId: root, field: issue.path === "$" ? null : issue.path };
    for (let n = parsed.length; n >= 0; n--) {
      const id = pathToId(parsed.slice(0, n));
      if (graph.byId.has(id)) {
        const rest = parsed.slice(n);
        return { ...issue, nodeId: id, field: rest.length ? restToField(rest) : null };
      }
    }
    return { ...issue, nodeId: root, field: issue.path };
  });
}

function restToField(rest: Path): string {
  return rest
    .map((s) => (typeof s === "number" ? `[${s}]` : s))
    .join(".")
    .replaceAll(".[", "[");
}

export interface ErrorIndex {
  /** Issues grouped by owner node. */
  own: Map<string, NodeIssue[]>;
  /** Nodes that have an issue somewhere below them (for the "problem inside" marker). */
  below: Set<string>;
}

export function indexIssues(mapped: readonly NodeIssue[], graph: Graph): ErrorIndex {
  const own = new Map<string, NodeIssue[]>();
  const below = new Set<string>();
  for (const m of mapped) {
    const list = own.get(m.nodeId);
    if (list) list.push(m);
    else own.set(m.nodeId, [m]);
    let cur = graph.byId.get(m.nodeId)?.parent ?? null;
    while (cur && !below.has(cur)) {
      below.add(cur);
      cur = graph.byId.get(cur)?.parent ?? null;
    }
  }
  return { own, below };
}

/** Two lines for a text and a failing column: the text, then a caret under the column. */
export function caretLine(text: string, column: number | null): string | null {
  if (column === null || column < 0) return null;
  return `${" ".repeat(Math.min(column, text.length))}^`;
}
