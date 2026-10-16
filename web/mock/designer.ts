import { isObject, type Json } from "../src/lib/designer/doc";
import type { Issue } from "../src/lib/designer/errors";
import { itemsOf, kindOf, propsOf, resolve, rootSchema } from "../src/lib/designer/schema";
import { structureProblems } from "../src/lib/designer/structure";
import { backtests } from "./fixture";
import type { RouteResult } from "./routes";

/** Names the engine's expression language knows (from the schema description). */
const FUNCTIONS = new Set(
  "ema sma rsi bb_upper bb_middle bb_lower atr donchian_upper donchian_lower donchian_mid prev cross_above cross_below abs min max coalesce open high low close volume".split(
    " ",
  ),
);
const NAMES = new Set(
  "open high low close volume in_position is_long is_short position_size entry_price unrealized_pnl_pct bars_in_position stop_price target_price true false and or not".split(
    " ",
  ),
);

/**
 * A small stand-in for the engine's expression parser, so the designer shows column errors without Python:
 * unbalanced parentheses, stray characters, unknown names and a dangling operator.
 */
export function checkExpression(src: string): { column: number; message: string } | null {
  const stack: number[] = [];
  const re = /\s+|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|"[^"]*"|[()]|[+\-*/,<>=!]+|./gy;
  let last: { text: string; at: number } | null = null;
  let m: RegExpExecArray | null = re.exec(src);
  while (m !== null) {
    const t = m[0];
    const at = m.index;
    m = re.exec(src);
    if (/^\s+$/.test(t)) continue;
    if (/^[A-Za-z_]/.test(t)) {
      const call = src[at + t.length] === "(";
      if (call && !FUNCTIONS.has(t)) return { column: at, message: `unknown function '${t}'` };
      if (!call && !NAMES.has(t)) return { column: at, message: `unknown name '${t}'` };
    } else if (t === "(") stack.push(at);
    else if (t === ")") {
      if (stack.pop() === undefined) return { column: at, message: "unmatched ')'" };
    } else if (!/^(\d|"|[+\-*/,<>=!]+$)/.test(t)) {
      return { column: at, message: `unexpected character '${t}'` };
    }
    last = { text: t, at };
  }
  const open = stack[0];
  if (open !== undefined) return { column: open, message: "unclosed '('" };
  if (last && /^[+\-*/,<>=!]+$/.test(last.text))
    return {
      column: src.length,
      message: `expression ends after '${last.text}'; expected a value`,
    };
  return null;
}

/** Same JSON shape as `POST /api/strategy/validate`: structure first, expressions once the structure is sound. */
export function validateDocument(body: unknown): { ok: boolean; errors: Issue[] } {
  if (!isObject(body)) {
    return {
      ok: false,
      errors: [{ path: "$", column: null, message: "a strategy document must be a JSON object" }],
    };
  }
  const errors = structureProblems(body as Json);
  if (errors.length === 0) {
    const walk = (v: Json, r: ReturnType<typeof resolve>, path: string) => {
      const kind = kindOf(r);
      if (kind === "dsl" && typeof v === "string") {
        const e = checkExpression(v);
        if (e) errors.push({ path, column: e.column, message: e.message });
      } else if (kind === "object" && isObject(v)) {
        for (const p of propsOf(r)) {
          const c = v[p.key];
          if (c !== undefined && c !== null) walk(c, p.resolved, `${path}.${p.key}`);
        }
      } else if (kind === "array" && Array.isArray(v)) {
        const item = itemsOf(r);
        if (item) v.forEach((x, i) => walk(x, item, `${path}[${i}]`));
      }
    };
    walk(body as Json, resolve(rootSchema), "$");
  }
  return { ok: errors.length === 0, errors };
}

interface Job {
  id: string;
  startedMs: number;
  backtestId: string;
}

/** How long a mock job "runs" before it is done. */
export const MOCK_JOB_MS = 1500;
const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"];
const INSTRUMENTS = ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"];

/** The designer's two POST routes and the job poll, stateful (one job at a time, like the real API). */
export function createDesignerMock(opts: { now?: () => number; jobMs?: number } = {}) {
  const now = opts.now ?? Date.now;
  const jobMs = opts.jobMs ?? MOCK_JOB_MS;
  const jobs = new Map<string, Job>();
  let seq = 0;

  const status = (j: Job) => {
    const done = now() - j.startedMs >= jobMs;
    return {
      id: j.id,
      state: done ? "done" : "running",
      started_ms: j.startedMs,
      finished_ms: done ? j.startedMs + jobMs : null,
      backtest_id: done ? j.backtestId : null,
      error: null,
    };
  };

  /** `null` when the request is not one of these routes. */
  return function handle(
    method: string,
    pathname: string,
    body: unknown,
    headers: Headers = new Headers(),
  ): RouteResult | null {
    if (method === "POST" && pathname === "/api/strategy/validate") {
      return { status: 200, body: validateDocument(body) };
    }
    if (method === "POST" && pathname === "/api/backtests/run") {
      if (headers.has("x-forwarded-for") || headers.has("forwarded")) {
        return {
          status: 403,
          body: {
            error:
              "backtests are allowed only on a loopback-bound API: requests that come through a proxy are refused; run backtests from the machine itself",
          },
        };
      }
      const running = [...jobs.values()].find((j) => status(j).state === "running");
      if (running) {
        return {
          status: 409,
          body: {
            error: `job ${running.id} is still running; wait for it to finish`,
            job: running.id,
          },
        };
      }
      const b = isObject(body) ? body : {};
      const errors: Issue[] = [];
      const bad = (field: string, message: string) =>
        errors.push({ path: `request.${field}`, column: null, message });
      if (typeof b.instrument !== "string" || !INSTRUMENTS.includes(b.instrument))
        bad("instrument", `must be one of ${INSTRUMENTS.join(", ")}`);
      if (typeof b.start !== "string" || Number.isNaN(Date.parse(b.start)))
        bad("start", "must be a YYYY-MM-DD date");
      if (typeof b.end !== "string" || Number.isNaN(Date.parse(b.end)))
        bad("end", "must be a YYYY-MM-DD date");
      else if (typeof b.start === "string" && Date.parse(b.end) <= Date.parse(b.start))
        bad("end", "must be after start");
      if (typeof b.timeframe === "string" && !TIMEFRAMES.includes(b.timeframe))
        bad("timeframe", "unknown timeframe");
      const v = validateDocument(b.strategy);
      errors.unshift(...v.errors);
      if (errors.length > 0) {
        return {
          status: 422,
          body: { error: `invalid backtest request: ${errors.length} problem(s)`, errors },
        };
      }
      const id = `job${(++seq).toString(16).padStart(6, "0")}`;
      const job = {
        id,
        startedMs: now(),
        backtestId: backtests[(seq - 1) % backtests.length]?.id ?? "",
      };
      jobs.set(id, job);
      return { status: 202, body: status(job) };
    }
    const m = /^\/api\/backtests\/jobs\/([^/]+)$/.exec(pathname);
    if (method === "GET" && m) {
      const job = jobs.get(decodeURIComponent(m[1] as string));
      return job
        ? { status: 200, body: status(job) }
        : {
            status: 404,
            body: { error: `job not found: ${m[1]}; jobs are lost when the API restarts` },
          };
    }
    return null;
  };
}
