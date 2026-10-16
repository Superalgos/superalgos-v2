import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { Backtest, Candle, EquityPoint, IndicatorResponse, Timeframe } from "../api/types";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { MarketChart } from "../components/MarketChart";
import {
  addSpec,
  backtestWindow,
  CANDLE_LIMIT,
  type ChartMarker,
  type ChartState,
  chartHref,
  debounce,
  defaultSpec,
  endMsFor,
  fillMarkers,
  INDICATOR_KINDS,
  type IndicatorKind,
  type IndicatorSpec,
  KIND_ORDER,
  marketSymbol,
  type PlannedIndicator,
  planPanes,
  removeSpec,
  signalMarkers,
  specError,
  specLabel,
  specString,
  TIMEFRAMES,
} from "../lib/market";
import { type Remote, useApi } from "../useApi";

const EMPTY_INDICATORS: IndicatorResponse = {};

interface ChartViewData {
  candles: Candle[];
  tf: Timeframe;
  plan: PlannedIndicator[];
  data: IndicatorResponse;
  markers: ChartMarker[];
  equity: EquityPoint[] | undefined;
  equityPane: number | null;
  label: string;
}

/**
 * The chart and its status. The MarketChart stays mounted across every change of what it shows;
 * while newer data loads (or fails) the last good view stays on screen with the problem inline.
 */
