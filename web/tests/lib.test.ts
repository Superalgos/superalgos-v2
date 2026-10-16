import { describe, expect, test } from "bun:test";
import { axisLabels, formatTickLabels, lineGeometry, niceTicks, totalReturnPct } from "../src/lib/chart";
import * as F from "../src/lib/format";
import { isOpen, tallyRows } from "../src/lib/governance";
import { allocationError, bpsToPct, splitPower, tierFor } from "../src/lib/power";
import { backtestHref, parseHash, providerHref } from "../src/lib/route";
import type { DevSignal } from "../src/api/types";
import {
  assignLanes,
  buildRows,
  describeTimeScale,
  resolveTimeScale,
  tierLabel,
  tierSchedule,
  trackSpanMs,
  trackTicks,
} from "../src/lib/timeline";
import {
  anchorPenaltyNote,
  badgesFor,
  signalCount,
  signalsFromSummary,
  sortProviders,
  verificationFromSummary,
} from "../src/lib/verification";
import { TIME_SCALE, devSignalsFor, me, providers } from "../mock/fixture";

const [A, B, C] = providers;
if (!A || !B || !C) throw new Error("fixture needs 3 providers");

describe("format", () => {
  test("percent and fractions", () => {
    expect(F.fmtPct(1.234, 1, true)).toBe("+1.2%");
    expect(F.fmtPct(-1.234, 2)).toBe("-1.23%");
    expect(F.fmtPct(Number.NaN)).toBe("n/a");
    expect(F.fmtFraction(0.615)).toBe("62%");
  });
  test("numbers", () => {
    expect(F.fmtNum(12345.678, 1)).toBe("12,345.7");
    expect(F.fmtNum(Number.POSITIVE_INFINITY)).toBe("n/a");
    expect(F.fmtCompact(1_500)).toBe("1.5k");
    expect(F.fmtCompact(2_500_000)).toBe("2.5M");
    expect(F.fmtCompact(3e9)).toBe("3.0B");
    expect(F.fmtCompact(42)).toBe("42");
    expect(F.fmtCompact(Number.NaN)).toBe("n/a");
  });
  test("keys and time", () => {
    expect(F.shortKey("abcdefghijklmnopqrstuvwxyz", 4, 3)).toBe("abcd…xyz");
    expect(F.shortKey("abc")).toBe("abc");
    expect(F.fmtTime(0)).toBe("1970-01-01 00:00:00 (UTC)");
    expect(F.fmtTime(Number.NaN)).toBe("n/a");
  });
  test("durations", () => {
    expect(F.fmtDuration(30_000)).toBe("30s");
    expect(F.fmtDuration(90_000)).toBe("1m 30s");
    expect(F.fmtDuration(120_000)).toBe("2m");
    expect(F.fmtDuration(3_900_000)).toBe("1h 5m");
    expect(F.fmtDuration(7_200_000)).toBe("2h");
    expect(F.fmtDuration(3 * 86_400_000)).toBe("3d");
    expect(F.fmtDuration(-30_000)).toBe("-30s");
    expect(F.fmtDuration(Number.NaN)).toBe("n/a");
    expect(F.fmtTimeframe(3600)).toBe("1h");
  });
  test("signClass", () => {
    expect([F.signClass(1), F.signClass(-1), F.signClass(0)]).toEqual(["pos", "neg", "flat"]);
  });
});

