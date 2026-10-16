import { fmtNum } from "../lib/format";
import type { Segment } from "../lib/power";
import { bpsToPct } from "../lib/power";

export function PowerBar({ segments }: { segments: Segment[] }) {
  let provider = 0;
  const cls = (s: Segment) => (s.kind === "provider" ? `seg p${provider++ % 4}` : `seg ${s.kind}`);
  const classes = segments.map(cls);
  return (
    <div className="powerbar-wrap">
      <div
        className="powerbar"
        role="img"
        aria-label={segments.map((s) => `${s.label} ${bpsToPct(s.bps)}`).join(", ")}
      >
        {segments.map((s, i) => (
          <div
            key={s.key}
            className={classes[i]}
            style={{ flexGrow: s.bps }}
            title={`${s.label}: ${bpsToPct(s.bps)}`}
          >
            {s.bps >= 700 && <span>{bpsToPct(s.bps)}</span>}
          </div>
        ))}
      </div>
      <ul className="legend">
        {segments.map((s, i) => (
          <li key={s.key}>
            <span className={`swatch ${classes[i]}`} aria-hidden="true" />
            <span>{s.label}</span>
            <span className="num">
              {bpsToPct(s.bps)} · {fmtNum(s.power)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
