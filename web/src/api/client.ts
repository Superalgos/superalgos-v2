import { observeBuild } from "../lib/build";
import type {
  AuthNonce,
  AuthSession,
  Backtest,
  Candle,
  DevSignal,
  FollowerSignal,
  IndicatorResponse,
  MarketSymbol,
  Me,
  Network,
  Proposal,
  Provider,
  Signal,
  Timeframe,
  TrackRecord,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
    detail = "",
  ) {
    super(
      `API request ${path} failed with status ${status}${detail}. Check that the API (or 'bun run mock') is running on 127.0.0.1:8787.`,
    );
  }
}

export type Fetcher = (input: string) => Promise<Response>;

export async function getJson<T>(path: string, fetcher: Fetcher = fetch): Promise<T> {
  let res: Response;
  try {
    res = await fetcher(path);
  } catch (e) {
    throw new Error(
      `API request ${path} could not reach the server (${e instanceof Error ? e.message : String(e)}). Start the API or 'bun run mock'.`,
    );
  }
  observeBuild(res);
  if (!res.ok) throw new ApiError(path, res.status);
  return (await res.json()) as T;
}

export type Poster = (input: string, init: RequestInit) => Promise<Response>;

/** POST a JSON body; the session cookie is HttpOnly so it rides along on same-origin requests. */
export async function postJson<T>(path: string, body: unknown, poster: Poster = fetch): Promise<T> {
  let res: Response;
  try {
    res = await poster(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body ?? {}),
    });
  } catch (e) {
    throw new Error(
      `API request POST ${path} could not reach the server (${e instanceof Error ? e.message : String(e)}). Start the API or 'bun run mock'.`,
    );
  }
  observeBuild(res);
  if (!res.ok) {
    let detail = "";
    try {
      const j = (await res.json()) as { error?: unknown };
      if (typeof j.error === "string") detail = `: ${j.error}`;
    } catch {
      // body was not JSON; the status is enough
    }
    throw new ApiError(`POST ${path}`, res.status, detail);
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : {}) as T;
}

export type ExportFormat = "bundle" | "csv";

/** Download URL of a provider's signed signal history (Content-Disposition: attachment). */
export function exportUrl(pubkey: string, format: ExportFormat): string {
  return `/api/providers/${encodeURIComponent(pubkey)}/export?format=${format}`;
}

export interface MarketQuery {
  instrument: string;
  tf: Timeframe;
  endMs?: number;
  limit?: number;
}

function marketQuery(q: MarketQuery): string {
  const p = new URLSearchParams({ instrument: q.instrument, tf: q.tf });
  if (q.endMs !== undefined) p.set("end_ms", String(Math.round(q.endMs)));
  if (q.limit !== undefined) p.set("limit", String(q.limit));
  return p.toString();
}

/** Path of /api/market/indicators; `specs` are joined with commas inside one `spec` value. */
export function indicatorsPath(q: MarketQuery, specs: string[]): string {
  return `/api/market/indicators?${marketQuery(q)}&spec=${encodeURIComponent(specs.join(","))}`;
}

export const api = {
  symbols: (q = "") => getJson<MarketSymbol[]>(`/api/market/symbols?q=${encodeURIComponent(q)}`),
  candles: (q: MarketQuery) => getJson<Candle[]>(`/api/market/candles?${marketQuery(q)}`),
  indicators: (q: MarketQuery, specs: string[]) =>
    getJson<IndicatorResponse>(indicatorsPath(q, specs)),
  providers: () => getJson<Provider[]>("/api/providers"),
  provider: (pk: string) => getJson<Provider>(`/api/providers/${encodeURIComponent(pk)}`),
  record: (pk: string) => getJson<TrackRecord>(`/api/providers/${encodeURIComponent(pk)}/record`),
  signals: (pk: string, limit = 50) =>
    getJson<Signal[]>(`/api/providers/${encodeURIComponent(pk)}/signals?limit=${limit}`),
  devReleases: (pk: string, limit = 50) =>
    getJson<DevSignal[]>(`/api/dev/providers/${encodeURIComponent(pk)}/releases?limit=${limit}`),
  mySignals: (limit = 50) => getJson<FollowerSignal[]>(`/api/me/signals?limit=${limit}`),
  backtests: () => getJson<Backtest[]>("/api/backtests"),
  backtest: (id: string) => getJson<Backtest>(`/api/backtests/${encodeURIComponent(id)}`),
  me: () => getJson<Me>("/api/me"),
  proposals: () => getJson<Proposal[]>("/api/governance/proposals"),
  authNonce: (address: string) => postJson<AuthNonce>("/api/auth/nonce", { address }),
  authVerify: (message: string, signature: string) =>
    postJson<AuthSession>("/api/auth/verify", { message, signature }),
  authLogout: () => postJson<Record<string, never>>("/api/auth/logout", {}),
  network: () => getJson<Network>("/api/network"),
};