function ChartView({
  view,
  candles,
  indicators,
  loading,
  hasSpecs,
  height,
}: {
  view: ChartViewData | null;
  candles: Remote<Candle[]>;
  indicators: Remote<IndicatorResponse>;
  loading: boolean;
  hasSpecs: boolean;
  height: number;
}) {
  return (
    <>
      {candles.state === "error" && (
        <p role="alert" className="error">
          {candles.message}
          {view ? " Showing the last loaded chart." : ""}
        </p>
      )}
      {indicators.state === "error" && (
        <p role="alert" className="error">
          {indicators.message}
          {view ? " Showing the last loaded chart." : ""}
        </p>
      )}
      {!view && candles.state === "loading" && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {view && (
        <>
          {loading && hasSpecs && indicators.state === "loading" && (
            <p role="status" className="muted small">
              Loading indicators…
            </p>
          )}
          {/* no resetKey: the boundary clears only on its Retry button, never on data changes */}
          <ErrorBoundary label="The chart">
            <MarketChart
              candles={view.candles}
              tf={view.tf}
              plan={view.plan}
              data={view.data}
              markers={view.markers}
              equity={view.equity}
              equityPane={view.equityPane}
              height={height}
              label={view.label}
              loading={loading && candles.state !== "error" && indicators.state !== "error"}
            />
          </ErrorBoundary>
          {view.candles.length === 0 && (
            <p className="muted">The API returned no candles for this window.</p>
          )}
        </>
      )}
    </>
  );
}

function go(next: ChartState) {
  window.location.hash = chartHref(next);
}

function SymbolSearch({ value, onPick }: { value: string; onPick: (symbol: string) => void }) {
  const [text, setText] = useState(value);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => setText(value), [value]);
  const push = useMemo(() => debounce((q: string) => setQuery(q), 250), []);
  useEffect(() => () => push.cancel(), [push]);
  const results = useApi(() => api.symbols(query), query);
  const list = results.state === "ok" ? results.data.slice(0, 12) : [];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const typed = marketSymbol(text);
    const hit = list.find((s) => s.instrument === typed) ?? list[0];
    if (hit) {
      onPick(hit.instrument);
      setOpen(false);
    }
  };

  return (
    <form className="symsearch" onSubmit={submit} role="search" aria-label="Instrument search">
      <label htmlFor="sym">Instrument</label>
      <input
        id="sym"
        type="search"
        autoComplete="off"
        value={text}
        placeholder="Search e.g. ETH"
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          push(e.target.value);
        }}
      />
      {open && (
        <ul className="symlist" aria-label="Matching instruments">
          {results.state === "loading" && <li className="muted small">Searching…</li>}
          {results.state === "error" && (
            <li role="alert" className="error small">
              {results.message}
            </li>
          )}
          {results.state === "ok" && list.length === 0 && (
            <li className="muted small">No Binance spot instrument matches "{query}".</li>
          )}
          {list.map((s) => (
            <li key={s.instrument}>
              <button
                type="button"
                onClick={() => {
                  onPick(s.instrument);
                  setOpen(false);
                }}
              >
                <b>{s.instrument}</b>{" "}
                <span className="muted small">
                  {s.base}/{s.quote}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}

function IndicatorPanel({
  specs,
  onChange,
}: {
  specs: IndicatorSpec[];
  onChange: (next: IndicatorSpec[]) => void;
}) {
  const [kind, setKind] = useState<IndicatorKind>("ema");
  const [params, setParams] = useState<string[]>(() => defaultSpec("ema").params.map(String));
  const [error, setError] = useState<string | null>(null);
  const defs = INDICATOR_KINDS[kind].params;

  const pickKind = (k: IndicatorKind) => {
    setKind(k);
    setParams(defaultSpec(k).params.map(String));
    setError(null);
  };
  const add = (e: FormEvent) => {
    e.preventDefault();
    const spec: IndicatorSpec = {
      kind,
      params: params.map((p) => (p.trim() === "" ? Number.NaN : Number(p))),
    };
    const err = specError(spec);
    setError(err);
    if (!err) onChange(addSpec(specs, spec));
  };

  return (
    <fieldset className="indpanel">
      <legend>Indicators</legend>
      <form onSubmit={add} className="indform">
        <label>
          Type
          <select value={kind} onChange={(e) => pickKind(e.target.value as IndicatorKind)}>
            {KIND_ORDER.map((k) => (
              <option key={k} value={k}>
                {INDICATOR_KINDS[k].label} ({INDICATOR_KINDS[k].pane ? "pane" : "price"})
              </option>
            ))}
          </select>
        </label>
        {defs.map((d, i) => (
          <label key={d.name}>
            {d.name}
            <input
              type="number"
              inputMode="decimal"
              step={d.integer ? 1 : "any"}
              min={d.min}
              max={d.max}
              value={params[i] ?? ""}
              onChange={(e) => setParams(params.map((p, j) => (j === i ? e.target.value : p)))}
            />
          </label>
        ))}
        <button type="submit">Add</button>
      </form>
      {error && (
        <p role="alert" className="error small">
          {error}
        </p>
      )}
      {specs.length === 0 ? (
        <p className="muted small">None. Values come from the API, computed by the engine.</p>
      ) : (
        <ul className="chips">
          {specs.map((s) => (
            <li key={specString(s)}>
              {specLabel(s)}{" "}
              <button
                type="button"
                className="linkbtn"
                aria-label={`Remove ${specLabel(s)}`}
                onClick={() => onChange(removeSpec(specs, s))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  );
}

export function ChartPage({ state }: { state: ChartState }) {
  const { instrument, tf, ind } = state;
  const providers = useApi(api.providers);
  const backtests = useApi(api.backtests);
  const signals = useApi(
    () => (state.provider ? api.signals(state.provider, 200) : Promise.resolve([])),
    state.provider ?? "",
  );
  const bt = useApi<Backtest | null>(
    () => (state.bt ? api.backtest(state.bt) : Promise.resolve(null)),
    state.bt ?? "",
  );

  const btData = bt.state === "ok" ? bt.data : null;
  const btMatches =
    !!btData && marketSymbol(btData.instrument) === instrument && btData.timeframe === tf;

  // the candle window: the backtest's dates, else around the provider's signals, else the latest candles
  const window_ = useMemo(() => {
    if (btMatches && btData) return backtestWindow(btData);
    if (signals.state === "ok") {
      const times = signals.data
        .filter((s) => marketSymbol(s.instrument) === instrument)
        .map((s) => s.created_ms);
      const endMs = endMsFor(times, tf);
      if (endMs !== undefined) return { endMs, limit: CANDLE_LIMIT };
    }
    return { endMs: undefined, limit: CANDLE_LIMIT };
  }, [btMatches, btData, signals, instrument, tf]);

  const query = {
    instrument,
    tf,
    limit: window_.limit,
    ...(window_.endMs !== undefined ? { endMs: window_.endMs } : {}),
  };
  const winKey = `${instrument}|${tf}|${window_.endMs}|${window_.limit}`;
  const candles = useApi(() => api.candles(query), winKey);
  const specs = ind.map(specString);
  const indicators = useApi<IndicatorResponse>(
    () => (specs.length ? api.indicators(query, specs) : Promise.resolve({})),
    `${winKey}|${specs.join(";")}`,
  );

  const provider =
    providers.state === "ok" ? providers.data.find((p) => p.pubkey === state.provider) : undefined;
  const sigMarkers = useMemo(
    () =>
      signals.state === "ok" && state.provider ? signalMarkers(signals.data, instrument, tf) : [],
    [signals, state.provider, instrument, tf],
  );
  const fillMarks = useMemo(
    () => (btMatches && btData ? fillMarkers(btData.fills, tf) : []),
    [btMatches, btData, tf],
  );
  const markers = useMemo(() => [...sigMarkers, ...fillMarks], [sigMarkers, fillMarks]);
  const plan = useMemo(() => planPanes(ind, btMatches), [ind, btMatches]);

  const otherInstruments =
    signals.state === "ok"
      ? [...new Set(signals.data.map((s) => marketSymbol(s.instrument)))].filter(
          (i) => i !== instrument,
        )
      : [];
  const narrow = typeof window !== "undefined" && window.innerWidth < 600;
  const height = (narrow ? 340 : 460) + 120 * (plan.paneCount - 1);

  // Stale-while-loading: the chart shows the last complete view until the next one has fully
  // arrived (candles, indicators, and the backtest / signals the markers come from).
  const held = useRef<ChartViewData | null>(null);
  const candleData = candles.state === "ok" ? candles.data : null;
  const indicatorsDone =
    indicators.state === "ok" || (indicators.state === "error" && held.current === null);
  const settled =
    candleData !== null && indicatorsDone && bt.state !== "loading" && signals.state !== "loading";
  const indicatorData = indicators.state === "ok" ? indicators.data : EMPTY_INDICATORS;
  const equity = btMatches && btData ? btData.equity : undefined;
  const label = `${instrument} ${tf} candles${provider ? ` with ${provider.name} signals` : ""}${btMatches && btData ? ` and ${btData.strategy_name} fills` : ""}`;
  const fresh = useMemo<ChartViewData | null>(
    () =>
      settled && candleData
        ? {
            candles: candleData,
            tf,
            plan: plan.items,
            data: indicatorData,
            markers,
            equity,
            equityPane: plan.equityPane,
            label,
          }
        : null,
    [settled, candleData, tf, plan, indicatorData, markers, equity, label],
  );
  if (fresh) held.current = fresh;
  const view = fresh ?? held.current;

  return (
    <>
      <h1>Market chart</h1>
      <p className="lede">
        Binance spot candles. Indicator values are computed by the engine and fetched from the API;
        nothing runs on a third-party platform.
      </p>
      <div className="chartbar">
        <SymbolSearch value={instrument} onPick={(i) => go({ ...state, instrument: i })} />
        <div role="group" aria-label="Timeframe" className="tfs">
          {TIMEFRAMES.map((t) => (
            <button
              key={t}
              type="button"
              aria-pressed={t === tf}
              onClick={() => go({ ...state, tf: t })}
            >
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="chartbar">
        <label>
          Signals from
          <select
            value={state.provider ?? ""}
            onChange={(e) => {
              const { provider: _p, ...rest } = state;
              go(e.target.value ? { ...rest, provider: e.target.value } : rest);
            }}
          >
            <option value="">No provider</option>
            {providers.state === "ok" &&
              providers.data.map((p) => (
                <option key={p.pubkey} value={p.pubkey}>
                  {p.name}
                </option>
              ))}
          </select>
        </label>
        <label>
          Backtest
          <select
            value={state.bt ?? ""}
            onChange={(e) => {
              const { bt: _b, ...rest } = state;
              const chosen =
                backtests.state === "ok"
                  ? backtests.data.find((b) => b.id === e.target.value)
                  : undefined;
              go(
                chosen
                  ? {
                      ...rest,
                      bt: chosen.id,
                      instrument: marketSymbol(chosen.instrument),
                      tf: chosen.timeframe,
                    }
                  : rest,
              );
            }}
          >
            <option value="">No backtest</option>
            {backtests.state === "ok" &&
              backtests.data.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.strategy_name} ({marketSymbol(b.instrument)} {b.timeframe})
                </option>
              ))}
          </select>
        </label>
      </div>
      <IndicatorPanel specs={ind} onChange={(next) => go({ ...state, ind: next })} />
      {state.provider && signals.state === "ok" && sigMarkers.length === 0 && (
        <p role="note" className="warnbox">
          {provider?.name ?? "This provider"} has no signals on {instrument} in the loaded history.
          {otherInstruments.length > 0 && (
            <>
              {" "}
              Its signals are on{" "}
              {otherInstruments.map((i, n) => (
                <span key={i}>
                  {n > 0 && ", "}
                  <a href={chartHref({ ...state, instrument: i })}>{i}</a>
                </span>
              ))}
              .
            </>
          )}
        </p>
      )}
      {btData && !btMatches && (
        <p role="note" className="warnbox">
          {btData.strategy_name} ran on {marketSymbol(btData.instrument)} {btData.timeframe}.{" "}
          <a
            href={chartHref({
              ...state,
              instrument: marketSymbol(btData.instrument),
              tf: btData.timeframe,
            })}
          >
            Switch to it
          </a>{" "}
          to see its fills.
        </p>
      )}
      <ChartView
        view={view}
        candles={candles}
        indicators={indicators}
        loading={!settled}
        hasSpecs={specs.length > 0}
        height={height}
      />
    </>
  );
}
