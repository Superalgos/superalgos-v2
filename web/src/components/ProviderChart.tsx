import { useMemo, useRef } from "react";
import { api } from "../api/client";
import type { Candle, Provider, Signal } from "../api/types";
import {
  chartHref,
  dominantInstrument,
  fitTimeframe,
  marketSymbol,
  providerChartEndMs,
  signalMarkers,
} from "../lib/market";
import { LIVE, useApi } from "../useApi";
import { MarketChart } from "./MarketChart";

/**
 * Mini chart of the provider's main instrument with its signals as markers, and a link to the full chart.
 * `live` (the relay runs in real time): the chart reaches the current time and re-reads its candles with the
 * page's polling. Not live (a time-scaled demo replay): the window ends just after the latest signal, as before.
 * `now` is the clock, injectable for tests.
 */
export function ProviderChart({
  provider,
  signals,
  live,
  now = Date.now,
}: {
  provider: Provider;
  signals: Signal[];
  live: boolean;
  now?: () => number;
}) {
  const instrument = dominantInstrument(signals);
  const own = useMemo(
    () => signals.filter((s) => instrument !== null && marketSymbol(s.instrument) === instrument),
    [signals, instrument],
  );
  const times = useMemo(() => own.map((s) => s.created_ms), [own]);
  const tf = fitTimeframe(provider.timeframe_s, times);
  // Replay: the window follows the latest signal, so a new signal changes the key. Live: the end is "now", read at
  // every fetch, so the key stays put and each poll just reloads the candles.
  const replayEnd = live ? undefined : providerChartEndMs(times, tf, { live: false, now: 0 });
  const key = `${instrument}|${tf}|${live ? "live" : replayEnd}`;
  const candles = useApi(
    () => {
      if (!instrument) return Promise.resolve([]);
      const endMs = live ? providerChartEndMs(times, tf, { live: true, now: now() }) : replayEnd;
      return api.candles({ instrument, tf, ...(endMs !== undefined ? { endMs } : {}), limit: 120 });
    },
    key,
    LIVE,
  );
  // In replay a new signal can move the end, hence the key. Keep drawing the previous candles of the same
  // instrument and timeframe (dimmed, "Loading…") instead of tearing the chart down: the hover and
  // zoom state live in it, and the page must not blank on a background refresh.
  const shown = useRef<{ instrument: string | null; tf: string; data: Candle[] } | null>(null);
  if (candles.state === "ok") shown.current = { instrument, tf, data: candles.data };
  const held = shown.current?.instrument === instrument && shown.current.tf === tf;
  const markers = useMemo(
    () => (instrument ? signalMarkers(own, instrument, tf) : []),
    [own, instrument, tf],
  );
  if (!instrument) return <p className="muted">No signals yet, so nothing to draw on a chart.</p>;
  return (
    <>
      <p className="small">
        {instrument} · {tf} · {markers.length} signal{markers.length === 1 ? "" : "s"} · hover a
        candle with a marker for details ·{" "}
        <a href={chartHref({ instrument, tf, provider: provider.pubkey })}>Open full chart</a>
      </p>
      {candles.state === "error" && !held && (
        <p role="alert" className="error">
          {candles.message}
        </p>
      )}
      {candles.state === "loading" && !held && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {shown.current && held && (
        <MarketChart
          candles={shown.current.data}
          tf={tf}
          markers={markers}
          height={300}
          loading={candles.state === "loading"}
          label={`${provider.name} signals on ${instrument} ${tf}`}
        />
      )}
    </>
  );
}
