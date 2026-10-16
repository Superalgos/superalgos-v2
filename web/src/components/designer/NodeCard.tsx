import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { memo } from "react";
import type { NodeKind } from "../../lib/designer/graph";

export interface CardData extends Record<string, unknown> {
  title: string;
  summary: string;
  kind: NodeKind;
  /** Problems the server reported on this very node. */
  errors: number;
  /** A problem somewhere below this node. */
  below: boolean;
}

export type CardNode = Node<CardData, "card">;

const KIND_LABEL: Record<NodeKind, string> = {
  strategy: "strategy",
  stage: "stage",
  event: "event · OR",
  situation: "situation · AND",
  condition: "condition",
  order: "order",
  formula: "formula",
  rule: "manage rule",
  level: "phases",
  phase: "phase",
  goto: "move to",
};

function Card({ data, selected }: NodeProps<CardNode>) {
  const cls = ["dn", `dn-${data.kind}`];
  if (selected) cls.push("dn-selected");
  if (data.errors > 0) cls.push("dn-bad");
  else if (data.below) cls.push("dn-below");
  return (
    <div className={cls.join(" ")}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="dn-kind">
        {KIND_LABEL[data.kind]}
        {data.errors > 0 ? (
          <b className="dn-flag" role="img" aria-label={`${data.errors} problems`}>
            ! {data.errors}
          </b>
        ) : data.below ? (
          <b className="dn-flag dn-flag-below" role="img" aria-label="a problem inside">
            ▾ inside
          </b>
        ) : null}
      </div>
      <div className="dn-title">{data.title}</div>
      <div className="dn-summary">{data.summary}</div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

export const NodeCard = memo(Card);
