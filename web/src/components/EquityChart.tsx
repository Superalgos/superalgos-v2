import { useId } from "react";
import type { EquityPoint } from "../api/types";
import { lineGeometry, totalReturnPct } from "../lib/chart";
import { fmtNum, fmtPct } from "../lib/format";

const BOX = { width: 520, height: 220, left: 54, right: 12, top: 10, bottom: 24 };

export function EquityChart({ points, title }: { points: EquityPoint[]; title: string }) {
  const id = useId();
  if (points.length === 0) return <p className="muted">No equity data.</p>;
  const g = lineGeometry(points, BOX);
  const ret = totalReturnPct(points);
  const desc = `${title}: equity from ${fmtNum(points[0]?.equity ?? 0)} to ${fmtNum(points[points.length - 1]?.equity ?? 0)} (${fmtPct(ret, 1, true)}) over ${points.length} points.`;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${BOX.width} ${BOX.height}`} role="img" aria-labelledby={`${id}-t`}>
        <title id={`${id}-t`}>{desc}</title>
        {g.yTicks.map((t) => (
          <g key={t.value}>
            <line className="grid" x1={BOX.left} x2={BOX.width - BOX.right} y1={t.y} y2={t.y} />
            <text className="tick" x={BOX.left - 6} y={t.y + 4} textAnchor="end">
              {t.label}
            </text>
          </g>
        ))}
        {g.xTicks.map((t, i) => (
          <text
            key={t.t}
            className="tick"
            x={t.x}
            y={BOX.height - 6}
            textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}
          >
            {t.label}
          </text>
        ))}
        <path className="area" d={g.area} />
        <path className="line" d={g.d} />
        {g.last && <circle className="dot" cx={g.last.x} cy={g.last.y} r={3.5} />}
      </svg>
      {g.xContext && <p className="muted small axiscap">{g.xContext}</p>}
      <figcaption className="sr">{desc}</figcaption>
    </figure>
  );
}
