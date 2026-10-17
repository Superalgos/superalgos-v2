import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import { exportUrl, indicatorsPath } from "../src/api/client";
import type { Fill, Signal } from "../src/api/types";
import {
  addSpec,
  backtestWindow,
  bandData,
  candleBucket,
  chartHref,
  chartQuery,
  classifyFills,
  debounce,
  defaultSpec,
  dominantInstrument,
  endMsFor,
  fillMarkers,
  fitTimeframe,
  formatReadout,
  lineData,
  marketSymbol,
  markersByTime,
  nearestTimeframe,
  parseChartQuery,
  parseSpec,
  planPanes,
  priceDigits,
  providerChartEndMs,
  readingsAt,
  removeSpec,
  signalMarkers,
  signalTooltip,
  specError,
  specLabel,
  specString,
  TF_MS,
  TIMEFRAMES,
} from "../src/lib/market";
import { parseHash } from "../src/lib/route";
import { route } from "../mock/routes";
import { backtests, providers, signalsFor } from "../mock/fixture";
import { candles, indicators, splitSpecs } from "../mock/market";

const q = (s = "") => new URLSearchParams(s);

function sig(over: Partial<Signal> = {}): Signal {
  return {
    id: "a".repeat(64),
    seq: 3,
    created_ms: Date.UTC(2026, 9, 13, 9, 17, 30),
    anchored_ms: Date.UTC(2026, 9, 13, 9, 17, 50),
    instrument: "BINANCE:BTCUSDT",
    side: "buy",
    action: "open",
    size_fraction: 0.25,
    private: false,
    status: "out",
    ...over,
  };
}

describe("symbols and timeframes", () => {
  test("marketSymbol strips the venue and upper-cases", () => {
    expect(marketSymbol("BINANCE:BTCUSDT")).toBe("BTCUSDT");
    expect(marketSymbol(" ethusdt ")).toBe("ETHUSDT");
  });
  test("nearestTimeframe", () => {
    expect(nearestTimeframe(3600)).toBe("1h");
    expect(nearestTimeframe(60)).toBe("1m");
    expect(nearestTimeframe(7 * 86400)).toBe("1d");
    expect(nearestTimeframe(0)).toBe("1m");
  });
  test("fitTimeframe goes finer when signals are bunched, stays when spread", () => {
    const t0 = 1_000_000_000_000;
    expect(fitTimeframe(3600, [t0])).toBe("1h");
    expect(fitTimeframe(3600, [t0, t0 + 20 * 3_600_000])).toBe("1h");
    expect(fitTimeframe(3600, [t0, t0 + 24 * 60_000])).toBe("1m");
    expect(fitTimeframe(3600, [t0, t0 + 3 * 3_600_000])).toBe("15m");
    expect(fitTimeframe(3600, [t0, t0 + 1000])).toBe("1m");
  });
  test("candleBucket floors to the candle open", () => {
    expect(candleBucket(Date.UTC(2026, 0, 1, 10, 59, 59), "1h")).toBe(Date.UTC(2026, 0, 1, 10));
  });
  test("endMsFor adds context bars, undefined without times", () => {
    expect(endMsFor([], "1h")).toBeUndefined();
    const t = Date.UTC(2026, 0, 1, 10, 30);
    expect(endMsFor([t, t - 5], "1h", 2)).toBe(Date.UTC(2026, 0, 1, 12));
  });
  test("providerChartEndMs: live ends at the clock, replay keeps the latest signal plus context", () => {
    const signal = Date.UTC(2026, 0, 1, 10, 30);
    const now = Date.UTC(2026, 9, 13, 16, 35); // months after the last signal
    expect(providerChartEndMs([signal], "1h", { live: true, now })).toBe(now);
    expect(providerChartEndMs([], "1h", { live: true, now })).toBe(now); // no signal needed to show the market
    expect(providerChartEndMs([signal], "1h", { live: false, now })).toBe(endMsFor([signal], "1h"));
    expect(providerChartEndMs([], "1h", { live: false, now })).toBeUndefined();
  });
  test("live: the window's last candle is within one bar of now, whatever the timeframe and signals", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...TIMEFRAMES),
        fc.integer({ min: 1_700_000_000_000, max: 2_000_000_000_000 }),
        fc.array(fc.integer({ min: 1_600_000_000_000, max: 1_700_000_000_000 }), { maxLength: 5 }),
        (tf, now, times) => {
          const end = providerChartEndMs(times, tf, { live: true, now }) ?? Number.NaN;
          const last = candleBucket(end, tf); // the candle the API returns last (opening at or before end_ms)
          expect(end).toBe(now);
          expect(now - last).toBeLessThan(TF_MS[tf]);
          expect(last).toBeLessThanOrEqual(now);
        },
      ),
    );
  });
  test("backtestWindow covers the dates on the timeframe and caps the limit", () => {
    const w = backtestWindow({ start: "2026-01-01", end: "2026-01-10", timeframe: "1d" });
    expect(w.endMs).toBe(Date.UTC(2026, 0, 11));
    expect(w.limit).toBe(12);
    expect(backtestWindow({ start: "2026-01-01", end: "2026-06-30", timeframe: "1m" }).limit).toBe(1000);
  });
  test("dominantInstrument", () => {
    expect(dominantInstrument([])).toBeNull();
    expect(
      dominantInstrument([
        { instrument: "BINANCE:ETHUSDT" },
        { instrument: "BTCUSDT" },
        { instrument: "BINANCE:BTCUSDT" },
      ]),
    ).toBe("BTCUSDT");
  });
});

