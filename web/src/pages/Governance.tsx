import { api } from "../api/client";
import { Show } from "../components/Remote";
import { fmtNum, fmtPct } from "../lib/format";
import { isOpen, tallyRows } from "../lib/governance";
import { useApi } from "../useApi";

export function GovernancePage() {
  const remote = useApi(api.proposals);
  return (
    <>
      <h1>Governance</h1>
      <p className="lede">
        Tallies are weighted by allocated governance power. A tally is complete only when the relays
        that answered cover every vote.
      </p>
      <Show remote={remote}>
        {(rows) => (
          <ul className="cards">
            {rows.map((p) => (
              <li key={p.id} className="card">
                <h2>{p.title}</h2>
                <p className="muted small">
                  {p.id} · {isOpen(p, Date.now()) ? "open until" : "closed"}{" "}
                  {p.deadline.slice(0, 10)}
                </p>
                <p className={p.complete ? "flag ok" : "flag warn"}>
                  <span aria-hidden="true">{p.complete ? "✓" : "!"}</span>{" "}
                  {p.complete
                    ? "Tally complete"
                    : "Tally incomplete: some relays have not answered"}
                </p>
                <ul className="tally">
                  {tallyRows(p).map((r) => (
                    <li key={r.option}>
                      <div className="tallyhead">
                        <span>
                          {r.option}
                          {r.leading && " (leading)"}
                        </span>
                        <span className="num">
                          {fmtNum(r.weight)} · {fmtPct(r.share * 100, 1)}
                        </span>
                      </div>
                      <div className="meter" aria-hidden="true">
                        <span
                          className={r.leading ? "lead" : ""}
                          style={{ width: `${r.share * 100}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
                <p className="small muted">
                  Relays answered ({p.relays_answered.length}):{" "}
                  {p.relays_answered.join(", ") || "none"}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Show>
    </>
  );
}
