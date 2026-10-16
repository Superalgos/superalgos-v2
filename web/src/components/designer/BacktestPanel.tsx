import { useEffect, useRef, useState } from "react";
import {
  type Fetcher,
  INSTRUMENTS,
  type JobStatus,
  openOnChartHref,
  pollJob,
  type RunOutcome,
  runBacktest,
  windowDays,
} from "../../lib/designer/backtest";
import type { Json } from "../../lib/designer/doc";
import type { Issue } from "../../lib/designer/errors";
import { rootSchema } from "../../lib/designer/schema";

const TIMEFRAMES = (rootSchema.properties?.timeframe?.enum ?? []) as string[];
const MAX_DAYS = 400;

export interface BacktestPanelProps {
  doc: Json;
  timeframe: string;
  /** Called with document problems the server reports for a run, so the tree can mark them. */
  onIssues: (issues: Issue[]) => void;
  onTimeframe: (tf: string) => void;
  /** Injected in tests. */
  fetcher?: Fetcher;
  pollMs?: number;
}

export function BacktestPanel({
  doc,
  timeframe,
  onIssues,
  onTimeframe,
  fetcher,
  pollMs,
}: BacktestPanelProps) {
  const [instrument, setInstrument] = useState<string>(INSTRUMENTS[0]);
  const [start, setStart] = useState("2025-01-01");
  const [end, setEnd] = useState("2025-02-01");
  const [outcome, setOutcome] = useState<RunOutcome | null>(null);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);

  const days = windowDays(start, end);
  const windowProblem =
    days === null
      ? "The end date must be after the start date (YYYY-MM-DD)."
      : days > MAX_DAYS
        ? `The window is ${days} days; the API allows at most ${MAX_DAYS}. Shorten it.`
        : null;

  const watch = async (id: string) => {
    abort.current?.abort();
    const ac = new AbortController();
    abort.current = ac;
    try {
      const final = await pollJob(id, {
        fetcher,
        intervalMs: pollMs,
        signal: ac.signal,
        onUpdate: setJob,
      });
      setJob(final);
    } catch (e) {
      if (!ac.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (abort.current === ac) setBusy(false);
    }
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    setJob(null);
    setOutcome(null);
    const out = await runBacktest({ strategy: doc, instrument, timeframe, start, end }, fetcher);
    setOutcome(out);
    if (out.kind === "started") {
      setJob(out.job);
      onIssues([]);
      await watch(out.job.id);
      return;
    }
    if (out.kind === "refused") onIssues(out.errors.filter((e) => e.path.startsWith("$")));
    setBusy(false);
  };

  const href = job ? openOnChartHref(job, instrument, timeframe) : null;
  return (
    <div className="bt">
      <h2>Backtest</h2>
      <p className="small muted">
        Runs this document on the local engine (loopback only, one job at a time). The timeframe is
        the document's.
      </p>
      <div className="pf">
        <label htmlFor="bt-inst">Instrument</label>
        <select id="bt-inst" value={instrument} onChange={(e) => setInstrument(e.target.value)}>
          {INSTRUMENTS.map((i) => (
            <option key={i}>{i}</option>
          ))}
        </select>
      </div>
      <div className="pf">
        <label htmlFor="bt-tf">Timeframe</label>
        <select id="bt-tf" value={timeframe} onChange={(e) => onTimeframe(e.target.value)}>
          {TIMEFRAMES.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </div>
      <div className="pf">
        <label htmlFor="bt-start">From</label>
        <input id="bt-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
      </div>
      <div className="pf">
        <label htmlFor="bt-end">To</label>
        <input id="bt-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
      </div>
      {windowProblem && <p className="fe-errors">{windowProblem}</p>}
      <button
        type="button"
        className="primary"
        disabled={busy || windowProblem !== null}
        onClick={run}
      >
        {busy ? "Running…" : "Run backtest"}
      </button>

      <div role="status" aria-live="polite">
        {outcome?.kind === "refused" && (
          <div className="error" data-status={outcome.status}>
            <strong>
              {outcome.status === 403 ? "Not allowed (403)" : `Refused (${outcome.status})`}
            </strong>
            <p>{outcome.reason}</p>
            {outcome.runningJob && (
              <button
                type="button"
                onClick={() => {
                  setBusy(true);
                  void watch(outcome.runningJob as string);
                }}
              >
                Follow the running job
              </button>
            )}
            {outcome.errors.length > 0 && (
              <ul>
                {outcome.errors.map((e) => (
                  <li key={`${e.path}:${e.column}:${e.message}`}>
                    <code>{e.path}</code>
                    {e.column !== null && ` col ${e.column}`}: {e.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {outcome?.kind === "unreachable" && <p className="error">{outcome.reason}</p>}
        {error && <p className="error">{error}</p>}
        {job && (
          <p>
            Job <code>{job.id}</code>: <strong>{job.state}</strong>
            {job.error && ` — ${job.error}`}
          </p>
        )}
        {href && (
          <p>
            <a className="bt-open" href={href}>
              Open on chart
            </a>
          </p>
        )}
      </div>
    </div>
  );
}
