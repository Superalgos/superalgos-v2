import type {
  Backtest,
  DevSignal,
  EquityPoint,
  Fill,
  FollowerSignal,
  Me,
  Network,
  Proposal,
  Provider,
  Signal,
  Timeframe,
  TrackRecord,
  Trade,
} from "../src/api/types";
import { candleAt, TF_MS } from "./market";

/** Deterministic PRNG (mulberry32) so fixtures are stable between runs and tests. */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const HOUR = 3_600_000;
/** A demo replay: the relay's delays run 60x faster than the schedule says. */
export const TIME_SCALE = 1 / 60;
export const T0 = Date.UTC(2026, 8, 1);
/** When the replayed signals start (the compressed-replay window used by Atlas and its signals). */
export const REPLAY_T0 = Date.UTC(2026, 9, 13, 9, 0);

export function walk(
  seed: number,
  n: number,
  drift: number,
  vol: number,
  step = HOUR * 6,
  start = T0,
): EquityPoint[] {
  const r = rng(seed);
  let eq = 10_000;
  const out: EquityPoint[] = [];
  for (let i = 0; i < n; i++) {
    eq *= 1 + drift + (r() - 0.5) * vol;
    out.push({ t: start + i * step, equity: Math.round(eq * 100) / 100 });
  }
  return out;
}

export function tradesFrom(seed: number, n: number): Trade[] {
  const r = rng(seed);
  const out: Trade[] = [];
  let t = T0;
  for (let i = 0; i < n; i++) {
    const side = r() > 0.5 ? "buy" : "sell";
    const entry = 60_000 + r() * 4_000;
    const pnl = (r() - 0.4) * 4;
    const exit = side === "buy" ? entry * (1 + pnl / 100) : entry * (1 - pnl / 100);
    const open_t = t + Math.round(r() * 8 * HOUR);
    const close_t = open_t + Math.round((1 + r() * 20) * HOUR);
    out.push({
      open_t,
      close_t,
      side,
      entry: Math.round(entry * 100) / 100,
      exit: Math.round(exit * 100) / 100,
      pnl_pct: Math.round(pnl * 100) / 100,
    });
    t = close_t;
  }
  return out;
}

const PK_A = "npub1atlasmomentum0000000000000000000000000000000000000000q4a";
const PK_B = "npub1quietreversion000000000000000000000000000000000000000q9b";
const PK_D = "npub1freshstart0000000000000000000000000000000000000000000q4d";
const PK_C = "npub1darkpoolfox00000000000000000000000000000000000000000000q2c";