describe("chart", () => {
  const box = { width: 100, height: 50, left: 10, right: 10, top: 5, bottom: 5 };
  test("formatTickLabels: distinct and readable for tiny, normal and huge ranges", () => {
    const distinct = (l: string[]) => expect(new Set(l).size).toBe(l.length);
    const tiny = niceTicks(9999.9996, 10000.0004, 4);
    const tl = formatTickLabels(tiny);
    distinct(tl);
    expect(tl[0]).toMatch(/^(9,999\.99|10,000\.00)/);
    const tinier = formatTickLabels(niceTicks(10000, 10000.00002, 4));
    distinct(tinier);
    expect(formatTickLabels([0, 20, 40, 60, 80, 100])).toEqual(["0", "20", "40", "60", "80", "100"]);
    expect(formatTickLabels([9500, 10000, 10500])).toEqual(["9,500", "10,000", "10,500"]);
    expect(formatTickLabels([0, 0.5, 1])).toEqual(["0.0", "0.5", "1.0"]);
    const huge = formatTickLabels(niceTicks(0, 3e12, 4));
    distinct(huge);
    expect(huge.every((l) => !l.includes("."))).toBe(true);
    expect(formatTickLabels([5])).toEqual(["5"]);
    expect(formatTickLabels([])).toEqual([]);
  });

  test("lineGeometry y labels never repeat when equity barely moves", () => {
    const pts = [0, 1, 2, 3].map((i) => ({ t: 1_700_000_000_000 + i * 60_000, equity: 10000 + i * 0.0003 }));
    const labels = lineGeometry(pts, { width: 520, height: 220, left: 54, right: 12, top: 10, bottom: 24 }).yTicks.map((t) => t.label);
    expect(labels.length).toBeGreaterThan(1);
    expect(new Set(labels).size).toBe(labels.length);
  });

  test("niceTicks", () => {
    expect(niceTicks(0, 100, 4)).toEqual([0, 20, 40, 60, 80, 100]);
    expect(niceTicks(5, 5)).toEqual([5]);
    expect(niceTicks(Number.NaN, 1)).toEqual([]);
  });
  test("axis labels adapt to the span", () => {
    const t0 = Date.UTC(2026, 9, 13, 9, 0, 0);
    const three = (span: number) => [t0, t0 + span / 2, t0 + span];
    // minutes inside one day: seconds under 10 min, HH:MM above; date moves to the caption
    const sec = axisLabels(three(5 * 60_000));
    expect(sec.labels).toEqual(["09:00:00", "09:02:30", "09:05:00"]);
    expect(sec.context).toBe("2026-10-13 (UTC)");
    expect(axisLabels(three(30 * 60_000)).labels).toEqual(["09:00", "09:15", "09:30"]);
    // crossing midnight or 1-3 days: date + time
    expect(axisLabels(three(36 * 3_600_000)).labels).toEqual(["10-13 09:00", "10-14 03:00", "10-14 21:00"]);
    expect(axisLabels(three(36 * 3_600_000)).context).toBe("times in UTC");
    // multi-day: dates, all distinct
    const days = axisLabels(three(10 * 86_400_000));
    expect(days.labels).toEqual(["2026-10-13", "2026-10-18", "2026-10-23"]);
    expect(new Set(days.labels).size).toBe(3);
  });
  test("axis labels never repeat for distinct ticks", () => {
    const t0 = Date.UTC(2026, 9, 13, 9, 0, 0);
    // 90 s: HH:MM would repeat, so seconds are used
    const l = axisLabels([t0, t0 + 45_000, t0 + 90_000]).labels;
    expect(new Set(l).size).toBe(3);
    expect(axisLabels([t0, t0, t0]).labels.length).toBe(3);
    expect(axisLabels([])).toEqual({ labels: [], context: null });
  });
  test("geometry ticks carry adaptive labels", () => {
    const t0 = Date.UTC(2026, 9, 13, 9, 0, 0);
    const g = lineGeometry(
      [
        { t: t0, equity: 100 },
        { t: t0 + 1_800_000, equity: 110 },
      ],
      box,
    );
    expect(g.xTicks.map((t) => t.label)).toEqual(["09:00", "09:15", "09:30"]);
    expect(g.xContext).toBe("2026-10-13 (UTC)");
  });
  test("empty geometry", () => {
    expect(lineGeometry([], box).last).toBeNull();
  });
  test("geometry maps endpoints inside the box", () => {
    const g = lineGeometry(
      [
        { t: 2, equity: 20 },
        { t: 1, equity: 10 },
      ],
      box,
    );
    expect(g.d.startsWith("M10.0")).toBe(true);
    expect(g.last?.x).toBe(90);
    expect(g.xTicks).toHaveLength(3);
    expect(g.area.endsWith("Z")).toBe(true);
    expect(g.last?.y).toBeLessThan(45);
  });
  test("flat and single-point series do not divide by zero", () => {
    const g = lineGeometry([{ t: 5, equity: 7 }], box);
    expect(g.d).not.toContain("NaN");
    expect(g.last?.x).toBe(50);
  });
  test("totalReturnPct", () => {
    expect(totalReturnPct([{ t: 0, equity: 100 }, { t: 1, equity: 110 }])).toBeCloseTo(10);
    expect(totalReturnPct([])).toBe(0);
    expect(totalReturnPct([{ t: 0, equity: 0 }, { t: 1, equity: 5 }])).toBe(0);
  });
});

