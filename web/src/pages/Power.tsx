import { useState } from "react";
import { api } from "../api/client";
import { MySignals } from "../components/MySignals";
import { PowerBar } from "../components/PowerBar";
import { Show } from "../components/Remote";
import { WalletSignIn } from "../components/WalletSignIn";
import { fmtNum, shortKey } from "../lib/format";
import { allocationError, splitPower, tierFor } from "../lib/power";
import { providerHref } from "../lib/route";
import { useApi } from "../useApi";

export function PowerPage() {
  const [epoch, setEpoch] = useState(0);
  const me = useApi(api.me, String(epoch));
  const providers = useApi(api.providers);
  return (
    <>
      <h1>My power</h1>
      <p className="lede">
        Token power is a budget: each unit serves one use at a time. No tokens move when you split
        it.
      </p>
      <Show remote={me}>
        {(m) => (
          <Show remote={providers}>
            {(ps) => {
              const err = allocationError(m);
              return (
                <>
                  <p className="muted small">
                    {m.signed_in ? "Your wallet" : "Demo identity"}{" "}
                    <code title={m.sa_address}>{shortKey(m.sa_address, 8, 6)}</code>
                  </p>
                  <WalletSignIn
                    signedIn={m.signed_in}
                    address={m.sa_address}
                    onChange={() => setEpoch((n) => n + 1)}
                  />
                  <p className="big num">
                    {fmtNum(m.token_power)} <small>SA token power</small>
                  </p>
                  {m.balances && (
                    <ul className="chips" aria-label="Balance by chain">
                      {m.balances.map((b) => (
                        <li key={b.chain}>
                          <span>{b.chain}</span> <b className="num">{fmtNum(b.balance)}</b>
                        </li>
                      ))}
                    </ul>
                  )}
                  {err && (
                    <p role="alert" className="error">
                      {err}
                    </p>
                  )}
                  <h2>Allocation</h2>
                  <PowerBar segments={splitPower(m, ps)} />
                  <h2>Tier with each backed provider</h2>
                  <div className="scroll">
                    <table>
                      <thead>
                        <tr>
                          <th scope="col">Provider</th>
                          <th scope="col" className="num">
                            Allocated power
                          </th>
                          <th scope="col">Tier</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(m.allocations.providers).map(([pk, bps]) => {
                          const p = ps.find((x) => x.pubkey === pk);
                          const power = (m.token_power * bps) / 10_000;
                          const tier = p ? tierFor(p, power) : null;
                          return (
                            <tr key={pk}>
                              <th scope="row">
                                {p ? <a href={providerHref(pk)}>{p.name}</a> : shortKey(pk, 10, 6)}
                              </th>
                              <td className="num">{fmtNum(power)}</td>
                              <td>
                                {tier === null
                                  ? p?.mode === "private"
                                    ? "no access"
                                    : "unknown"
                                  : `Tier ${tier + 1}`}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  <MySignals providers={ps} />
                </>
              );
            }}
          </Show>
        )}
      </Show>
    </>
  );
}
