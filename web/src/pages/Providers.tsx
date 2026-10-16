import { useState } from "react";
import { api } from "../api/client";
import { Badges } from "../components/Badges";
import { Show } from "../components/Remote";
import { fmtCompact, fmtFraction, fmtPct, fmtTimeframe, signClass } from "../lib/format";
import { providerHref } from "../lib/route";
import {
  type SortKey,
  signalsFromSummary,
  sortProviders,
  verificationFromSummary,
} from "../lib/verification";
import { LIVE, useApi } from "../useApi";

const COLS: { key: SortKey; label: string }[] = [
  { key: "pnl_pct", label: "PnL" },
  { key: "win_rate", label: "Win rate" },
  { key: "max_dd_pct", label: "Max DD" },
  { key: "trades", label: "Trades" },
  { key: "followers", label: "Followers" },
  { key: "power_backing", label: "Power" },
];

export function ProvidersPage() {
  const remote = useApi(api.providers, "", LIVE);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "pnl_pct", desc: true });
  return (
    <>
      <h1>Providers</h1>
      <p className="lede">
        Every figure is recomputed from signed, anchored signals. Badges show what was verified.
      </p>
      <Show remote={remote}>
        {(rows) => (
          <div className="scroll">
            <table>
              <caption className="sr">Provider leaderboard, sorted by {sort.key}</caption>
              <thead>
                <tr>
                  <th scope="col">Provider</th>
                  <th scope="col">Verified</th>
                  {COLS.map((c) => (
                    <th
                      key={c.key}
                      scope="col"
                      className="num"
                      aria-sort={
                        sort.key === c.key ? (sort.desc ? "descending" : "ascending") : "none"
                      }
                    >
                      <button
                        type="button"
                        className="sortbtn"
                        onClick={() =>
                          setSort((s) => ({ key: c.key, desc: s.key === c.key ? !s.desc : true }))
                        }
                      >
                        {c.label}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sortProviders(rows, sort.key, sort.desc).map((p) => (
                  <tr key={p.pubkey}>
                    <th scope="row">
                      <a href={providerHref(p.pubkey)}>{p.name}</a>
                      <div className="muted small">
                        {p.mode} · {fmtTimeframe(p.timeframe_s)} · {p.tiers.length} tier
                        {p.tiers.length === 1 ? "" : "s"}
                      </div>
                    </th>
                    <td>
                      <Badges
                        v={verificationFromSummary(p.record_summary)}
                        signals={signalsFromSummary(p.record_summary)}
                      />
                    </td>
                    <td className={`num ${signClass(p.record_summary.pnl_pct)}`}>
                      {fmtPct(p.record_summary.pnl_pct, 1, true)}
                    </td>
                    <td className="num">{fmtFraction(p.record_summary.win_rate)}</td>
                    <td className="num">{fmtPct(p.record_summary.max_dd_pct)}</td>
                    <td className="num">{p.record_summary.trades}</td>
                    <td className="num">{p.followers}</td>
                    <td className="num">{fmtCompact(p.power_backing)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Show>
    </>
  );
}
