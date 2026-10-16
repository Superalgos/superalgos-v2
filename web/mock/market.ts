import type {
  BandPoint,
  Candle,
  IndicatorResponse,
  MarketSymbol,
  Timeframe,
  ValuePoint,
} from "../src/api/types";
import { isTimeframe, TF_MS } from "../src/lib/market";

// Stand-in for the F7 market endpoints. The real API serves Binance candles and computes indicators
// with sa_engine; this mock invents deterministic ones so the chart works standalone. The indicator
// maths here exists only to fill the mock (the browser never computes indicators, SEP-8.2).

export { isTimeframe, TF_MS };

/** "Now" of the mock market: shortly after the replay signals of the provider fixtures. */
export const MOCK_NOW = Date.UTC(2026, 9, 13, 12, 0);

const BASES: [string, string, number][] = [
  ["BTC", "USDT", 64_000],
  ["ETH", "USDT", 3_200],
  ["BNB", "USDT", 590],
  ["SOL", "USDT", 150],
  ["XRP", "USDT", 0.55],
  ["ADA", "USDT", 0.45],
  ["DOGE", "USDT", 0.12],
  ["LINK", "USDT", 14],
  ["ETH", "BTC", 0.05],
  ["BNB", "BTC", 0.009],
  ["BTC", "USDC", 64_010],
  ["SA", "USDT", 0.004],
];

export const symbols: MarketSymbol[] = BASES.map(([base, quote]) => ({
  instrument: `${base}${quote}`,
  base,
  quote,
}));

const BASE_PRICE = new Map(BASES.map(([b, q, p]) => [`${b}${q}`, p]));

export function searchSymbols(q: string): MarketSymbol[] {
  const needle = q.trim().toUpperCase();
  return needle
    ? symbols.filter((s) => s.instrument.includes(needle) || s.base.includes(needle))
    : symbols;
}

function hash(str: string, n: number): number {
  let h = 2166136261 ^ n;
  for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  h ^= h >>> 15;
  h = Math.imul(h, 2246822519);
  h ^= h >>> 13;
  return (h >>> 0) / 4294967296;
}

function price(instrument: string, t: number): number {
  const base = BASE_PRICE.get(instrument) ?? 1;
  const seed = hash(instrument, 1) * 6.28;
  const wave =
    0.09 * Math.sin(t / 2.6e8 + seed) +
    0.035 * Math.sin(t / 3.1e7 + seed * 2) +
    0.008 * Math.sin(t / 2.3e6 + seed * 3);
  return base * (1 + wave);
}

export function candleAt(instrument: string, tf: Timeframe, open: number): Candle {
  const step = TF_MS[tf];
  const samples = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => price(instrument, open + (i / 7) * step));
  const wick = (k: number) => 1 + 0.0015 * hash(instrument, open + k) * Math.sqrt(step / 60_000);
  const o = price(instrument, open);
  const c = price(instrument, open + step);
  const round = (x: number) => Number(x.toPrecision(7));
  return {
    t: open,
    o: round(o),
    h: round(Math.max(...samples) * wick(1)),
    l: round(Math.min(...samples) / wick(2)),
    c: round(c),
    v: Math.round(1000 * (0.5 + hash(instrument, open + 3)) * Math.sqrt(step / 60_000)) / 10,
  };
}

export function candles(instrument: string, tf: Timeframe, endMs: number, limit: number): Candle[] {
  const step = TF_MS[tf];
  const last = Math.floor(endMs / step) * step;
  const out: Candle[] = [];
  for (let i = limit - 1; i >= 0; i--) out.push(candleAt(instrument, tf, last - i * step));
  return out;
}

// ---------------------------------------------------------------- indicators

/** Split "ema(20),bbands(20,2)" on the commas outside parentheses. */
export function splitSpecs(spec: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of spec) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === "," && depth === 0) {
      out.push(cur.trim());
      cur = "";
    } else cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

