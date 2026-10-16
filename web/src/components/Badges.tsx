import type { Verification } from "../api/types";
import { badgesFor } from "../lib/verification";

const ICON = { ok: "✓", warn: "!", bad: "✕", neutral: "–" } as const;

export function Badges({ v, signals = null }: { v: Verification; signals?: number | null }) {
  return (
    <ul className="badges" aria-label="Verification">
      {badgesFor(v, signals).map((b) => (
        <li key={b.key} className={`badge ${b.state}`} title={b.detail}>
          <span aria-hidden="true">{ICON[b.state]}</span> {b.label}
          <span className="sr"> — {b.detail}</span>
        </li>
      ))}
    </ul>
  );
}