describe("power", () => {
  test("splitPower covers 100% and names providers", () => {
    const segs = splitPower(me, providers);
    expect(segs.reduce((s, x) => s + x.bps, 0)).toBe(10_000);
    expect(segs[0]?.kind).toBe("governance");
    expect(segs[1]?.label).toBe(A.name);
    expect(segs.at(-1)?.kind).toBe("unallocated");
    expect(segs.reduce((s, x) => s + x.power, 0)).toBeCloseTo(me.token_power);
  });
  test("fully allocated has no unallocated segment; unknown provider falls back to key", () => {
    const full = { ...me, allocations: { governance_bps: 0, providers: { abcdefghijkl: 10_000, zero: 0 } } };
    const segs = splitPower(full, providers);
    expect(segs).toHaveLength(1);
    expect(segs[0]?.label).toBe("abcdefgh…");
  });
  test("allocationError", () => {
    expect(allocationError(me)).toBeNull();
    const over = { ...me, allocations: { governance_bps: 9000, providers: { x: 2000 } } };
    expect(allocationError(over)).toContain("11000");
  });
  test("tierFor picks highest satisfied tier", () => {
    expect(tierFor(A, 12_000)).toBe(0);
    expect(tierFor(A, 1_500)).toBe(1);
    expect(tierFor(A, 500)).toBe(2);
    expect(tierFor(A, 0)).toBe(3);
    expect(tierFor(C, 100)).toBeNull();
  });
  test("bpsToPct", () => {
    expect(bpsToPct(3000)).toBe("30%");
    expect(bpsToPct(1250)).toBe("12.5%");
  });
});

