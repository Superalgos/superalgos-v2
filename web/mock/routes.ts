import { signalsBundle, signalsCsv } from "./exporter";
import {
  backtests,
  devSignalsFor,
  meFor,
  mySignals,
  network,
  proposals,
  providers,
  recordFor,
  signalsFor,
} from "./fixture";
import {
  candles,
  indicators,
  isTimeframe,
  MOCK_NOW,
  searchSymbols,
  splitSpecs,
  symbols,
} from "./market";

/** `contentType` set means `body` is a string served as a file named `filename`. */
export interface RouteResult {
  status: number;
  body: unknown;
  contentType?: string;
  filename?: string;
}

/** Pure request router so it can be unit-tested without a socket. */
export function route(
  pathname: string,
  search: URLSearchParams,
  sessionAddress: string | null = null,
): RouteResult {
  const ok = (body: unknown) => ({ status: 200, body });
  const notFound = (what: string) => ({
    status: 404,
    body: { error: `${what} not found: ${pathname}` },
  });
  const bad = (error: string): RouteResult => ({ status: 400, body: { error } });
  const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] !== "api") return notFound("route");
  const [, a, b, c] = parts;
  const exportRoute = (pk: string, format: string | null): RouteResult => {
    const sigs = signalsFor(pk, 1000);
    if (!sigs) return notFound("provider");
    if (format === "csv") {
      return {
        status: 200,
        body: signalsCsv(sigs),
        contentType: "text/csv; charset=utf-8",
        filename: `signals-${pk}.csv`,
      };
    }
    if (format === "bundle") {
      return {
        status: 200,
        body: JSON.stringify(signalsBundle(pk, sigs, MOCK_NOW), null, 2),
        contentType: "application/json",
        filename: `signals-${pk}.bundle.json`,
      };
    }
    return bad(`format must be "bundle" or "csv", got ${JSON.stringify(format)}`);
  };
  if (a === "providers" && !b) return ok(providers);
  if (a === "providers" && b && !c) {
    const p = providers.find((x) => x.pubkey === b);
    return p ? ok(p) : notFound("provider");
  }
  if (a === "providers" && b && c === "record") {
    const r = recordFor(b);
    return r ? ok(r) : notFound("provider");
  }
  if (a === "providers" && b && c === "signals") {
    const limit = Number.parseInt(search.get("limit") ?? "50", 10);
    const s = signalsFor(b, Number.isFinite(limit) && limit > 0 ? limit : 50);
    return s ? ok(s) : notFound("provider");
  }
  if (a === "providers" && b && c === "export") return exportRoute(b, search.get("format"));
  if (a === "dev" && b === "providers" && c && parts[4] === "releases") {
    const limit = Number.parseInt(search.get("limit") ?? "50", 10);
    const s = devSignalsFor(c, Number.isFinite(limit) && limit > 0 ? limit : 50);
    return s ? ok(s) : notFound("provider");
  }
  if (a === "market") return marketRoute(b, search);
  if (a === "backtests" && !b) return ok(backtests);
  if (a === "backtests" && b) {
    const bt = backtests.find((x) => x.id === b);
    return bt ? ok(bt) : notFound("backtest");
  }
  if (a === "me" && b === "signals") {
    const limit = Number.parseInt(search.get("limit") ?? "50", 10);
    return ok(mySignals(Number.isFinite(limit) && limit > 0 ? limit : 50));
  }
  if (a === "me") return ok(meFor(sessionAddress));
  if (a === "governance" && b === "proposals") return ok(proposals);
  if (a === "network") return ok(network);
  return notFound("route");
}

function marketRoute(what: string | undefined, search: URLSearchParams): RouteResult {
  const bad = (error: string): RouteResult => ({ status: 400, body: { error } });
  if (what === "symbols") return { status: 200, body: searchSymbols(search.get("q") ?? "") };
  if (what !== "candles" && what !== "indicators") {
    return { status: 404, body: { error: `market route not found: ${what}` } };
  }
  const instrument = (search.get("instrument") ?? "").toUpperCase();
  const tf = search.get("tf");
  if (!symbols.some((s) => s.instrument === instrument)) {
    return {
      status: 404,
      body: {
        error: `unknown instrument ${JSON.stringify(instrument)}: search /api/market/symbols`,
      },
    };
  }
  if (!isTimeframe(tf)) {
    return {
      status: 400,
      body: { error: `tf must be 1m, 5m, 15m, 1h, 4h or 1d, got ${JSON.stringify(tf)}` },
    };
  }
  const endRaw = search.get("end_ms");
  const end = endRaw === null ? MOCK_NOW : Number(endRaw);
  const limRaw = Number.parseInt(search.get("limit") ?? "500", 10);
  if (!Number.isFinite(end)) return bad(`end_ms must be epoch milliseconds, got ${endRaw}`);
  const limit = Math.min(Math.max(Number.isFinite(limRaw) ? limRaw : 500, 1), 1000);
  if (what === "candles") return { status: 200, body: candles(instrument, tf, end, limit) };
  const specs = splitSpecs(search.get("spec") ?? "");
  // indicators are warmed up on the candles before the window, so the first window point has a value
  const warm = 600;
  const cs = candles(instrument, tf, end, limit + warm);
  const from = (cs[warm]?.t ?? 0) - 0;
  try {
    const all = indicators(cs, specs);
    const body = Object.fromEntries(
      Object.entries(all).map(([k, v]) => [k, (v as { t: number }[]).filter((p) => p.t >= from)]),
    );
    return { status: 200, body };
  } catch (e) {
    return bad(e instanceof Error ? e.message : String(e));
  }
}