describe("indicator specs", () => {
  test("string, label, parse round trip", () => {
    expect(specString({ kind: "bbands", params: [20, 2] })).toBe("bbands(20,2)");
    expect(specLabel({ kind: "ema", params: [9] })).toBe("EMA 9");
    expect(parseSpec("bbands(20, 2.5)")).toEqual({ kind: "bbands", params: [20, 2.5] });
    expect(parseSpec("RSI(14)")).toEqual({ kind: "rsi", params: [14] });
    expect(parseSpec("nope(3)")).toBeNull();
    expect(parseSpec("ema(1)")).toBeNull();
    expect(parseSpec("ema 20")).toBeNull();
    expect(parseSpec("ema(20,3)")).toBeNull();
  });
  test("specError names the parameter and the allowed range", () => {
    expect(specError({ kind: "ema", params: [20] })).toBeNull();
    expect(specError({ kind: "ema", params: [1] })).toMatch(/period is 1.*between 2 and 500/);
    expect(specError({ kind: "ema", params: [2.5] })).toMatch(/whole number/);
    expect(specError({ kind: "ema", params: [Number.NaN] })).toMatch(/not a number/);
    expect(specError({ kind: "bbands", params: [20, 99] })).toMatch(/std dev is 99/);
    expect(specError({ kind: "bbands", params: [20] })).toMatch(/takes 2 parameter/);
  });
  test("add ignores duplicates, remove drops by spec", () => {
    const a = defaultSpec("ema");
    let l = addSpec([], a);
    l = addSpec(l, { kind: "ema", params: [20] });
    expect(l).toHaveLength(1);
    l = addSpec(l, defaultSpec("rsi"));
    expect(removeSpec(l, a).map(specString)).toEqual(["rsi(14)"]);
  });
});

