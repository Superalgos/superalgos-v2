import type { Signal } from "../src/api/types";

/** Deterministic lowercase hex of `len` characters derived from `seed`. */
export function hex(seed: string, len: number): string {
  let h = 2166136261;
  let out = "";
  let i = 0;
  while (out.length < len) {
    h = Math.imul(h ^ (seed.charCodeAt(i % seed.length) + i), 16777619) >>> 0;
    out += h.toString(16).padStart(8, "0");
    i++;
  }
  return out.slice(0, len);
}

export const CSV_HEADER = [
  "seq",
  "id",
  "created_ms",
  "created_iso",
  "anchored_ms",
  "instrument",
  "side",
  "action",
  "size_fraction",
  "private",
] as const;

/** Signals as CSV. */
export function signalsCsv(signals: Signal[]): string {
  const rows = signals.map((s) =>
    [
      s.seq,
      s.id,
      s.created_ms,
      new Date(s.created_ms).toISOString(),
      s.anchored_ms ?? "",
      s.instrument,
      s.side,
      s.action,
      s.size_fraction,
      s.private,
    ].join(","),
  );
  return `${[CSV_HEADER.join(","), ...rows].join("\n")}\n`;
}

/**
 * Shape of sa_core's Bundle (v, provider, events, reveals, proofs, exported_at). The ids and
 * signatures are placeholders derived from the fixture: only the real API's export passes `sa verify`.
 */
export function signalsBundle(pubkey: string, signals: Signal[], exportedAtMs: number) {
  const provider = hex(pubkey, 64);
  return {
    v: 1,
    provider,
    events: signals.map((s) => ({
      id: hex(s.id, 64),
      pubkey: provider,
      created_at: Math.floor(s.created_ms / 1000),
      kind: 9317,
      tags: [["seq", String(s.seq)]],
      content: JSON.stringify({
        instrument: s.instrument,
        side: s.side,
        action: s.action,
        size_fraction: s.size_fraction,
      }),
      sig: hex(`sig${s.id}`, 128),
    })),
    reveals: [],
    proofs: [],
    exported_at: Math.floor(exportedAtMs / 1000),
  };
}
