import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import type { FollowerOutcome, FollowerSignal, SignalStatus } from "../src/api/types";
import { outcomeLabel, signalStatusLabel, tierDelayLabel } from "../src/lib/signals";

describe("provider status labels", () => {
  test("out, or the step that is missing", () => {
    expect(signalStatusLabel("out")).toBe("Out");
    expect(signalStatusLabel("waiting_for_time_stamp")).toBe("Waiting for time-stamp");
  });
  test("every status has its own non-empty label", () => {
    const all: SignalStatus[] = ["out", "waiting_for_time_stamp"];
    fc.assert(
      fc.property(fc.constantFrom(...all), fc.constantFrom(...all), (a, b) => {
        expect(signalStatusLabel(a).length).toBeGreaterThan(0);
        expect(signalStatusLabel(a) === signalStatusLabel(b)).toBe(a === b);
      }),
    );
  });
});

describe("follower outcome labels", () => {
  test("executed and rejected carry the reason", () => {
    expect(outcomeLabel("executed", "working order")).toBe("Executed: working order");
    expect(outcomeLabel("rejected", "gap: seq 6 does not follow 4")).toBe(
      "Rejected: gap: seq 6 does not follow 4",
    );
    expect(outcomeLabel("pending", "")).toBe("Pending");
  });
  test("the reason is always shown, and the label starts with the outcome", () => {
    const outcomes: FollowerOutcome[] = ["executed", "rejected", "pending"];
    fc.assert(
      fc.property(fc.constantFrom(...outcomes), fc.string({ minLength: 1 }), (o, reason) => {
        const label = outcomeLabel(o, reason);
        expect(label.endsWith(reason)).toBe(true);
        expect(label.toLowerCase().startsWith(o === "pending" ? "pending" : o)).toBe(true);
      }),
    );
  });
});

describe("tier delay label", () => {
  const row = (tier: number | null, delay_ms: number | null): Pick<FollowerSignal, "tier" | "delay_ms"> => ({
    tier,
    delay_ms,
  });
  test("names the follower's own tier (1-based) and its delay", () => {
    expect(tierDelayLabel(row(1, 30_000))).toBe("Tier 2, delay 30s");
    expect(tierDelayLabel(row(0, 0))).toBe("Tier 1, no delay");
    expect(tierDelayLabel(row(null, null))).toBe("n/a");
    expect(tierDelayLabel(row(2, null))).toBe("n/a");
  });
});