function ema(cs: Candle[], n: number): ValuePoint[] {
  const out: ValuePoint[] = [];
  const k = 2 / (n + 1);
  let prev = 0;
  cs.forEach((c, i) => {
    if (i < n - 1) return;
    prev = i === n - 1 ? cs.slice(0, n).reduce((a, x) => a + x.c, 0) / n : c.c * k + prev * (1 - k);
    out.push({ t: c.t, value: prev });
  });
  return out;
}

function sma(cs: Candle[], n: number): ValuePoint[] {
  const out: ValuePoint[] = [];
  for (let i = n - 1; i < cs.length; i++) {
    const w = cs.slice(i - n + 1, i + 1);
    out.push({ t: cs[i]?.t ?? 0, value: w.reduce((a, x) => a + x.c, 0) / n });
  }
  return out;
}

function bbands(cs: Candle[], n: number, mult: number): BandPoint[] {
  const out: BandPoint[] = [];
  for (let i = n - 1; i < cs.length; i++) {
    const w = cs.slice(i - n + 1, i + 1).map((x) => x.c);
    const mid = w.reduce((a, x) => a + x, 0) / n;
    const sd = Math.sqrt(w.reduce((a, x) => a + (x - mid) ** 2, 0) / n);
    out.push({ t: cs[i]?.t ?? 0, upper: mid + mult * sd, mid, lower: mid - mult * sd });
  }
  return out;
}

function rsi(cs: Candle[], n: number): ValuePoint[] {
  const out: ValuePoint[] = [];
  let gain = 0;
  let loss = 0;
  for (let i = 1; i < cs.length; i++) {
    const d = (cs[i]?.c ?? 0) - (cs[i - 1]?.c ?? 0);
    const g = Math.max(d, 0);
    const l = Math.max(-d, 0);
    if (i <= n) {
      gain += g / n;
      loss += l / n;
    } else {
      gain = (gain * (n - 1) + g) / n;
      loss = (loss * (n - 1) + l) / n;
    }
    if (i >= n) {
      out.push({ t: cs[i]?.t ?? 0, value: loss === 0 ? 100 : 100 - 100 / (1 + gain / loss) });
    }
  }
  return out;
}

function atr(cs: Candle[], n: number): ValuePoint[] {
  const out: ValuePoint[] = [];
  let cur = 0;
  for (let i = 1; i < cs.length; i++) {
    const c = cs[i] as Candle;
    const pc = (cs[i - 1] as Candle).c;
    const tr = Math.max(c.h - c.l, Math.abs(c.h - pc), Math.abs(c.l - pc));
    if (i <= n) cur += tr / n;
    else cur = (cur * (n - 1) + tr) / n;
    if (i >= n) out.push({ t: c.t, value: cur });
  }
  return out;
}

/** Indicator series for each spec string; throws a message naming the bad spec. */
export function indicators(cs: Candle[], specs: string[]): IndicatorResponse {
  const out: IndicatorResponse = {};
  for (const spec of specs) {
    const m = /^([a-z]+)\(([\d.,\s]*)\)$/.exec(spec);
    const args = (m?.[2] ?? "").split(",").map((x) => Number(x.trim()));
    const n = args[0] ?? Number.NaN;
    if (!m || !Number.isInteger(n) || n < 2) {
      throw new Error(`bad indicator spec "${spec}": use name(period[,param]), e.g. ema(20)`);
    }
    switch (m[1]) {
      case "ema":
        out[spec] = ema(cs, n);
        break;
      case "sma":
        out[spec] = sma(cs, n);
        break;
      case "bbands":
        out[spec] = bbands(cs, n, args[1] ?? 2);
        break;
      case "rsi":
        out[spec] = rsi(cs, n);
        break;
      case "atr":
        out[spec] = atr(cs, n);
        break;
      default:
        throw new Error(
          `unknown indicator "${m[1]}" in "${spec}": supported are ema, sma, bbands, rsi, atr`,
        );
    }
  }
  return out;
}
