/** The v1-style canvas (prototype): icon nodes, status rings, flowing dashed edges, collapse. */
import {
  type EdgeProps,
  type Edge as FlowEdge,
  Handle,
  MiniMap,
  type Node,
  type NodeProps,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { V1_ICONS } from "../../assets/v1-icons";
import type { ErrorIndex } from "../../lib/designer/errors";
import type { Graph, NodeKind } from "../../lib/designer/graph";
import {
  createLayoutCache,
  defaultCollapsed,
  descendantCounts,
  expandTo,
  iconFor,
  type LayoutMode,
  toggled,
  visibleGraph,
} from "../../lib/designer/layouts";
import {
  applyRadial,
  patchSetting,
  pinFromCentre,
  type RadialSettings,
  resolveSettings,
} from "../../lib/designer/radial";

/** Box of one node (centres are what the layouts place); the icon centre is the edge anchor. */
export const V1_W = 140;
export const V1_H = 124;
const ICON = 56;
const LABEL_H = 34;
const CENTRE_Y = LABEL_H + ICON / 2;

const KIND_LABEL: Record<NodeKind, string> = {
  strategy: "Strategy",
  stage: "Stage",
  event: "Event",
  situation: "Situation",
  condition: "Condition",
  order: "Order",
  formula: "Formula",
  rule: "Manage rule",
  level: "Phases",
  phase: "Phase",
  goto: "Move to",
};

export interface IconData extends Record<string, unknown> {
  title: string;
  summary: string;
  kind: NodeKind;
  errors: number;
  below: boolean;
  hasChildren: boolean;
  collapsed: boolean;
  /** Nodes hidden under this one while it is collapsed. */
  hidden: number;
  onToggle: (id: string) => void;
}

export type IconNode = Node<IconData, "icon">;

const ANCHOR = { left: "50%", top: CENTRE_Y, opacity: 0, pointerEvents: "none" } as const;

function Icon({ id, data, selected }: NodeProps<IconNode>) {
  const ring = ["v1-ring"];
  if (selected) ring.push("v1-ring-selected");
  if (data.errors > 0) ring.push("v1-ring-bad");
  else if (data.below) ring.push("v1-ring-below");
  const file = iconFor(data.kind, data.title);
  const tip = [data.title, data.summary].filter(Boolean).join(" · ");
  return (
    <div className={`v1n v1n-${data.kind}`} style={{ width: V1_W, height: V1_H }} title={tip}>
      <Handle type="target" position={Position.Top} isConnectable={false} style={ANCHOR} />
      <div className="v1-name">{data.title}</div>
      <div className={ring.join(" ")} style={{ width: ICON + 16, height: ICON + 16 }}>
        <img src={V1_ICONS[file]} alt="" width={ICON} height={ICON} draggable={false} />
        <span className="v1-dot" />
      </div>
      <div className="v1-type">
        {KIND_LABEL[data.kind]}
        {data.errors > 0 && <b className="v1-flag"> ! {data.errors}</b>}
      </div>
      {selected && data.summary && <div className="v1-summary">{data.summary}</div>}
      {data.hasChildren && (
        <button
          type="button"
          className={data.collapsed ? "v1-toggle v1-toggle-closed" : "v1-toggle"}
          aria-label={
            data.collapsed
              ? `Expand ${data.title} (${data.hidden} hidden)`
              : `Collapse ${data.title}`
          }
          onClick={(e) => {
            e.stopPropagation();
            data.onToggle(id);
          }}
          onDoubleClick={(e) => e.stopPropagation()}
        >
          {data.collapsed ? `+${data.hidden}` : "−"}
        </button>
      )}
      <Handle type="source" position={Position.Bottom} isConnectable={false} style={ANCHOR} />
    </div>
  );
}

const IconCard = memo(Icon);

/** Straight line drawn child to parent so the dash animation runs toward the parent, as in v1. */
function Flow({ id, sourceX, sourceY, targetX, targetY }: EdgeProps<FlowEdge>) {
  return (
    <path
      id={id}
      className="v1-edge"
      d={`M${targetX},${targetY} L${sourceX},${sourceY}`}
      fill="none"
    />
  );
}

const nodeTypes = { icon: IconCard };
const edgeTypes = { flow: Flow };
const FIT = { padding: 0.15, duration: 200 } as const;

export interface V1CanvasProps {
  mode: Exclude<LayoutMode, "cards">;
  graph: Graph;
  selected: string;
  errors: ErrorIndex;
  focus: { id: string; nonce: number } | null;
  /** Changes when the strategy is (re)loaded or the layout is reset: collapse starts over. */
  fit?: number;
  onSelect: (id: string) => void;
  /** `ops.pin` (Radial only) pins the node where it is now. */
  onContextMenu: (id: string, x: number, y: number, ops?: { pin: () => void }) => void;
  /** Radial settings by stable key, the key of each node id, and where changes go. */
  radial?: RadialSettings;
  keys?: ReadonlyMap<string, string>;
  onRadialChange?: (next: RadialSettings) => void;
}

/** A drag that moved less than this many pixels is a click, not a pin. */
const DRAG_SLOP = 4;
const NO_SETTINGS: RadialSettings = {};
const NO_KEYS: ReadonlyMap<string, string> = new Map();

function Inner(p: V1CanvasProps) {
  const rf = useReactFlow();
  const [collapsed, setCollapsed] = useState(() => defaultCollapsed(p.graph));
  const cache = useRef(createLayoutCache());
  const data = useRef(new Map<string, IconData>());
  const onToggle = useCallback((id: string) => setCollapsed((c) => toggled(c, id)), []);

  const visible = useMemo(() => visibleGraph(p.graph, collapsed), [p.graph, collapsed]);
  // layout depends on the visible structure only, not on titles or condition text
  const radial = p.radial ?? NO_SETTINGS;
  const keys = p.keys ?? NO_KEYS;
  const byId = useMemo(() => resolveSettings(radial, keys, visible), [radial, keys, visible]);
  const resting = cache.current.get(p.mode, visible, byId);
  // while a node is being dragged, its subtree follows the pointer
  const [drag, setDrag] = useState<{ id: string; x: number; y: number } | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);
  const dragPin = useMemo(
    () => (drag && p.mode === "radial" ? pinFromCentre(resting, visible, drag.id, drag) : null),
    [drag, resting, visible, p.mode],
  );
  const positions = useMemo(
    () =>
      drag && dragPin
        ? applyRadial(visible, resting, { ...byId, [drag.id]: { ...byId[drag.id], pin: dragPin } })
        : resting,
    [drag, dragPin, visible, resting, byId],
  );
  const centreOf = (n: { position: { x: number; y: number } }) => ({
    x: n.position.x + V1_W / 2,
    y: n.position.y + CENTRE_Y,
  });
  const counts = useMemo(() => descendantCounts(p.graph), [p.graph]);

  const nodes = useMemo(() => {
    const out: IconNode[] = [];
    for (const n of visible.nodes) {
      const pos = positions[n.id] ?? { x: 0, y: 0 };
      const full = p.graph.byId.get(n.id) ?? n;
      const errs = p.errors.own.get(n.id)?.length ?? 0;
      const below = p.errors.below.has(n.id);
      const hasChildren = full.children.length > 0;
      const isCollapsed = collapsed.has(n.id) && hasChildren;
      const hidden = counts.get(n.id) ?? 0;
      const old = data.current.get(n.id);
      const d: IconData =
        old &&
        old.title === n.title &&
        old.summary === n.summary &&
        old.kind === n.kind &&
        old.errors === errs &&
        old.below === below &&
        old.hasChildren === hasChildren &&
        old.collapsed === isCollapsed &&
        old.hidden === hidden
          ? old
          : {
              title: n.title,
              summary: n.summary,
              kind: n.kind,
              errors: errs,
              below,
              hasChildren,
              collapsed: isCollapsed,
              hidden,
              onToggle,
            };
      data.current.set(n.id, d);
      out.push({
        id: n.id,
        type: "icon",
        position: { x: pos.x - V1_W / 2, y: pos.y - CENTRE_Y },
        width: V1_W,
        height: V1_H,
        selected: n.id === p.selected,
        draggable: p.mode === "radial" && n.parent !== null && !!p.onRadialChange,
        data: d,
      });
    }
    return out;
  }, [visible, positions, p.graph, p.errors, p.selected, collapsed, counts, onToggle]);

  const edges = useMemo<FlowEdge[]>(
    () => visible.edges.map((e) => ({ ...e, type: "flow", selectable: false })),
    [visible],
  );

  // problems list: reveal the node, then centre on it
  const { focus } = p;
  const pending = useRef<string | null>(null);
  useEffect(() => {
    if (!focus) return;
    setCollapsed((c) => expandTo(p.graph, c, focus.id));
    pending.current = focus.id;
    // only when the request changes
  }, [focus?.nonce]);
  useEffect(() => {
    const id = pending.current;
    if (!id) return;
    const pos = positions[id];
    if (!pos) return;
    pending.current = null;
    rf.setCenter(pos.x, pos.y, { zoom: 1, duration: 200 });
  }, [positions, rf]);

  // load / reset: start from the default collapse and fit the view
  const fitNonce = p.fit ?? 0;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setCollapsed(defaultCollapsed(p.graph));
    const t = setTimeout(() => void rf.fitView(FIT), 80);
    return () => clearTimeout(t);
    // only when the request changes
  }, [fitNonce]);

  // refit when the user collapses or expands (the visible set changes)
  const shown = visible.nodes.length;
  const lastShown = useRef(shown);
  useEffect(() => {
    if (lastShown.current === shown) return;
    lastShown.current = shown;
    const t = setTimeout(() => void rf.fitView(FIT), 50);
    return () => clearTimeout(t);
  }, [shown, rf]);

  const [showMap, setShowMap] = useState(false);
  const allOpen = collapsed.size === 0;

  return (
    <ReactFlow
      className="v1-flow"
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      nodesDraggable={p.mode === "radial" && !!p.onRadialChange}
      onNodeDragStart={(_, n) => {
        dragStart.current = centreOf(n);
      }}
      onNodeDrag={(_, n) => setDrag({ id: n.id, ...centreOf(n) })}
      onNodeDragStop={(_, n) => {
        const start = dragStart.current;
        const end = centreOf(n);
        dragStart.current = null;
        setDrag(null);
        const key = keys.get(n.id);
        if (!start || key === undefined || !p.onRadialChange) return;
        if (Math.hypot(end.x - start.x, end.y - start.y) < DRAG_SLOP) return;
        const pin = pinFromCentre(resting, visible, n.id, end);
        if (pin) p.onRadialChange(patchSetting(radial, key, { pin }));
      }}
      nodesConnectable={false}
      deleteKeyCode={null}
      zoomOnDoubleClick={false}
      onNodeClick={(_, n) => p.onSelect(n.id)}
      onNodeDoubleClick={(_, n) => {
        if ((p.graph.byId.get(n.id)?.children.length ?? 0) > 0) onToggle(n.id);
      }}
      onNodeContextMenu={(ev, n) => {
        ev.preventDefault();
        p.onSelect(n.id);
        const key = keys.get(n.id);
        const pin = () => {
          const at = resting[n.id];
          const spot = at ? pinFromCentre(resting, visible, n.id, at) : null;
          if (spot && key !== undefined)
            p.onRadialChange?.(patchSetting(radial, key, { pin: spot }));
        };
        p.onContextMenu(n.id, ev.clientX, ev.clientY, p.mode === "radial" ? { pin } : undefined);
      }}
      onPaneClick={() => p.onSelect("$")}
      onlyRenderVisibleElements
      fitView
      fitViewOptions={{ padding: 0.15 }}
      minZoom={0.05}
      colorMode="dark"
      proOptions={{ hideAttribution: false }}
    >
      <Panel position="top-right">
        <button
          type="button"
          className="mm-toggle"
          onClick={() =>
            setCollapsed(
              allOpen
                ? new Set(
                    p.graph.nodes
                      .filter((n) => n.children.length > 0 && n.parent !== null)
                      .map((n) => n.id),
                  )
                : new Set(),
            )
          }
        >
          {allOpen ? "Collapse all" : "Expand all"}
        </button>{" "}
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

export function V1Canvas(p: V1CanvasProps) {
  return (
    <ReactFlowProvider>
      <Inner {...p} />
    </ReactFlowProvider>
  );
}
