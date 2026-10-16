import {
  Background,
  Controls,
  type Edge,
  MiniMap,
  type NodeChange,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ErrorIndex } from "../../lib/designer/errors";
import { type Graph, layout, NODE_H, NODE_W, type Positions } from "../../lib/designer/graph";
import type { LayoutMode } from "../../lib/designer/layouts";
import type { RadialSettings } from "../../lib/designer/radial";
import { type CardData, type CardNode, NodeCard } from "./NodeCard";
import { V1Canvas } from "./V1Canvas";

const nodeTypes = { card: NodeCard };
const FIT = { padding: 0.15, duration: 200 } as const;
const NO_EDGE_OPTIONS = { type: "smoothstep" } as const;

export interface CanvasProps {
  graph: Graph;
  /** Positions the user set (view file); the rest is laid out automatically. */
  positions: Positions;
  selected: string;
  errors: ErrorIndex;
  /** Node to bring into view (from the problems list). */
  focus: { id: string; nonce: number } | null;
  /** Changes when the whole graph should be fitted into view (load, Auto layout). */
  fit?: number;
  /** `cards` (default) is the original canvas; the others are the v1-style prototype layouts. */
  mode?: LayoutMode;
  onSelect: (id: string) => void;
  onContextMenu: (id: string, x: number, y: number, ops?: { pin: () => void }) => void;
  /** Radial layout settings (by stable key), the key of each node id, and where changes go. */
  radial?: RadialSettings;
  keys?: ReadonlyMap<string, string>;
  onRadialChange?: (next: RadialSettings) => void;
  onMove: (positions: Positions) => void;
  onMoveEnd: () => void;
}

/** Builds the React Flow nodes, reusing the previous `data` objects when nothing changed so memoised cards skip rendering. */
export function toFlow(
  graph: Graph,
  positions: Positions,
  selected: string,
  errors: ErrorIndex,
  cache: Map<string, CardData>,
): { nodes: CardNode[]; edges: Edge[] } {
  const placed = layout(graph, positions);
  const nodes: CardNode[] = graph.nodes.map((n) => {
    const count = errors.own.get(n.id)?.length ?? 0;
    const below = errors.below.has(n.id);
    const old = cache.get(n.id);
    const data =
      old &&
      old.title === n.title &&
      old.summary === n.summary &&
      old.kind === n.kind &&
      old.errors === count &&
      old.below === below
        ? old
        : { title: n.title, summary: n.summary, kind: n.kind, errors: count, below };
    cache.set(n.id, data);
    return {
      id: n.id,
      type: "card",
      position: placed[n.id] ?? { x: 0, y: 0 },
      width: NODE_W,
      height: NODE_H,
      selected: n.id === selected,
      data,
    };
  });
  const edges: Edge[] = graph.edges.map((e) => ({ ...e, type: "smoothstep" }));
  return { nodes, edges };
}

function Inner(p: CanvasProps) {
  const cache = useRef(new Map<string, CardData>());
  const { nodes, edges } = useMemo(
    () => toFlow(p.graph, p.positions, p.selected, p.errors, cache.current),
    [p.graph, p.positions, p.selected, p.errors],
  );
  const rf = useReactFlow();
  const { focus } = p;
  useEffect(() => {
    if (!focus) return;
    const n = nodes.find((x) => x.id === focus.id);
    if (n)
      rf.setCenter(n.position.x + NODE_W / 2, n.position.y + NODE_H / 2, {
        zoom: 1,
        duration: 200,
      });
    // only when the request changes, not when the nodes move
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.nonce]);

  // fit the whole graph after a load or Auto layout, once the new positions are rendered
  const [showMap, setShowMap] = useState(false);
  const fitNonce = p.fit ?? 0;
  const firstFit = useRef(true);
  useEffect(() => {
    if (firstFit.current) {
      firstFit.current = false;
      return;
    }
    const t = setTimeout(() => void rf.fitView(FIT), 50);
    return () => clearTimeout(t);
    // only when the request changes, not when the nodes move
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitNonce]);

  const { positions, onMove } = p;
  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      let next: Positions | null = null;
      for (const c of changes) {
        if (c.type === "position" && c.position) {
          next ??= { ...positions };
          next[c.id] = { x: c.position.x, y: c.position.y };
        }
      }
      if (next) onMove(next);
    },
    [positions, onMove],
  );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      defaultEdgeOptions={NO_EDGE_OPTIONS}
      onNodesChange={onNodesChange}
      onNodeClick={(_, n) => p.onSelect(n.id)}
      onNodeContextMenu={(ev, n) => {
        ev.preventDefault();
        p.onSelect(n.id);
        p.onContextMenu(n.id, ev.clientX, ev.clientY);
      }}
      onPaneClick={() => p.onSelect("$")}
      onNodeDragStop={p.onMoveEnd}
      nodesConnectable={false}
      deleteKeyCode={null}
      onlyRenderVisibleElements
      fitView
      fitViewOptions={{ padding: 0.15 }}
      minZoom={0.1}
      colorMode="system"
      proOptions={{ hideAttribution: false }}
    >
      <Background />
      <Controls showInteractive={false} />
      <Panel position="top-right">
        <button
          type="button"
          className="mm-toggle"
          aria-pressed={showMap}
          onClick={() => setShowMap((v) => !v)}
        >
          {showMap ? "Hide minimap" : "Show minimap"}
        </button>
      </Panel>
      {showMap && <MiniMap pannable zoomable position="top-right" className="mm" />}
    </ReactFlow>
  );
}

export const Canvas = memo(function Canvas(p: CanvasProps) {
  if (p.mode && p.mode !== "cards") {
    return (
      <V1Canvas
        mode={p.mode}
        graph={p.graph}
        selected={p.selected}
        errors={p.errors}
        focus={p.focus}
        fit={p.fit}
        onSelect={p.onSelect}
        onContextMenu={p.onContextMenu}
        radial={p.radial}
        keys={p.keys}
        onRadialChange={p.onRadialChange}
      />
    );
  }
  return (
    <ReactFlowProvider>
      <Inner {...p} />
    </ReactFlowProvider>
  );
});
