import { api } from "../api/client";
import { Show } from "../components/Remote";
import { fmtNum } from "../lib/format";
import { useApi } from "../useApi";

export function NetworkPage() {
  const remote = useApi(api.network);
  return (
    <>
      <h1>Network</h1>
      <Show remote={remote}>
        {(n) => (
          <div className="split2">
            <section aria-labelledby="relays">
              <h2 id="relays">Relays</h2>
              <ul className="status">
                {n.relays.map((r) => (
                  <li key={r.url} className={r.ok ? "ok" : "bad"}>
                    <span aria-hidden="true">{r.ok ? "●" : "○"}</span> <code>{r.url}</code>{" "}
                    <b>{r.ok ? "up" : "down"}</b>
                  </li>
                ))}
              </ul>
            </section>
            <section aria-labelledby="chains">
              <h2 id="chains">Chains</h2>
              <ul className="status">
                {n.chains.map((c) => (
                  <li key={c.name} className={c.ok ? "ok" : "bad"}>
                    <span aria-hidden="true">{c.ok ? "●" : "○"}</span> {c.name}{" "}
                    <b>{c.ok ? "up" : "down"}</b>
                    {c.block !== null && <span className="muted"> · block {fmtNum(c.block)}</span>}
                  </li>
                ))}
              </ul>
            </section>
          </div>
        )}
      </Show>
    </>
  );
}