export const providers: Provider[] = [
  {
    pubkey: PK_A,
    name: "Atlas Momentum",
    sa_address: "0x1111111111111111111111111111111111111111",
    mode: "public",
    timeframe_s: 3600,
    tiers: [
      { min_power: 10_000, delay_fraction: 0 },
      { min_power: 1_000, delay_fraction: 0.1 },
      { min_power: 100, delay_fraction: 0.5 },
      { min_power: 0, delay_fraction: 2 },
    ],
    followers: 42,
    power_backing: 184_000,
    record_summary: {
      pnl_pct: 18.4,
      win_rate: 0.61,
      max_dd_pct: 6.2,
      trades: 96,
      completeness: 1,
      anchor_coverage: 1,
      signatures_ok: true,
      excluded: 0,
      late_anchor: 0,
      unanchored: 0,
    },
  },
  {
    pubkey: PK_B,
    name: "Quiet Reversion",
    sa_address: "0x2222222222222222222222222222222222222222",
    mode: "public",
    timeframe_s: 14_400,
    tiers: [
      { min_power: 5_000, delay_fraction: 0 },
      { min_power: 0, delay_fraction: 0.4 },
    ],
    followers: 17,
    power_backing: 52_500,
    record_summary: {
      pnl_pct: 7.9,
      win_rate: 0.54,
      max_dd_pct: 4.1,
      trades: 58,
      completeness: 0.94,
      anchor_coverage: 0.72,
      signatures_ok: true,
      excluded: 3,
      late_anchor: 12,
      unanchored: 4,
    },
  },
  {
    pubkey: PK_C,
    name: "Dark Pool Fox",
    sa_address: "0x3333333333333333333333333333333333333333",
    mode: "private",
    timeframe_s: 900,
    tiers: [{ min_power: 20_000, delay_fraction: 0 }],
    followers: 5,
    power_backing: 96_000,
    record_summary: {
      pnl_pct: -2.3,
      win_rate: 0.43,
      max_dd_pct: 11.8,
      trades: 31,
      completeness: 1,
      anchor_coverage: 0.4,
      signatures_ok: true,
      excluded: 1,
      late_anchor: 8,
      unanchored: 10,
    },
  },
  {
    pubkey: PK_D,
    name: "Fresh Start",
    sa_address: "0x4444444444444444444444444444444444444444",
    mode: "public",
    timeframe_s: 3600,
    tiers: [
      { min_power: 1_000, delay_fraction: 0 },
      { min_power: 0, delay_fraction: 0.5 },
    ],
    followers: 0,
    power_backing: 0,
    record_summary: {
      pnl_pct: 0,
      win_rate: 0,
      max_dd_pct: 0,
      trades: 0,
      completeness: 1,
      anchor_coverage: 0,
      signatures_ok: true,
      excluded: 0,
      late_anchor: 0,
      unanchored: 0,
    },
  },
];

export function recordFor(pubkey: string): TrackRecord | undefined {
  const p = providers.find((x) => x.pubkey === pubkey);
  if (!p) return undefined;
  const seed = pubkey.length * 7 + pubkey.charCodeAt(8);
  const s = p.record_summary;
  const gappy = s.completeness < 1;
  const replay = pubkey === PK_A; // Atlas: a compressed replay, 120 points 15 s apart
  return {
    equity:
      s.trades === 0
        ? []
        : replay
          ? walk(seed, 120, s.pnl_pct / 100 / 120, 0.012, 15_000, REPLAY_T0)
          : walk(seed, 120, s.pnl_pct / 100 / 120, 0.012),
    trades: tradesFrom(seed + 1, Math.min(s.trades, 40)),
    gaps: gappy
      ? [
          { after_seq: 31, missing: 2 },
          { after_seq: 77, missing: 1 },
        ]
      : [],
    unrevealed_commitments:
      p.mode === "private"
        ? [
            { id: "c0ffee01".repeat(8), seq: 12, committed_ms: T0 + 40 * HOUR },
            { id: "badc0de2".repeat(8), seq: null, committed_ms: T0 + 90 * HOUR },
          ]
        : [],
    verification: {
      signatures_ok: true,
      chain_complete: !gappy,
      anchored_pct: s.anchor_coverage * 100,
    },
  };
}

export function devSignalsFor(pubkey: string, limit: number): DevSignal[] | undefined {
  const p = providers.find((x) => x.pubkey === pubkey);
  if (!p) return undefined;
  const r = rng(pubkey.length + 99);
  const n = p.record_summary.trades === 0 ? 0 : Math.min(limit, 14);
  const out: DevSignal[] = [];
  for (let i = 0; i < n; i++) {
    const created = REPLAY_T0 + i * p.timeframe_s * 1000 * 1.7 * TIME_SCALE;
    const created_ms = Math.round(created);
    const anchored_ms = r() > 0.15 ? created_ms + Math.round((20 + r() * 400) * 1000) : null;
    out.push({
      id: `${(i + 1).toString(16).padStart(2, "0")}${"ab12cd34".repeat(7)}`,
      seq: i + 1,
      created_ms,
      anchored_ms,
      status: anchored_ms === null ? "waiting_for_time_stamp" : "out",
      instrument: "BINANCE:BTCUSDT",
      side: i % 2 === 0 ? "buy" : "sell",
      action: i % 2 === 0 ? "open" : "close",
      size_fraction: Math.round((0.1 + r() * 0.4) * 100) / 100,
      private: p.mode === "private",
      release: p.tiers
        .map((t, tier) => ({
          tier,
          // wall clock: the schedule shrunk by the replay time scale; every third signal's best
          // tier carries a real receipt time (a follower ledger) a little after the schedule
          at_ms:
            created_ms +
            Math.round(t.delay_fraction * p.timeframe_s * 1000 * TIME_SCALE) +
            (i % 3 === 0 && tier === 0 ? 1_400 : 800),
          observed: i % 3 === 0 && tier === 0,
        }))
        .sort((a, b) => a.at_ms - b.at_ms),
    });
  }
  return out;
}