describe("indicator-pane wiring", () => {
  test("overlays share the price pane; RSI and ATR get their own, in order", () => {
    const plan = planPanes(
      [defaultSpec("rsi"), defaultSpec("ema"), defaultSpec("bbands"), defaultSpec("atr")],
      false,
    );
    expect(plan.items.map((i) => i.placement)).toEqual([
      { type: "pane", pane: 1 },
      { type: "overlay" },
      { type: "overlay" },
      { type: "pane", pane: 2 },
    ]);
    expect(plan.equityPane).toBeNull();
    expect(plan.paneCount).toBe(3);
    expect(plan.items.map((i) => i.key)).toEqual(["rsi(14)", "ema(20)", "bbands(20,2)", "atr(14)"]);
  });
  test("the equity curve takes the pane after the indicator panes", () => {
    const plan = planPanes([defaultSpec("rsi")], true);
    expect(plan.equityPane).toBe(2);
    expect(plan.paneCount).toBe(3);
    expect(planPanes([], true).equityPane).toBe(1);
    expect(planPanes([], false)).toEqual({ items: [], equityPane: null, paneCount: 1 });
  });
  test("overlay colours cycle and differ", () => {
    const plan = planPanes(Array.from({ length: 6 }, (_, i) => ({ kind: "ema" as const, params: [5 + i] })), false);
    expect(plan.items[0]?.color).not.toBe(plan.items[1]?.color);
    expect(plan.items[5]?.color).toBe(plan.items[0]?.color);
  });
  test("API series become chart data without recomputation", () => {
    expect(lineData([{ t: 2000, value: 5 }])).toEqual([{ time: 2, value: 5 }]);
    expect(lineData(undefined)).toEqual([]);
    expect(lineData([{ t: 1000, upper: 3, mid: 2, lower: 1 }])).toEqual([]);
    const b = bandData([{ t: 3000, upper: 3, mid: 2, lower: 1 }]);
    expect(b).toEqual({
      upper: [{ time: 3, value: 3 }],
      mid: [{ time: 3, value: 2 }],
      lower: [{ time: 3, value: 1 }],
    });
    expect(bandData(undefined).mid).toEqual([]);
    expect(bandData([{ t: 1, value: 1 }]).mid).toEqual([]);
  });
  test("crosshair readout shows OHLCV and the indicator values at that candle", () => {
    const plan = planPanes([defaultSpec("ema"), defaultSpec("bbands")], false).items;
    const data = {
      "ema(20)": [{ t: 1000, value: 10.5 }],
      "bbands(20,2)": [{ t: 1000, upper: 12, mid: 11, lower: 10 }],
    };
    const r = readingsAt(plan, data, 1000);
    expect(r.map((x) => x.label)).toEqual(["EMA 20", "Bollinger 20, 2"]);
    expect(readingsAt(plan, data, 5000)).toEqual([]);
    const text = formatReadout({ t: 1000, o: 1, h: 3, l: 0.5, c: 2, v: 10 }, r);
    expect(text).toContain("O 1.000 H 3.000 L 0.500 C 2.000");
    expect(text).toContain("Vol 10.00");
    expect(text).toContain("EMA 20: 10.50");
    expect(text).toContain("upper 12.00 mid 11.00 lower 10.00");
    expect(priceDigits(65000)).toBe(2);
    expect(priceDigits(0.004)).toBe(6);
  });
});

describe("signal markers (SEP-8.3)", () => {
  test("one marker per signal of the instrument, buy below and sell above, at the created candle", () => {
    const sigs = [
      sig(),
      sig({ id: "b".repeat(64), seq: 4, side: "sell", created_ms: Date.UTC(2026, 9, 13, 10, 5) }),
      sig({ id: "c".repeat(64), seq: 5, instrument: "BINANCE:ETHUSDT" }),
    ];
    const m = signalMarkers(sigs, "BTCUSDT", "1h");
    expect(m).toHaveLength(2);
    expect(m[0]).toMatchObject({ position: "belowBar", shape: "arrowUp", tone: "buy", text: "#3" });
    expect(m[0]?.time).toBe(Date.UTC(2026, 9, 13, 9) / 1000);
    expect(m[0]?.t).toBe(sigs[0]?.created_ms ?? 0);
    expect(m[1]).toMatchObject({ position: "aboveBar", shape: "arrowDown", tone: "sell" });
    expect(m[1]?.time).toBe(Date.UTC(2026, 9, 13, 10) / 1000);
  });
  test("tooltip has seq, action, size and the time-stamp status, and no per-tier data", () => {
    const tip = signalTooltip(sig());
    expect(tip).toContain("#3 buy open");
    expect(tip).toContain("size 25%");
    expect(tip).toContain("time-stamped 2026-10-13 09:17:50 (UTC) (+20 s)");
    expect(tip).toContain("created 2026-10-13 09:17:30 (UTC)");
    expect(tip).not.toContain("tier");
    expect(signalTooltip(sig({ anchored_ms: null, status: "waiting_for_time_stamp" }))).toContain(
      "waiting for time-stamp",
    );
  });
  test("markers sort by time and group by candle", () => {
    const sigs = [sig({ created_ms: 5_000_000 }), sig({ id: "d".repeat(64), seq: 9, created_ms: 4_000_000 })];
    const m = signalMarkers(sigs, "BTCUSDT", "1d");
    expect(m.map((x) => x.t)).toEqual([4_000_000, 5_000_000]);
    expect(markersByTime(m).get(0)).toHaveLength(2);
  });
  test("mock provider signals map one marker each", () => {
    const pk = providers[0]?.pubkey ?? "";
    const s = signalsFor(pk, 50) ?? [];
    expect(signalMarkers(s, "BTCUSDT", "1m")).toHaveLength(s.length);
  });
});

