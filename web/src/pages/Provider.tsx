import { api, exportUrl } from "../api/client";
import { Badges } from "../components/Badges";
import { EquityChart } from "../components/EquityChart";
import { ProviderChart } from "../components/ProviderChart";
import { Show } from "../components/Remote";
import { Stat } from "../components/Stat";
import { fmtFraction, fmtNum, fmtPct, fmtTime, shortKey, signClass } from "../lib/format";
import { signalStatusLabel } from "../lib/signals";
import { anchorPenaltyNote, signalCount } from "../lib/verification";
import { LIVE, useApi } from "../useApi";

export function ProviderPage({ pubkey }: { pubkey: string }) {
  const providers = useApi(api.providers, "", LIVE);
  const record = useApi(() => api.record(pubkey), pubkey, LIVE);
  const signals = useApi(() => api.signals(pubkey, 30), pubkey, LIVE);
  const network = useApi(api.network, "", LIVE);
  return (
    <>
      <p>
        <a href="#/">← All providers</a>
      </p>
      <Show remote={providers}>
        {(all) => {
          const p = all.find((x) => x.pubkey === pubkey);
          if (!p) {
            return (
              <p role="alert" className="error">
                No provider with key {shortKey(pubkey, 12, 6)}. Go back to the leaderboard and pick
                one.
              </p>
            );
          }
          const s = p.record_summary;
          return (
            <>
              <h1>{p.name}</h1>
              <p className="muted small">
                <code title={p.pubkey}>{shortKey(p.pubkey, 10, 6)}</code> · SA{" "}
                <code title={p.sa_address}>{shortKey(p.sa_address, 8, 6)}</code> · {p.mode}
              </p>
              <dl className="stats">
                <Stat label="PnL" tone={signClass(s.pnl_pct)}>
                  {fmtPct(s.pnl_pct, 1, true)}
                </Stat>
                <Stat label="Win rate">{fmtFraction(s.win_rate)}</Stat>
                <Stat label="Max drawdown">{fmtPct(s.max_dd_pct)}</Stat>
                <Stat label="Trades">{s.trades}</Stat>
                <Stat label="Followers">{p.followers}</Stat>
                <Stat label="Completeness">{fmtFraction(s.completeness)}</Stat>
                <Stat label="Anchored on time">{fmtFraction(s.anchor_coverage)}</Stat>
                <Stat label="Late anchors" tone={s.late_anchor > 0 ? "neg" : undefined}>
                  {s.late_anchor}
                </Stat>
                <Stat label="Unanchored" tone={s.unanchored > 0 ? "neg" : undefined}>
                  {s.unanchored}
                </Stat>
                <Stat label="Excluded">{s.excluded}</Stat>
              </dl>
              {anchorPenaltyNote(s) && (
                <p role="note" className="warnbox">
                  {anchorPenaltyNote(s)}
                </p>
              )}
              <h2>Signals on the chart</h2>
              <Show remote={network}>
                {(n) => (
                  <Show remote={signals}>
                    {(sig) => (
                      <ProviderChart provider={p} signals={sig} live={n.time_scale === 1} />
                    )}
                  </Show>
                )}
              </Show>
              <h2>Signals</h2>
              <p className="lede">
                "Out" means the signal is signed, time-stamped and on the relay. Otherwise the
                missing step is named.
              </p>
              <Show remote={signals}>
                {(sig) =>
                  sig.length === 0 ? (
                    <p className="muted">No signals published yet.</p>
                  ) : (
                    <div className="scroll">
                      <table>
                        <caption className="sr">Signals and their status</caption>
                        <thead>
                          <tr>
                            <th scope="col" className="num">
                              #
                            </th>
                            <th scope="col">Created</th>
                            <th scope="col">Signal</th>
                            <th scope="col">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sig.map((s) => (
                            <tr key={s.id}>
                              <td className="num">{s.seq}</td>
                              <td>{fmtTime(s.created_ms)}</td>
                              <td>
                                <span className={s.side === "buy" ? "pos" : "neg"}>
                                  {s.action} {s.side}
                                </span>{" "}
                                {s.instrument} · {Math.round(s.size_fraction * 100)}% size
                                {s.private && <span className="tag">private</span>}
                              </td>
                              <td className={s.status === "out" ? "pos" : "neg"}>
                                {signalStatusLabel(s.status)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )
                }
              </Show>
              <h2>Export signals</h2>
              <p className="lede">
                The signed history of this provider: the bundle is what <code>sa verify</code>{" "}
                accepts; the CSV is the same signals as a table.
              </p>
              <p className="actions">
                <a className="btn" href={exportUrl(p.pubkey, "bundle")} download>
                  Export signals (bundle JSON)
                </a>
                <a className="btn" href={exportUrl(p.pubkey, "csv")} download>
                  Export signals (CSV)
                </a>
              </p>
              <h2>Track record</h2>
              <Show remote={record}>
                {(r) => (
                  <>
                    <Badges
                      v={r.verification}
                      signals={signalCount(signals.state === "ok" ? signals.data.length : null, s)}
                    />
                    <EquityChart points={r.equity} title={`${p.name} equity`} />
                    {r.gaps.length > 0 && (
                      <p role="note" className="warnbox">
                        Chain gaps:{" "}
                        {r.gaps.map((g) => `${g.missing} missing after #${g.after_seq}`).join("; ")}
                        . Gaps lower completeness.
                      </p>
                    )}
                    {r.unrevealed_commitments.length > 0 && (
                      <p role="note" className="warnbox">
                        {r.unrevealed_commitments.length} commitment
                        {r.unrevealed_commitments.length === 1 ? "" : "s"} never revealed; counted
                        as misses.
                      </p>
                    )}
                    <h3>Trades</h3>
                    {r.trades.length === 0 && <p className="muted">No closed trades yet.</p>}
                    <div className="scroll" hidden={r.trades.length === 0}>
                      <table>
                        <caption className="sr">Closed trades</caption>
                        <thead>
                          <tr>
                            <th scope="col">Opened</th>
                            <th scope="col">Side</th>
                            <th scope="col" className="num">
                              Entry
                            </th>
                            <th scope="col" className="num">
                              Exit
                            </th>
                            <th scope="col" className="num">
                              PnL
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {r.trades.map((t) => (
                            <tr key={`${t.open_t}-${t.close_t}`}>
                              <td>{fmtTime(t.open_t)}</td>
                              <td>{t.side}</td>
                              <td className="num">{fmtNum(t.entry, 2)}</td>
                              <td className="num">{fmtNum(t.exit, 2)}</td>
                              <td className={`num ${signClass(t.pnl_pct)}`}>
                                {fmtPct(t.pnl_pct, 2, true)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </Show>
            </>
          );
        }}
      </Show>
    </>
  );
}
