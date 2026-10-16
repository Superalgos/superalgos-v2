import { useEffect, useState } from "react";
import { api } from "./api/client";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { DEV_NAV, NAV, parseHash, type Route } from "./lib/route";
import { BacktestsPage } from "./pages/Backtests";
import { ChartPage } from "./pages/Chart";
import { DesignerPage } from "./pages/Designer";
import { DevPage } from "./pages/Dev";
import { GovernancePage } from "./pages/Governance";
import { NetworkPage } from "./pages/Network";
import { PowerPage } from "./pages/Power";
import { ProviderPage } from "./pages/Provider";
import { ProvidersPage } from "./pages/Providers";
import { useApi } from "./useApi";

function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => {
      setRoute(parseHash(window.location.hash));
      document.getElementById("main")?.focus();
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

function Page({ route }: { route: Route }) {
  switch (route.page) {
    case "providers":
      return <ProvidersPage />;
    case "provider":
      return <ProviderPage pubkey={route.pubkey} />;
    case "power":
      return <PowerPage />;
    case "backtests":
      return <BacktestsPage selected={route.id} />;
    case "governance":
      return <GovernancePage />;
    case "chart":
      return <ChartPage state={route.state} />;
    case "network":
      return <NetworkPage />;
    case "designer":
      return <DesignerPage />;
    case "dev":
      return <DevPage />;
  }
}

export function App() {
  const route = useRoute();
  const current = route.page === "provider" ? "providers" : route.page;
  // The API says whether it serves the dev views; until it answers (or if it cannot) they are not offered.
  const network = useApi(api.network);
  const nav = network.state === "ok" && network.data.dev_views ? [...NAV, DEV_NAV] : NAV;
  return (
    <>
      <button
        type="button"
        className="skip"
        onClick={() => document.getElementById("main")?.focus()}
      >
        Skip to content
      </button>
      <header className="top">
        <span className="brand">
          Superalgos <small>v2</small>
        </span>
        <nav aria-label="Main">
          <ul>
            {nav.map((n) => (
              <li key={n.page}>
                <a href={n.href} aria-current={n.page === current ? "page" : undefined}>
                  {n.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main id="main" tabIndex={-1} className={route.page === "designer" ? "wide" : undefined}>
        <ErrorBoundary label="This page" resetKey={JSON.stringify(route)}>
          <Page route={route} />
        </ErrorBoundary>
      </main>
    </>
  );
}