describe("backtest overlay markers (SEP-8.5)", () => {
  const fills: Fill[] = [
    { t: 3000, side: "buy", price: 100, qty: 1 },
    { t: 1000, side: "sell", price: 90, qty: 2 }, // short entry, earlier
  ];
  test("classifyFills walks the position and sorts by time", () => {
    expect(classifyFills([
      { t: 1, side: "buy", price: 1, qty: 1 },
      { t: 2, side: "sell", price: 2, qty: 1 },
      { t: 3, side: "sell", price: 2, qty: 1 },
      { t: 4, side: "buy", price: 1, qty: 1 },
    ]).map((x) => x.kind)).toEqual(["entry", "exit", "entry", "exit"]);
    expect(classifyFills(fills).map((x) => [x.fill.t, x.kind])).toEqual([[1000, "entry"], [3000, "exit"]].map(([a, b]) => [a, b]));
    // flipping through zero counts as an entry of the new side
    expect(classifyFills([
      { t: 1, side: "buy", price: 1, qty: 1 },
      { t: 2, side: "sell", price: 1, qty: 3 },
    ]).map((x) => x.kind)).toEqual(["entry", "entry"]);
  });
  test("markers carry the report's exact times and prices", () => {
    const m = fillMarkers(fills, "1h");
    expect(m).toHaveLength(2);
    expect(m[0]).toMatchObject({ t: 1000, tone: "entry", position: "aboveBar", text: "In" });
    expect(m[1]).toMatchObject({ t: 3000, tone: "exit", shape: "circle", position: "belowBar", text: "Out" });
    expect(m[1]?.tooltip).toContain("Exit buy 1.0000 @ 100.00");
    expect(m[1]?.tooltip).toContain("1970-01-01 00:00:03");
  });
  test("every mock backtest fill becomes exactly one marker with its own time and price", () => {
    for (const b of backtests) {
      const ms = fillMarkers(b.fills, b.timeframe);
      expect(ms).toHaveLength(b.fills.length);
      const sorted = [...b.fills].sort((x, y) => x.t - y.t);
      ms.forEach((mk, i) => {
        expect(mk.t).toBe(sorted[i]?.t ?? -1);
        expect(mk.tooltip).toContain(sorted[i]?.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) ?? "?");
        expect(mk.time).toBe(candleBucket(mk.t, b.timeframe) / 1000);
      });
    }
  });
});

