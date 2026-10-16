// Plain-language labels for the per-party views: the provider's status for a signal, the follower's outcome.

import type { FollowerOutcome, FollowerSignal, SignalStatus } from "../api/types";
import { fmtDuration } from "./format";

/** The provider's one status for a signal, as shown on the Provider page. */
export function signalStatusLabel(status: SignalStatus): string {
  switch (status) {
    case "out":
      return "Out";
    case "waiting_for_time_stamp":
      return "Waiting for time-stamp";
  }
}

/** The follower's result for a signal: executed, or rejected with the recorded reason. */
export function outcomeLabel(outcome: FollowerOutcome, reason: string): string {
  const head =
    outcome === "executed" ? "Executed" : outcome === "rejected" ? "Rejected" : "Pending";
  return reason ? `${head}: ${reason}` : head;
}

/** "Tier 2, delay 30s": the follower's own tier and the delay it schedules (null tier: unknown). */
export function tierDelayLabel(s: Pick<FollowerSignal, "tier" | "delay_ms">): string {
  if (s.tier === null || s.delay_ms === null) return "n/a";
  return `Tier ${s.tier + 1}, ${s.delay_ms === 0 ? "no delay" : `delay ${fmtDuration(s.delay_ms)}`}`;
}