/** The provider's own view of its signals: no per-tier or per-follower data. */
export function signalsFor(pubkey: string, limit: number): Signal[] | undefined {
  return devSignalsFor(pubkey, limit)?.map(({ release: _release, ...signal }) => signal);
}

/** The viewer's own follower ledger: an executed signal, one rejected by a check, one not placeable, one unknown. */
export function mySignals(limit: number): FollowerSignal[] {
  const first = providers[0];
  if (!first) return [];
  const sigs = signalsFor(first.pubkey, 4) ?? [];
  const out: FollowerSignal[] = sigs.map((s, i) => ({
    id: s.id,
    provider: first.pubkey,
    seq: s.seq,
    received_ms: s.created_ms + 1_400,
    created_ms: s.created_ms,
    tier: 1,
    delay_ms: Math.round(
      (first.tiers[1]?.delay_fraction ?? 0) * first.timeframe_s * 1000 * TIME_SCALE,
    ),
    outcome: i === 3 ? "rejected" : "executed",
    reason: i === 3 ? "not enough cash for the minimum quantity" : "working order",
    instrument: s.instrument,
    side: s.side,
    action: s.action,
  }));
  out.push({
    id: "fe".repeat(32),
    provider: first.pubkey,
    seq: null,
    received_ms: REPLAY_T0 + 60_000,
    created_ms: null,
    tier: null,
    delay_ms: null,
    outcome: "rejected",
    reason: "gap: seq 6 does not follow 4",
    instrument: null,
    side: null,
    action: null,
  });
  return out.sort((a, b) => b.received_ms - a.received_ms).slice(0, limit);
}

const DAY = 24 * HOUR;

/** Fills of a backtest on its own candles: entry then exit, every fifth round trip short. */
export function fillsFor(
  seed: number,
  instrument: string,
  tf: Timeframe,
  startMs: number,
  endMs: number,
  roundTrips: number,
): Fill[] {
  const r = rng(seed);
  const step = TF_MS[tf];
  const slots = Math.floor((endMs - startMs) / step);
  const n = Math.min(roundTrips * 2, Math.floor(slots / 2));
  const out: Fill[] = [];
  let last = -1;
  for (let i = 0; i < n; i++) {
    const slot = Math.max(last + 1, Math.floor(((i + r() * 0.8) / n) * slots));
    last = slot;
    const open = Math.floor(startMs / step) * step + slot * step;
    const trip = Math.floor(i / 2);
    const isEntry = i % 2 === 0;
    const short = trip % 5 === 4;
    const c = candleAt(instrument, tf, open);
    out.push({
      t: open + Math.round(r() * (step - 1)),
      side: isEntry !== short ? "buy" : "sell",
      price: c.c,
      qty: Math.round((0.05 + trip * 0.0001) * 1e4) / 1e4,
    });
  }
  return out;
}

function backtest(
  b: Omit<Backtest, "fills" | "equity" | "timeframe"> & {
    seed: number;
    drift: number;
    vol: number;
  },
  timeframe: Timeframe,
): Backtest {
  const { seed, drift, vol, ...rest } = b;
  const startMs = Date.parse(`${b.start}T00:00:00Z`);
  const endMs = Date.parse(`${b.end}T00:00:00Z`) + DAY;
  const days = Math.round((endMs - startMs) / DAY);
  return {
    ...rest,
    timeframe,
    fills: fillsFor(seed, b.instrument.split(":").pop() ?? "", timeframe, startMs, endMs, b.trades),
    equity: walk(seed, days, drift, vol, DAY, startMs),
  };
}