describe("URL state", () => {
  test("defaults and fallbacks", () => {
    expect(parseChartQuery("")).toEqual({ instrument: "BTCUSDT", tf: "1h", ind: [] });
    expect(parseChartQuery("?instrument=ethusdt&tf=zz&ind=bad(1)")).toEqual({ instrument: "ETHUSDT", tf: "1h", ind: [] });
  });
  test("round trip with indicators, provider and backtest", () => {
    const st = {
      instrument: "SOLUSDT",
      tf: "15m" as const,
      ind: [{ kind: "bbands" as const, params: [20, 2] }, { kind: "rsi" as const, params: [14] }],
      provider: "npub1x",
      bt: "bt-1",
    };
    expect(parseChartQuery(chartQuery(st))).toEqual(st);
    expect(chartHref({ instrument: "ETHUSDT", tf: "4h" })).toBe("#/chart?instrument=ETHUSDT&tf=4h");
  });
  test("parseHash reads the chart route and keeps the other routes", () => {
    expect(parseHash("#/chart?instrument=ETHUSDT&tf=1d")).toEqual({
      page: "chart",
      state: { instrument: "ETHUSDT", tf: "1d", ind: [] },
    });
    expect(parseHash("#/chart").page).toBe("chart");
    expect(parseHash("#/providers/abc")).toEqual({ page: "provider", pubkey: "abc" });
    expect(parseHash("#/backtests/x?foo=1")).toEqual({ page: "backtests", id: "x" });
  });
});

describe("API urls", () => {
  test("export URLs", () => {
    expect(exportUrl("npub1/x", "bundle")).toBe("/api/providers/npub1%2Fx/export?format=bundle");
    expect(exportUrl("pk", "csv")).toBe("/api/providers/pk/export?format=csv");
  });
  test("indicators path joins specs into one param", () => {
    const p = indicatorsPath({ instrument: "BTCUSDT", tf: "1h", endMs: 5.4, limit: 10 }, ["ema(20)", "bbands(20,2)"]);
    expect(p).toBe(
      "/api/market/indicators?instrument=BTCUSDT&tf=1h&end_ms=5&limit=10&spec=ema(20)%2Cbbands(20%2C2)",
    );
    expect(decodeURIComponent(p.split("spec=")[1] ?? "")).toBe("ema(20),bbands(20,2)");
  });
});

describe("debounce", () => {
  test("only the last call fires; cancel drops it", async () => {
    const seen: number[] = [];
    const d = debounce((n: number) => seen.push(n), 15);
    d(1);
    d(2);
    d(3);
    await Bun.sleep(300);
    expect(seen).toEqual([3]);
    d(4);
    d.cancel();
    d.cancel();
    await Bun.sleep(300);
    expect(seen).toEqual([3]);
  });
});