describe("timeline", () => {
  test("labels and schedule", () => {
    expect(tierLabel(A, 3)).toBe("public");
    expect(tierLabel(A, 0)).toContain("10,000");
    expect(tierLabel(A, 9)).toBe("tier 9");
    const sched = tierSchedule(A);
    expect(sched.map((s) => s.delayMs)).toEqual([0, 360_000, 1_800_000, 7_200_000]);
  });
  test("one marker per tier at its scheduled offset, newest row first", () => {
    const signals = devSignalsFor(A.pubkey, 5) ?? [];
    const rows = buildRows(A, signals, TIME_SCALE);
    expect(rows[0]?.signal.seq).toBe(5);
    const m = rows[0]?.markers ?? [];
    expect(m.map((x) => x.tier)).toEqual([0, 1, 2, 3]);
    expect(m.map((x) => x.scheduledMs)).toEqual([0, 360_000, 1_800_000, 7_200_000]);
    // span = largest delay (2 timeframes), so the 2x tier sits at the right edge and the rest are spread
    expect(m.map((x) => x.position)).toEqual([0, 0.05, 0.25, 1]);
    expect(m.every((x) => x.position >= 0 && x.position <= 1)).toBe(true);
    expect(rows[0]?.anchorLagMs === null || (rows[0]?.anchorLagMs ?? 0) > 0).toBe(true);
  });
  test("replay scaling does not collapse the markers: wall delays are tiny, positions are not", () => {
    const sig = (devSignalsFor(A.pubkey, 1) ?? [])[0] as DevSignal;
    const m = buildRows(A, [sig], TIME_SCALE)[0]?.markers ?? [];
    expect(m[3]?.wallDelayMs).toBeLessThan(130_000);
    expect(m[3]?.position).toBe(1);
    expect(m[2]?.position).toBe(0.25);
  });
  test("a tier absent from release[] is still drawn, from the schedule", () => {
    const sig = (devSignalsFor(A.pubkey, 1) ?? [])[0] as DevSignal;
    const partial = { ...sig, created_ms: 1_000, release: [{ tier: 3, at_ms: 1_000 + 120_000, observed: false }] };
    const m = buildRows(A, [partial], 1 / 60)[0]?.markers ?? [];
    expect(m.map((x) => x.tier)).toEqual([0, 1, 2, 3]);
    expect(m[1]?.atMs).toBe(1_000 + 6_000);
    expect(m[1]?.observed).toBe(false);
  });
  test("observed receipts are flagged and get their own position only when visibly apart", () => {
    const sig = (devSignalsFor(A.pubkey, 1) ?? [])[0] as DevSignal;
    const late = {
      ...sig,
      created_ms: 0,
      release: [
        { tier: 0, at_ms: 0, observed: true },
        { tier: 1, at_ms: 12_000, observed: true }, // 12 s wall = 720 s market vs 360 s scheduled
        { tier: 2, at_ms: 30_000, observed: false },
        { tier: 3, at_ms: 120_000, observed: false },
      ],
    };
    const m = buildRows(A, [late], 1 / 60)[0]?.markers ?? [];
    expect(m[0]?.observed).toBe(true);
    expect(m[0]?.receivedPosition).toBeNull(); // on schedule
    expect(m[1]?.receivedPosition).toBeCloseTo(0.1, 6);
    expect(m[1]?.position).toBeCloseTo(0.05, 6);
    expect(m[2]?.receivedPosition).toBeNull();
  });
  test("late release clamps to 1 and unanchored lag is null", () => {
    const sig = (devSignalsFor(A.pubkey, 1) ?? [])[0] as DevSignal;
    const s = { ...sig, anchored_ms: null, created_ms: 0, release: [{ tier: 9, at_ms: 99_999_999, observed: false }] };
    const row = buildRows(A, [s], 1)[0];
    expect(row?.markers.find((x) => x.tier === 9)?.position).toBe(1);
    expect(row?.anchorLagMs).toBeNull();
  });
  test("track spans at least the largest delay", () => {
    expect(trackSpanMs(A)).toBe(7_200_000);
    expect(trackSpanMs(B)).toBe(14_400_000);
    expect(trackTicks(A).map((t) => t.position)).toEqual([0, 0.5, 1]);
    expect(trackTicks(B).map((t) => t.position)).toEqual([0, 1]);
  });
  test("assignLanes stacks markers closer than the gap", () => {
    expect(assignLanes([0, 0, 0.03, 0.5, 0.52], 0.07)).toEqual([0, 1, 2, 0, 1]);
    expect(assignLanes([0, 0.1, 0.2], 0.07)).toEqual([0, 0, 0]);
    expect(assignLanes([], 0.07)).toEqual([]);
  });
  test("coinciding tiers get distinct lanes", () => {
    const sig = (devSignalsFor(A.pubkey, 1) ?? [])[0] as DevSignal;
    const same = { ...sig, created_ms: 0, release: [] };
    const flat = { ...A, tiers: A.tiers.map((t) => ({ ...t, delay_fraction: 0 })) };
    const row = buildRows(flat, [same], 1)[0];
    expect(row?.markers.map((m) => m.lane)).toEqual([0, 1, 2, 3]);
    expect(row?.lanes).toBe(4);
  });
  test("time scale: declared wins, else inferred from computed releases, else 1", () => {
    const sigs = devSignalsFor(A.pubkey, 4) ?? [];
    expect(resolveTimeScale(A, sigs, 0.5)).toBe(0.5);
    expect(resolveTimeScale(A, sigs, undefined)).toBeCloseTo(TIME_SCALE, 3);
    expect(resolveTimeScale(A, [], null)).toBe(1);
    expect(resolveTimeScale(A, sigs, Number.NaN)).toBeCloseTo(TIME_SCALE, 3);
  });
  test("describeTimeScale", () => {
    expect(describeTimeScale(1)).toBeNull();
    expect(describeTimeScale(undefined)).toBeNull();
    expect(describeTimeScale(0)).toBeNull();
    expect(describeTimeScale(1 / 1440)).toBe("1,440x faster than real time");
    expect(describeTimeScale(0.25)).toBe("4.00x faster than real time");
    expect(describeTimeScale(2)).toBe("2.00x slower than real time");
  });
});

describe("governance", () => {
  test("tallyRows shares and leader", () => {
    const rows = tallyRows({ options: ["Yes", "No", "Abstain"], tally: { Yes: 75, No: 25 } });
    expect(rows.map((r) => r.share)).toEqual([0.75, 0.25, 0]);
    expect(rows.filter((r) => r.leading).map((r) => r.option)).toEqual(["Yes"]);
  });
  test("empty tally has no leader; unknown options included", () => {
    expect(tallyRows({ options: ["A"], tally: {} })[0]?.leading).toBe(false);
    expect(tallyRows({ options: ["A"], tally: { Z: 1 } }).map((r) => r.option)).toEqual(["A", "Z"]);
  });
  test("isOpen", () => {
    expect(isOpen({ deadline: "2030-01-01T00:00:00Z" }, Date.UTC(2026, 0, 1))).toBe(true);
    expect(isOpen({ deadline: "2020-01-01T00:00:00Z" }, Date.UTC(2026, 0, 1))).toBe(false);
  });
});

