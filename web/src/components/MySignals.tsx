import { api } from "../api/client";
import type { Provider } from "../api/types";
import { fmtTime, shortKey } from "../lib/format";
import { providerHref } from "../lib/route";
import { outcomeLabel, tierDelayLabel } from "../lib/signals";
import { LIVE, useApi } from "../useApi";
import { Show } from "./Remote";

/**
 * The follower's own view: the signals its follower received, when, the delay its own tier schedules, and whether
 * it executed or rejected each (with the reason). Read from that follower's ledger only; no other follower appears.
 */
export function MySignals({ providers }: { providers: Provider[] }) {
  const mine = useApi(() => api.mySignals(50), "", LIVE);
  return (
    <>
      <h2>My signals</h2>
      <p className="lede">
        Signals your follower received, newest first. The delay is what your tier schedules before
        you get a signal.
      </p>
      <Show remote={mine}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="muted">No signals received yet.</p>
          ) : (
            <div className="scroll">
              <table>
                <caption className="sr">Signals received by this follower</caption>
                <thead>
                  <tr>
                    <th scope="col">Received</th>
                    <th scope="col">Provider</th>
                    <th scope="col">Signal</th>
                    <th scope="col">Your tier</th>
                    <th scope="col">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const p = providers.find((x) => x.pubkey === r.provider);
                    return (
                      <tr key={`${r.id}-${r.received_ms}`}>
                        <td>{fmtTime(r.received_ms)}</td>
                        <td>
                          {p ? (
                            <a href={providerHref(r.provider)}>{p.name}</a>
                          ) : (
                            shortKey(r.provider, 10, 6)
                          )}
                        </td>
                        <td>
                          {r.action && r.side
                            ? `${r.action} ${r.side} ${r.instrument ?? ""}`
                            : "n/a"}
                          {r.seq !== null && <span className="muted"> #{r.seq}</span>}
                        </td>
                        <td>{tierDelayLabel(r)}</td>
                        <td
                          className={
                            r.outcome === "executed" ? "pos" : r.outcome === "rejected" ? "neg" : ""
                          }
                        >
                          {outcomeLabel(r.outcome, r.reason)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        }
      </Show>
    </>
  );
}
