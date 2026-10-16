// Typed mirror of the read-only HTTP API (apps/sa-api). Units are documented in API-CHANGES.md.

export type Mode = "public" | "private";
export type Side = "buy" | "sell";
export type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

/** F7: GET /api/market/symbols */
export interface MarketSymbol {
  instrument: string; // "BTCUSDT"
  base: string;
  quote: string;
}

/** F7: GET /api/market/candles. `t` is the candle open time, epoch ms. */
export interface Candle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface ValuePoint {
  t: number;
  value: number;
}

export interface BandPoint {
  t: number;
  upper: number;
  mid: number;
  lower: number;
}

/** F7: GET /api/market/indicators, keyed by the spec string that was asked for (e.g. "ema(20)"). */
export type IndicatorSeries = ValuePoint[] | BandPoint[];
export type IndicatorResponse = Record<string, IndicatorSeries>;

export interface Tier {
  min_power: number;
  delay_fraction: number;
}

export interface RecordSummary {
  pnl_pct: number;
  win_rate: number; // 0..1
  max_dd_pct: number;
  trades: number;
  completeness: number; // 0..1
  anchor_coverage: number; // 0..1, on-time anchors only
  signatures_ok: boolean;
  excluded: number; // signals left out of the score (no candles, unfilled, over the exposure cap...)
  late_anchor: number; // anchored after the tolerance: scored as misses, not hidden
  unanchored: number; // published but never anchored: scored as misses, not hidden
}

export interface Provider {
  pubkey: string;
  name: string;
  sa_address: string;
  mode: Mode;
  timeframe_s: number;
  tiers: Tier[];
  followers: number;
  power_backing: number;
  record_summary: RecordSummary;
}

export interface EquityPoint {
  t: number; // epoch ms
  equity: number;
}

export interface Trade {
  open_t: number;
  close_t: number;
  side: Side;
  entry: number;
  exit: number;
  pnl_pct: number;
}

export interface Gap {
  after_seq: number;
  missing: number;
}

export interface UnrevealedCommitment {
  id: string;
  seq: number | null;
  committed_ms: number;
}

export interface Verification {
  signatures_ok: boolean;
  chain_complete: boolean;
  anchored_pct: number; // 0..100
}

export interface TrackRecord {
  equity: EquityPoint[];
  trades: Trade[];
  gaps: Gap[];
  unrevealed_commitments: UnrevealedCommitment[];
  verification: Verification;
}

/** The provider's one status for a signal it published: out, or the step that is missing. */
export type SignalStatus = "out" | "waiting_for_time_stamp";

/** Dev views only: the release of a signal to one tier. */
export interface Release {
  tier: number; // index into provider.tiers
  at_ms: number; // wall clock; scheduled offset = delay_fraction x timeframe x network.time_scale
  observed: boolean; // true when at_ms is a real follower receipt time, false when computed
}

export interface Signal {
  id: string;
  seq: number;
  created_ms: number;
  anchored_ms: number | null;
  instrument: string;
  side: Side;
  action: "open" | "close" | "adjust";
  size_fraction: number;
  private: boolean;
  status: SignalStatus; // "out": signed, time-stamped (anchored) and on the relay
}

/** Dev views only: GET /api/dev/providers/{pubkey}/releases. */
export interface DevSignal extends Signal {
  release: Release[];
}

export type FollowerOutcome = "executed" | "rejected" | "pending";

/** GET /api/me/signals: one row of the viewer's own follower ledger. */
export interface FollowerSignal {
  id: string;
  provider: string;
  seq: number | null;
  received_ms: number;
  created_ms: number | null;
  tier: number | null; // the follower's own tier with this provider
  delay_ms: number | null; // wall-clock delay that tier schedules (delay fraction x timeframe x time scale)
  outcome: FollowerOutcome;
  reason: string;
  instrument: string | null;
  side: Side | null;
  action: "open" | "close" | "adjust" | null;
}

export interface Fill {
  t: number; // epoch ms
  side: Side;
  price: number;
  qty: number;
}

export interface Backtest {
  id: string;
  strategy_name: string;
  instrument: string; // "BINANCE:BTCUSDT" or "BTCUSDT": the chart strips the venue prefix
  timeframe: Timeframe; // F7: the candles the strategy ran on
  fills: Fill[]; // F7: every fill, in time order
  start: string; // ISO date
  end: string;
  pnl_pct: number;
  trades: number;
  win_rate: number; // 0..1
  max_dd_pct: number;
  equity: EquityPoint[];
}

export interface Me {
  sa_address: string;
  signed_in: boolean; // false: the server's demo identity; true: the wallet session's address
  token_power: number;
  balances?: ChainBalance[]; // proposed, see API-CHANGES.md
  allocations: { governance_bps: number; providers: Record<string, number> };
}

export interface ChainBalance {
  chain: string;
  balance: number;
}

export interface Proposal {
  id: string;
  title: string;
  options: string[];
  deadline: string; // ISO
  tally: Record<string, number>;
  complete: boolean;
  relays_answered: string[];
}

export interface Network {
  time_scale: number; // relay time_scale: 1 live, 1/speed in a demo replay
  dev_views: boolean; // the API serves the development views (per-tier delivery timeline)
  relays: { url: string; ok: boolean }[];
  chains: { name: string; ok: boolean; block: number | null }[];
}

/** POST /api/auth/nonce: the server builds the full EIP-4361 message for `address`. */
export interface AuthNonce {
  nonce: string;
  message: string;
}

/** POST /api/auth/verify: the session itself travels in an HttpOnly cookie. */
export interface AuthSession {
  address: string;
}
