import type { DevSignal, Provider } from "../api/types";

export interface TierMarker {
  tier: number;
  label: string;
  /** Wall-clock release time: the observed receipt, or the schedule (created + delay x time scale). */
  atMs: number;
  /** Scheduled delay in market time: delay_fraction x timeframe. Drives the marker position. */
  scheduledMs: number;
  /** at_ms - created_ms, wall clock (shrunk by the time scale in a replay). */
  wallDelayMs: number;
  /** 0..1 along the track. */
  position: number;
  /** True when atMs is a real follower receipt time; false when it is computed from the schedule. */
  observed: boolean;
  /** Where the real receipt falls on the track (market-time equivalent), when it differs visibly from the schedule. */
  receivedPosition: number | null;
  /** Vertical lane so coinciding markers stay legible (0 = baseline). */
  lane: number;
}

export interface SignalRow {
  signal: DevSignal;
  markers: TierMarker[];
  lanes: number;
  anchorLagMs: number | null;
}

export function tierLabel(provider: Pick<Provider, "tiers" | "mode">, tier: number): string {
  const t = provider.tiers[tier];
  if (!t) return `tier ${tier}`;
  return t.min_power > 0 ? `≥ ${t.min_power.toLocaleString("en-US")} power` : "public";
}

/** Scheduled delay of every tier in market time (delay_fraction x timeframe), in tier order. */
export function scheduledDelays(provider: Pick<Provider, "tiers" | "timeframe_s">): number[] {
  return provider.tiers.map((t) => t.delay_fraction * provider.timeframe_s * 1000);
}

/** The track covers at least one timeframe and always the largest scheduled delay. */
export function trackSpanMs(provider: Pick<Provider, "tiers" | "timeframe_s">): number {
  return Math.max(provider.timeframe_s * 1000, ...scheduledDelays(provider), 1);
}

/** Time-scale readout such as "1,440x faster than real time"; null when running in real time. */
export function describeTimeScale(scale: number | null | undefined): string | null {
  if (scale === null || scale === undefined || !Number.isFinite(scale) || scale <= 0) return null;
  if (Math.abs(scale - 1) < 1e-9) return null;
  if (scale < 1) {
    const speed = 1 / scale;
    const shown = speed >= 10 ? Math.round(speed).toLocaleString("en-US") : speed.toFixed(2);
    return `${shown}x faster than real time`;
  }
  return `${scale.toFixed(2)}x slower than real time`;
}

/**
 * Time scale to use: the network's when it is a usable number, else inferred from the signals
 * (median wall-delay / scheduled-delay over computed, non-zero tier delays), else 1.
 */
export function resolveTimeScale(
  provider: Provider,
  signals: DevSignal[],
  declared: number | null | undefined,
): number {
  if (typeof declared === "number" && Number.isFinite(declared) && declared > 0) return declared;
  const sched = scheduledDelays(provider);
  const ratios: number[] = [];
  for (const s of signals) {
    for (const r of s.release) {
      const want = sched[r.tier];
      if (r.observed || !want) continue;
      ratios.push((r.at_ms - s.created_ms) / want);
    }
  }
  if (ratios.length === 0) return 1;
  ratios.sort((a, b) => a - b);
  const mid = ratios[Math.floor(ratios.length / 2)] ?? 1;
  return mid > 0 && Number.isFinite(mid) ? mid : 1;
}

/** Spread positions over vertical lanes: a marker joins the first lane whose last marker is >= gap away. */
export function assignLanes(positions: number[], gap: number): number[] {
  const order = positions.map((p, i) => ({ p, i })).sort((a, b) => a.p - b.p || a.i - b.i);
  const laneEnd: number[] = [];
  const out = new Array<number>(positions.length).fill(0);
  for (const { p, i } of order) {
    let lane = laneEnd.findIndex((end) => p - end >= gap - 1e-9);
    if (lane === -1) {
      lane = laneEnd.length;
      laneEnd.push(p);
    } else {
      laneEnd[lane] = p;
    }
    out[i] = lane;
  }
  return out;
}

const clamp01 = (n: number) => Math.min(Math.max(n, 0), 1);

/**
 * One marker per tier of the provider (and any extra tier a release names), at its scheduled offset.
 * A tier missing from `release[]` is still drawn, from the schedule. Receipt times that the API marks
 * `observed` get a separate position so they read as distinct from the schedule.
 */
export function buildRows(
  provider: Provider,
  signals: DevSignal[],
  timeScale = 1,
  gap = 0.07,
): SignalRow[] {
  const span = trackSpanMs(provider);
  const sched = scheduledDelays(provider);
  return [...signals]
    .sort((a, b) => b.seq - a.seq)
    .map((signal) => {
      const byTier = new Map(signal.release.map((r) => [r.tier, r]));
      const tiers = [
        ...new Set([...provider.tiers.map((_, i) => i), ...signal.release.map((r) => r.tier)]),
      ].sort((a, b) => a - b);
      const base = tiers.map((tier) => {
        const rel = byTier.get(tier);
        const scheduledMs = sched[tier] ?? (rel ? (rel.at_ms - signal.created_ms) / timeScale : 0);
        const atMs = rel ? rel.at_ms : signal.created_ms + Math.round(scheduledMs * timeScale);
        const wallDelayMs = atMs - signal.created_ms;
        const position = clamp01(scheduledMs / span);
        const observed = rel?.observed === true;
        const received = observed ? clamp01(wallDelayMs / timeScale / span) : null;
        return {
          tier,
          label: tierLabel(provider, tier),
          atMs,
          scheduledMs,
          wallDelayMs,
          position,
          observed,
          receivedPosition:
            received !== null && Math.abs(received - position) > 0.004 ? received : null,
        };
      });
      const lanes = assignLanes(
        base.map((m) => m.position),
        gap,
      );
      const markers = base.map((m, i) => ({ ...m, lane: lanes[i] ?? 0 }));
      return {
        signal,
        markers,
        lanes: Math.max(1, ...markers.map((m) => m.lane + 1)),
        anchorLagMs: signal.anchored_ms === null ? null : signal.anchored_ms - signal.created_ms,
      };
    });
}

/** Expected delay per tier as a human-checkable list, sorted by delay. */
export function tierSchedule(
  provider: Provider,
): { tier: number; label: string; delayMs: number }[] {
  return provider.tiers
    .map((t, tier) => ({
      tier,
      label: tierLabel(provider, tier),
      delayMs: t.delay_fraction * provider.timeframe_s * 1000,
    }))
    .sort((a, b) => a.delayMs - b.delayMs || a.tier - b.tier);
}

/** Axis ticks along the track: 0, one timeframe (when the track is longer), and the end. */
export function trackTicks(
  provider: Pick<Provider, "tiers" | "timeframe_s">,
): { position: number; ms: number }[] {
  const span = trackSpanMs(provider);
  const tf = provider.timeframe_s * 1000;
  const ms = tf < span ? [0, tf, span] : [0, span];
  return ms.map((m) => ({ ms: m, position: m / span }));
}
