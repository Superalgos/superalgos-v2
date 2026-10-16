import type { CSSProperties } from "react";
import type { DevSignal, Provider } from "../api/types";
import { fmtDuration, fmtTime } from "../lib/format";
import {
  buildRows,
  describeTimeScale,
  resolveTimeScale,
  tierSchedule,
  trackSpanMs,
  trackTicks,
} from "../lib/timeline";

/**
 * One track per signal; one marker per tier at its scheduled offset (delay_fraction x timeframe).
 * The track spans at least the largest delay. Coinciding markers stack in lanes. Real receipt times
 * (when the API reports them) are drawn as ringed markers, with a tick where they fall on the schedule.
 */
export function TierTimeline({
  provider,
  signals,
  timeScale,
}: {
  provider: Provider;
  signals: DevSignal[];
  timeScale?: number | null;
}) {
  const scale = resolveTimeScale(provider, signals, timeScale);
  const rows = buildRows(provider, signals, scale);
  const schedule = tierSchedule(provider);
  const ticks = trackTicks(provider);
  const scaled = describeTimeScale(scale);
  const span = trackSpanMs(provider);
  if (rows.length === 0) return <p className="muted">No signals published yet.</p>;
  return (
    <div className="timeline">
      {scaled && (
        <p role="note" className="warnbox">
          Demo replay: the relay runs {scaled} (time scale{" "}
          {scale < 1 ? `1/${Math.round(1 / scale)}` : scale}). Markers sit at the scheduled offset
          (delay fraction x timeframe, in market time); hover a marker for the wall-clock receive
          time, which is {scale < 1 ? "shorter" : "longer"} by that factor.
        </p>
      )}
      <ul className="tierkey" aria-label="Tier schedule">
        {schedule.map((t) => (
          <li key={t.tier}>
            <span className={`mk t${t.tier % 5}`} aria-hidden="true">
              {t.tier + 1}
            </span>
            Tier {t.tier + 1} ({t.label}) receives after {fmtDuration(t.delayMs)}
            {scaled && ` (${fmtDuration(t.delayMs * scale)} on the wall clock)`}
          </li>
        ))}
        <li>
          <span className="mk t0 obs" aria-hidden="true">
            n
          </span>
          Ringed marker: a follower's real receipt time. Plain marker: scheduled time.
        </li>
      </ul>
      <ol className="sigrows">
        {rows.map(({ signal: s, markers, lanes, anchorLagMs }) => (
          <li key={s.id}>
            <div className="sighead">
              <span className="num">#{s.seq}</span>
              <span className={s.side === "buy" ? "pos" : "neg"}>
                {s.action} {s.side}
              </span>
              <span>{s.instrument}</span>
              <span className="num muted">{Math.round(s.size_fraction * 100)}% size</span>
              {s.private && <span className="tag">private</span>}
              <time className="muted" dateTime={new Date(s.created_ms).toISOString()}>
                {fmtTime(s.created_ms)}
              </time>
              <span className="muted">
                {anchorLagMs === null ? "not anchored" : `anchored +${fmtDuration(anchorLagMs)}`}
              </span>
            </div>
            <div className="track" style={{ "--lanes": lanes } as CSSProperties}>
              <span className="origin" aria-hidden="true" />
              {markers.map((m) => (
                <span key={`r${m.tier}`}>
                  {m.receivedPosition !== null && (
                    <span
                      className={`rx t${m.tier % 5}`}
                      style={{ left: `${m.receivedPosition * 100}%` }}
                      aria-hidden="true"
                    />
                  )}
                  <span
                    className={`mk t${m.tier % 5}${m.observed ? " obs" : ""}`}
                    style={{ left: `${m.position * 100}%`, "--lane": m.lane } as CSSProperties}
                    title={`Tier ${m.tier + 1} ${m.observed ? "received" : "scheduled"} ${fmtTime(m.atMs)} (+${fmtDuration(m.wallDelayMs)} wall clock; schedule +${fmtDuration(m.scheduledMs)})`}
                  >
                    {m.tier + 1}
                  </span>
                </span>
              ))}
            </div>
            <div className="trackaxis" aria-hidden="true">
              {ticks.map((t) => (
                <span key={t.ms} style={{ left: `${t.position * 100}%` }}>
                  {t.ms === 0 ? "0" : fmtDuration(t.ms)}
                </span>
              ))}
            </div>
            <p className="sr">
              {markers
                .map(
                  (m) =>
                    `Tier ${m.tier + 1} ${m.observed ? "received" : "scheduled"} at plus ${fmtDuration(m.scheduledMs)}`,
                )
                .join("; ")}
            </p>
          </li>
        ))}
      </ol>
      <p className="muted axis">
        Track spans {fmtDuration(span)} from signal creation
        {span > provider.timeframe_s * 1000
          ? `, longer than one timeframe (${fmtDuration(provider.timeframe_s * 1000)}) because a tier waits more than that`
          : ` (one timeframe)`}
        ; left edge is publish time.
      </p>
    </div>
  );
}