export const backtests: Backtest[] = [
  backtest(
    {
      id: "bt-ema-cross",
      strategy_name: "EMA cross 12/48",
      instrument: "BINANCE:BTCUSDT",
      start: "2026-01-01",
      end: "2026-06-30",
      pnl_pct: 14.2,
      trades: 88,
      win_rate: 0.52,
      max_dd_pct: 8.4,
      seed: 11,
      drift: 0.0009,
      vol: 0.015,
    },
    "1d",
  ),
  backtest(
    {
      id: "bt-rsi-revert",
      strategy_name: "RSI mean reversion",
      instrument: "BINANCE:ETHUSDT",
      start: "2026-02-01",
      end: "2026-06-30",
      pnl_pct: 5.1,
      trades: 143,
      win_rate: 0.58,
      max_dd_pct: 5.7,
      seed: 23,
      drift: 0.0003,
      vol: 0.012,
    },
    "4h",
  ),
  backtest(
    {
      id: "bt-breakout",
      strategy_name: "Donchian breakout",
      instrument: "BINANCE:BTCUSDT",
      start: "2026-02-01",
      end: "2026-07-31",
      pnl_pct: -3.8,
      trades: 41,
      win_rate: 0.39,
      max_dd_pct: 13.2,
      seed: 37,
      drift: -0.0002,
      vol: 0.016,
    },
    "1d",
  ),
];

export const me: Me = {
  sa_address: "0xDE30000000000000000000000000000000000DE3",
  signed_in: false,
  token_power: 24_000,
  balances: [
    { chain: "BSC", balance: 15_000 },
    { chain: "Ethereum", balance: 7_500 },
    { chain: "zkSync", balance: 1_500 },
  ],
  allocations: { governance_bps: 3000, providers: { [PK_A]: 5000, [PK_B]: 1500 } },
};

/** /api/me: the demo identity, or the wallet session's address (same figures in the mock). */
export function meFor(address: string | null): Me {
  return address ? { ...me, sa_address: address, signed_in: true } : me;
}

export const proposals: Proposal[] = [
  {
    id: "sap-12",
    title: "Raise default relay delay for the public tier",
    options: ["Yes", "No", "Abstain"],
    deadline: "2026-12-01T00:00:00Z",
    tally: { Yes: 41_200, No: 12_800, Abstain: 3_000 },
    complete: true,
    relays_answered: ["wss://relay-a.example", "wss://relay-b.example", "wss://relay-c.example"],
  },
  {
    id: "sap-13",
    title: "Fund an independent audit of the anchoring code",
    options: ["Yes", "No"],
    deadline: "2026-11-15T00:00:00Z",
    tally: { Yes: 9_100, No: 9_400 },
    complete: false,
    relays_answered: ["wss://relay-a.example"],
  },
  {
    id: "sap-09",
    title: "Adopt zkSync in the chain registry",
    options: ["Yes", "No"],
    deadline: "2026-09-20T00:00:00Z",
    tally: { Yes: 70_000, No: 2_000 },
    complete: true,
    relays_answered: ["wss://relay-a.example", "wss://relay-b.example"],
  },
];

export const network: Network = {
  time_scale: TIME_SCALE,
  dev_views: true, // the mock stands in for `sa demo`
  relays: [
    { url: "wss://relay-a.example", ok: true },
    { url: "wss://relay-b.example", ok: true },
    { url: "wss://relay-c.example", ok: false },
  ],
  chains: [
    { name: "BSC", ok: true, block: 51_204_331 },
    { name: "Ethereum", ok: true, block: 21_904_112 },
    { name: "zkSync", ok: false, block: null },
  ],
};
