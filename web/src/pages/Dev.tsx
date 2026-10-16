import { useState } from "react";
import { api } from "../api/client";
import type { Provider } from "../api/types";
import { Show } from "../components/Remote";
import { TierTimeline } from "../components/TierTimeline";
import { shortKey } from "../lib/format";
import { LIVE, useApi } from "../useApi";

/**
 * DEVELOPMENT VIEW, not a product page: how the relay releases each signal to every power tier, with the real
 * receipt times of the followers this API was configured to read. It exists only while the API runs with dev views
 * on (`sa demo`); a production API neither serves the data nor shows this page.
 */
export function DevPage() {
  const network = useApi(api.network, "", LIVE);
  return (
    <>
      <h1>Dev views</h1>
      <p role="note" className="warnbox">
        Development and testing only. These views show per-tier delivery and other followers'
        receipts, which a provider or follower never sees in the product.
      </p>
      <Show remote={network}>
        {(n) => (n.dev_views ? <Timelines timeScale={n.time_scale} /> : <DevOff />)}
      </Show>
    </>
  );
}

function DevOff() {
  return (
    <p role="status" className="muted">
      Dev views are off on this API. Start it with <code>"dev_views": true</code> in its config (
      <code>sa demo</code> does) to see them.
    </p>
  );
}

function Timelines({ timeScale }: { timeScale: number }) {
  const providers = useApi(api.providers, "", LIVE);
  const [picked, setPicked] = useState<string | null>(null);
  return (
    <Show remote={providers}>
      {(all) => {
        const p = all.find((x) => x.pubkey === picked) ?? all[0];
        if (!p) return <p className="muted">No providers yet.</p>;
        return (
          <>
            <h2>Signal timeline by tier</h2>
            <p className="lede">
              Higher power tiers receive each signal sooner. One marker per tier, at its scheduled
              offset after the signal was created.
            </p>
            <label>
              Provider{" "}
              <select value={p.pubkey} onChange={(e) => setPicked(e.target.value)}>
                {all.map((x) => (
                  <option key={x.pubkey} value={x.pubkey}>
                    {x.name} ({shortKey(x.pubkey, 8, 4)})
                  </option>
                ))}
              </select>
            </label>
            <ProviderTimeline key={p.pubkey} provider={p} timeScale={timeScale} />
          </>
        );
      }}
    </Show>
  );
}

function ProviderTimeline({ provider, timeScale }: { provider: Provider; timeScale: number }) {
  const signals = useApi(() => api.devReleases(provider.pubkey, 30), provider.pubkey, LIVE);
  return (
    <Show remote={signals}>
      {(sig) => <TierTimeline provider={provider} signals={sig} timeScale={timeScale} />}
    </Show>
  );
}