describe("mock market endpoints", () => {
  const base = "instrument=BTCUSDT&tf=1h&end_ms=1791878400000";
  test("symbols search", () => {
    const all = route("/api/market/symbols", q()).body as { instrument: string }[];
    expect(all.length).toBeGreaterThan(5);
    const eth = route("/api/market/symbols", q("q=eth")).body as { instrument: string }[];
    expect(eth.every((s) => s.instrument.includes("ETH"))).toBe(true);
    expect(route("/api/market/symbols", q("q=zzzz")).body).toEqual([]);
  });
  test("candles are deterministic, ordered, sane and honour limit and tf", () => {
    const r = route("/api/market/candles", q(`${base}&limit=5`));
    const cs = r.body as ReturnType<typeof candles>;
    expect(cs).toHaveLength(5);
    expect(cs).toEqual(route("/api/market/candles", q(`${base}&limit=5`)).body as typeof cs);
    cs.forEach((c, i) => {
      expect(c.h).toBeGreaterThanOrEqual(Math.max(c.o, c.c));
      expect(c.l).toBeLessThanOrEqual(Math.min(c.o, c.c));
      if (i > 0) expect(c.t - (cs[i - 1]?.t ?? 0)).toBe(TF_MS["1h"]);
    });
    expect((route("/api/market/candles", q(`${base}&limit=99999`)).body as unknown[]).length).toBe(1000);
    expect((route("/api/market/candles", q("instrument=BTCUSDT&tf=1m&limit=abc")).body as unknown[]).length).toBe(500);
  });
  test("bad input gives actionable errors", () => {
    expect(route("/api/market/candles", q("instrument=NOPE&tf=1h")).status).toBe(404);
    const tf = route("/api/market/candles", q("instrument=BTCUSDT&tf=2h"));
    expect(tf.status).toBe(400);
    expect(JSON.stringify(tf.body)).toContain("2h");
    expect(route("/api/market/candles", q("instrument=BTCUSDT&tf=1h&end_ms=abc")).status).toBe(400);
    expect(route("/api/market/zzz", q()).status).toBe(404);
    expect(route("/api/market/indicators", q(`${base}&spec=foo(3)`)).status).toBe(400);
    expect(route("/api/market/indicators", q(`${base}&spec=ema(x)`)).status).toBe(400);
  });
  test("indicators come back keyed by spec, with band points for bbands", () => {
    const spec = "ema(20),sma(5),rsi(14),atr(14),bbands(20,2)";
    const r = route("/api/market/indicators", q(`${base}&limit=30&spec=${encodeURIComponent(spec)}`));
    expect(r.status).toBe(200);
    const body = r.body as Record<string, { t: number; value?: number; upper?: number; lower?: number }[]>;
    expect(Object.keys(body).sort()).toEqual(["atr(14)", "bbands(20,2)", "ema(20)", "rsi(14)", "sma(5)"]);
    for (const k of Object.keys(body)) expect(body[k]).toHaveLength(30);
    expect(body["rsi(14)"]?.every((p) => (p.value ?? -1) >= 0 && (p.value ?? 101) <= 100)).toBe(true);
    expect(body["bbands(20,2)"]?.every((p) => (p.upper ?? 0) >= (p.lower ?? 1))).toBe(true);
    expect(splitSpecs(spec)).toHaveLength(5);
    expect(splitSpecs("")).toEqual([]);
  });
  test("indicator maths on a known series", () => {
    const cs = [1, 2, 3, 4, 5, 6].map((c, i) => ({ t: i, o: c, h: c + 1, l: c - 1, c, v: 1 }));
    expect((indicators(cs, ["sma(3)"])["sma(3)"] as { value: number }[]).map((p) => p.value)).toEqual([2, 3, 4, 5]);
    expect((indicators(cs, ["ema(3)"])["ema(3)"] as { value: number }[])[0]?.value).toBe(2);
    expect((indicators(cs, ["rsi(3)"])["rsi(3)"] as { value: number }[]).at(-1)?.value).toBe(100);
    expect((indicators(cs, ["atr(3)"])["atr(3)"] as { value: number }[])[0]?.value).toBeCloseTo(2);
  });
});

describe("mock export", () => {
  const pk = providers[0]?.pubkey ?? "";
  test("bundle is the signed-history shape with one event per signal", () => {
    const r = route(`/api/providers/${pk}/export`, q("format=bundle"));
    expect(r.status).toBe(200);
    expect(r.contentType).toBe("application/json");
    expect(r.filename).toContain(".bundle.json");
    const b = JSON.parse(String(r.body));
    expect(b.v).toBe(1);
    expect(b.provider).toMatch(/^[0-9a-f]{64}$/);
    expect(b.events).toHaveLength((signalsFor(pk, 1000) ?? []).length);
    expect(b.events[0].id).toMatch(/^[0-9a-f]{64}$/);
    expect(b.events[0].sig).toMatch(/^[0-9a-f]{128}$/);
  });
  test("csv has a header and a row per signal", () => {
    const r = route(`/api/providers/${pk}/export`, q("format=csv"));
    expect(r.contentType).toContain("text/csv");
    const lines = String(r.body).trimEnd().split("\n");
    expect(lines[0]).toStartWith("seq,id,created_ms");
    expect(lines).toHaveLength(1 + (signalsFor(pk, 1000) ?? []).length);
  });
  test("unknown format or provider", () => {
    expect(route(`/api/providers/${pk}/export`, q("format=xml")).status).toBe(400);
    expect(route(`/api/providers/${pk}/export`, q()).status).toBe(400);
    expect(route("/api/providers/nope/export", q("format=csv")).status).toBe(404);
  });
});
