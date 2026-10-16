/** Running a backtest from the designer: `POST /api/backtests/run`, then polling `GET /api/backtests/jobs/{id}` (SEP-5.7). */

import { chartHref, isTimeframe, marketSymbol } from "../market";
import type { Json } from "./doc";
import type { Issue } from "./errors";

export interface JobStatus {
  id: string;
  state: "running" | "done" | "failed" | "timeout";
  started_ms: number;
  finished_ms: number | null;
  backtest_id: string | null;
  error: string | null;
}

export interface RunRequest {
  strategy: Json;
  instrument: string;
  timeframe: string;
  start: string;
  end: string;
}

export type RunOutcome =
  | { kind: "started"; job: JobStatus }
  /** The server said no: 403 (not loopback), 409 (a job is running), 422 (bad request) or another status. */
  | { kind: "refused"; status: number; reason: string; errors: Issue[]; runningJob?: string }
  | { kind: "unreachable"; reason: string };

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

async function readJson(res: Response): Promise<Record<string, unknown>> {
  try {
    const v = (await res.json()) as unknown;
    return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** The 403 text the API sends names the bind or header that failed; it is shown as is. */
export async function runBacktest(req: RunRequest, fetcher: Fetcher = fetch): Promise<RunOutcome> {
  let res: Response;
  try {
    res = await fetcher("/api/backtests/run", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(req),
    });
  } catch (e) {
    return {
      kind: "unreachable",
      reason: `could not reach the API (${e instanceof Error ? e.message : String(e)}). Start the API or 'bun run mock', then run again.`,
    };
  }
  const body = await readJson(res);
  if (res.status === 202 || res.status === 200) {
    return { kind: "started", job: body as unknown as JobStatus };
  }
  const detail = typeof body.error === "string" ? body.error : `HTTP ${res.status}`;
  const errors = Array.isArray(body.errors) ? (body.errors as Issue[]) : [];
  const reason =
    res.status === 403
      ? `Backtests are not allowed from this page: ${detail}`
      : res.status === 409
        ? `Another backtest is already running: ${detail}`
        : res.status === 422
          ? `The backtest request was refused: ${detail}`
          : `The backtest could not start (${res.status}): ${detail}`;
  return {
    kind: "refused",
    status: res.status,
    reason,
    errors,
    runningJob: typeof body.job === "string" ? body.job : undefined,
  };
}

export async function getJob(id: string, fetcher: Fetcher = fetch): Promise<JobStatus> {
  const res = await fetcher(`/api/backtests/jobs/${encodeURIComponent(id)}`);
  if (!res.ok) {
    const body = await readJson(res);
    throw new Error(
      `GET /api/backtests/jobs/${id} answered ${res.status}${typeof body.error === "string" ? `: ${body.error}` : ""}. Jobs are lost when the API restarts; run the backtest again.`,
    );
  }
  return (await res.json()) as JobStatus;
}

export interface PollOptions {
  fetcher?: Fetcher;
  intervalMs?: number;
  sleep?: (ms: number) => Promise<void>;
  onUpdate?: (job: JobStatus) => void;
  signal?: AbortSignal;
  /** Safety bound on the number of polls (the server bounds the job itself). */
  maxPolls?: number;
}

/** Polls until the job leaves `running`; throws when `signal` aborts or `maxPolls` is hit. */
export async function pollJob(id: string, opts: PollOptions = {}): Promise<JobStatus> {
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const max = opts.maxPolls ?? 2000;
  for (let n = 0; n < max; n++) {
    if (opts.signal?.aborted) throw new Error(`polling job ${id} was cancelled`);
    const job = await getJob(id, opts.fetcher);
    opts.onUpdate?.(job);
    if (job.state !== "running") return job;
    await sleep(opts.intervalMs ?? 1000);
  }
  throw new Error(
    `job ${id} still running after ${max} polls; the page stopped watching. Open #/backtests later to find its report.`,
  );
}

/** `#/chart?...&bt=<id>` for a finished job; the chart switches to the report's own instrument and timeframe. */
export function openOnChartHref(
  job: JobStatus,
  instrument: string,
  timeframe: string,
): string | null {
  if (job.state !== "done" || !job.backtest_id) return null;
  return chartHref({
    instrument: marketSymbol(instrument),
    ...(isTimeframe(timeframe) ? { tf: timeframe } : {}),
    bt: job.backtest_id,
  });
}

export const INSTRUMENTS = ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"] as const;

/** Whole days between two `YYYY-MM-DD` dates (null when either is not a date or end is not after start). */
export function windowDays(start: string, end: string): number | null {
  const a = Date.parse(`${start}T00:00:00Z`);
  const b = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) return null;
  return Math.round((b - a) / 86_400_000);
}
