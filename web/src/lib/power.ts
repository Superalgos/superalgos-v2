import type { Me, Provider } from "../api/types";

export interface Segment {
  key: string;
  label: string;
  bps: number;
  power: number;
  kind: "governance" | "provider" | "unallocated";
}

export const FULL_BPS = 10_000;

/** Split token power into governance, per-provider and unallocated segments. */
export function splitPower(me: Me, providers: Provider[]): Segment[] {
  const names = new Map(providers.map((p) => [p.pubkey, p.name]));
  const power = (bps: number) => (me.token_power * bps) / FULL_BPS;
  const segs: Segment[] = [];
  const gov = me.allocations.governance_bps;
  if (gov > 0) {
    segs.push({
      key: "governance",
      label: "Governance",
      bps: gov,
      power: power(gov),
      kind: "governance",
    });
  }
  const entries = Object.entries(me.allocations.providers)
    .filter(([, bps]) => bps > 0)
    .sort((a, b) => b[1] - a[1]);
  for (const [pk, bps] of entries) {
    segs.push({
      key: pk,
      label: names.get(pk) ?? `${pk.slice(0, 8)}…`,
      bps,
      power: power(bps),
      kind: "provider",
    });
  }
  const used = segs.reduce((s, x) => s + x.bps, 0);
  if (used < FULL_BPS) {
    const rest = FULL_BPS - used;
    segs.push({
      key: "unallocated",
      label: "Unallocated",
      bps: rest,
      power: power(rest),
      kind: "unallocated",
    });
  }
  return segs;
}

/** Returns an error message when the allocation exceeds 100%, else null. */
export function allocationError(me: Me): string | null {
  const total =
    me.allocations.governance_bps +
    Object.values(me.allocations.providers).reduce((s, b) => s + b, 0);
  if (total > FULL_BPS) {
    return `Allocation totals ${total} bps, above the ${FULL_BPS} bps cap. The relay will reject this allocation; re-sign a split that sums to at most ${FULL_BPS}.`;
  }
  return null;
}

/** Highest tier index whose min_power is within allocated power, or null when none applies. */
export function tierFor(provider: Provider, allocatedPower: number): number | null {
  let best: number | null = null;
  let bestMin = -1;
  provider.tiers.forEach((t, i) => {
    if (t.min_power <= allocatedPower && t.min_power > bestMin) {
      best = i;
      bestMin = t.min_power;
    }
  });
  return best;
}

export function bpsToPct(bps: number): string {
  return `${(bps / 100).toFixed(bps % 100 === 0 ? 0 : 1)}%`;
}