describe("route", () => {
  test("parseHash", () => {
    expect(parseHash("")).toEqual({ page: "providers" });
    expect(parseHash("#/")).toEqual({ page: "providers" });
    expect(parseHash("#/providers/npub%201")).toEqual({ page: "provider", pubkey: "npub 1" });
    expect(parseHash("#/power")).toEqual({ page: "power" });
    expect(parseHash("#/backtests")).toEqual({ page: "backtests" });
    expect(parseHash("#/backtests/x")).toEqual({ page: "backtests", id: "x" });
    expect(parseHash("#/governance")).toEqual({ page: "governance" });
    expect(parseHash("#/network")).toEqual({ page: "network" });
    expect(parseHash("#/nope")).toEqual({ page: "providers" });
  });
  test("hrefs round-trip", () => {
    expect(parseHash(providerHref("a/b"))).toEqual({ page: "provider", pubkey: "a/b" });
    expect(parseHash(backtestHref("a b"))).toEqual({ page: "backtests", id: "a b" });
  });
});

describe("verification", () => {
  test("badge states", () => {
    const ok = badgesFor({ signatures_ok: true, chain_complete: true, anchored_pct: 100 });
    expect(ok.every((b) => b.state === "ok")).toBe(true);
    const mixed = badgesFor({ signatures_ok: false, chain_complete: false, anchored_pct: 72 });
    expect(mixed.map((b) => b.state)).toEqual(["bad", "bad", "warn"]);
    expect(mixed[2]?.label).toBe("Anchored 72%");
    expect(badgesFor({ signatures_ok: true, chain_complete: true, anchored_pct: 10 })[2]?.state).toBe("bad");
  });
  test("no signals yet is neutral, not a red Anchored 0%", () => {
    const v = { signatures_ok: true, chain_complete: true, anchored_pct: 0 };
    const none = badgesFor(v, 0);
    expect(none[2]?.state).toBe("neutral");
    expect(none[2]?.label).toBe("No signals yet");
    expect(none.some((b) => b.state === "bad")).toBe(false);
    // signals exist and none anchored: red; unknown count keeps the old behaviour
    expect(badgesFor(v, 3)[2]?.state).toBe("bad");
    expect(badgesFor(v)[2]?.state).toBe("bad");
    expect(badgesFor(v, 3)[2]?.label).toBe("Anchored 0%");
  });
  test("signal count from the loaded list or the summary", () => {
    const fresh = providers.find((p) => p.name === "Fresh Start")?.record_summary as never;
    expect(signalsFromSummary(fresh)).toBe(0);
    expect(signalsFromSummary(A.record_summary)).toBeNull();
    expect(signalCount(5, A.record_summary)).toBe(5);
    expect(signalCount(0, fresh)).toBe(0);
    expect(signalCount(null, fresh)).toBe(0);
    expect(signalCount(0, A.record_summary)).toBeNull();
  });
  test("late and missing anchors are shown as misses, never hidden", () => {
    expect(anchorPenaltyNote(A.record_summary)).toBeNull();
    expect(anchorPenaltyNote(B.record_summary)).toBe(
      "4 never anchored and 12 anchored late: scored as misses (a loss in win rate, no gain in PnL), not left out.",
    );
    const onlyLate = { ...A.record_summary, late_anchor: 1 };
    expect(anchorPenaltyNote(onlyLate)).toContain("1 anchored late");
    expect(anchorPenaltyNote(onlyLate)).not.toContain("never anchored");
    const onlyMissing = { ...A.record_summary, unanchored: 2 };
    expect(anchorPenaltyNote(onlyMissing)).toContain("2 never anchored");
    expect(B.record_summary.excluded).toBe(3);
  });
  test("verificationFromSummary", () => {
    expect(verificationFromSummary(B.record_summary)).toEqual({
      signatures_ok: true,
      chain_complete: false,
      anchored_pct: 72,
    });
  });
  test("sortProviders directions", () => {
    expect(sortProviders(providers, "pnl_pct").map((p) => p.name)[0]).toBe(A.name);
    expect(sortProviders(providers, "pnl_pct", false)[0]?.name).toBe(C.name);
    expect(sortProviders(providers, "max_dd_pct")[0]?.name).toBe(B.name); // lowest drawdown first
    expect(sortProviders(providers, "max_dd_pct", false)[0]?.name).toBe(C.name);
    expect(sortProviders(providers, "followers")[0]?.name).toBe(A.name);
    expect(sortProviders(providers, "power_backing")[0]?.name).toBe(A.name);
    expect(sortProviders(providers, "trades")[0]?.name).toBe(A.name);
  });
});
