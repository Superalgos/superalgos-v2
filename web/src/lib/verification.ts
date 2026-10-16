import type { RecordSummary, Verification } from "../api/types";

export type BadgeState = "ok" | "warn" | "bad" | "neutral";

export interface Badge {
  key: "signatures" | "chain" | "anchored";
  label: string;
  state: BadgeState;
  detail: string;
}

/**
 * `signals` is how many signals the provider has published when known (null: unknown). With none,
 * "anchored 0%" is not a failure, so that badge goes neutral; red only when signals exist and
 * too few are anchored.
 */
export function badgesFor(v: Verification, signals: number | null = null): Badge[] {
  const none = signals === 0;
  return [
    {
      key: "signatures",
      label: "Signatures",
      state: v.signatures_ok ? "ok" : "bad",
      detail: v.signatures_ok ? "all signatures verify" : "a signature failed verification",
    },
    {
      key: "chain",
      label: "Chain",
      state: v.chain_complete ? "ok" : "bad",
      detail: v.chain_complete ? "no gaps in seq/prev chain" : "gaps in the signal chain",
    },
    none
      ? {
          key: "anchored",
          label: "No signals yet",
          state: "neutral",
          detail: "nothing to anchor until the provider publishes a signal",
        }
      : {
          key: "anchored",
          label: `Anchored ${Math.round(v.anchored_pct)}%`,
          state: v.anchored_pct >= 99.5 ? "ok" : v.anchored_pct >= 50 ? "warn" : "bad",
          detail: `${v.anchored_pct.toFixed(1)}% of events carry a time anchor`,
        },
  ];
}

/**
 * Leaderboard rows carry no signal count; a summary with no trades and no anchors is a provider
 * that has not published anything yet. Returns 0 for that, else null (unknown).
 */
export function signalsFromSummary(s: RecordSummary): number | null {
  return s.trades === 0 && s.anchor_coverage === 0 ? 0 : null;
}

/** Signal count for the badges: the loaded list when it has entries, else what the summary implies. */
export function signalCount(loaded: number | null, s: RecordSummary): number | null {
  if (loaded !== null && loaded > 0) return loaded;
  return signalsFromSummary(s);
}

/** Leaderboard rows only carry a summary: derive the badge inputs from it. */
export function verificationFromSummary(s: RecordSummary): Verification {
  return {
    signatures_ok: s.signatures_ok,
    chain_complete: s.completeness >= 1,
    anchored_pct: s.anchor_coverage * 100,
  };
}

/**
 * Signals that were published but not anchored on time are scored as misses, never hidden. Returns the
 * sentence the provider page shows when there are any (null when there are none).
 */
export function anchorPenaltyNote(s: RecordSummary): string | null {
  const bad = s.late_anchor + s.unanchored;
  if (bad === 0) return null;
  const parts = [
    s.unanchored > 0 ? `${s.unanchored} never anchored` : null,
    s.late_anchor > 0 ? `${s.late_anchor} anchored late` : null,
  ].filter((x): x is string => x !== null);
  return `${parts.join(" and ")}: scored as misses (a loss in win rate, no gain in PnL), not left out.`;
}

export type SortKey =
  | "pnl_pct"
  | "win_rate"
  | "max_dd_pct"
  | "trades"
  | "followers"
  | "power_backing";

export function sortProviders<
  T extends { followers: number; power_backing: number; record_summary: RecordSummary },
>(rows: T[], key: SortKey, desc = true): T[] {
  const val = (r: T): number =>
    key === "followers"
      ? r.followers
      : key === "power_backing"
        ? r.power_backing
        : r.record_summary[key];
  // max drawdown: lower is better, so "desc" (best first) means ascending
  const dir = key === "max_dd_pct" ? (desc ? 1 : -1) : desc ? -1 : 1;
  // providers with no trades have nothing to rank: they go last in either direction
  const empty = (r: T) => (r.record_summary.trades === 0 ? 1 : 0);
  return [...rows].sort((a, b) => empty(a) - empty(b) || dir * (val(a) - val(b)));
}
