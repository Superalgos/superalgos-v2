import { type ChartState, parseChartQuery } from "./market";

export type Route =
  | { page: "providers" }
  | { page: "provider"; pubkey: string }
  | { page: "power" }
  | { page: "backtests"; id?: string }
  | { page: "governance" }
  | { page: "chart"; state: ChartState }
  | { page: "designer" }
  | { page: "network" }
  | { page: "dev" };

export const NAV: { page: Route["page"]; href: string; label: string }[] = [
  { page: "providers", href: "#/", label: "Providers" },
  { page: "power", href: "#/power", label: "My power" },
  { page: "chart", href: "#/chart", label: "Chart" },
  { page: "designer", href: "#/designer", label: "Designer" },
  { page: "backtests", href: "#/backtests", label: "Backtests" },
  { page: "governance", href: "#/governance", label: "Governance" },
  { page: "network", href: "#/network", label: "Network" },
];

/** Only offered when the API runs with dev views on (`Network.dev_views`). */
export const DEV_NAV = { page: "dev", href: "#/dev", label: "Dev views" } as const;

export function parseHash(hash: string): Route {
  const [path = "", ...rest] = hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const [head, arg] = parts;
  switch (head) {
    case "providers":
      return arg ? { page: "provider", pubkey: arg } : { page: "providers" };
    case "power":
      return { page: "power" };
    case "backtests":
      return arg ? { page: "backtests", id: arg } : { page: "backtests" };
    case "governance":
      return { page: "governance" };
    case "network":
      return { page: "network" };
    case "designer":
      return { page: "designer" };
    case "dev":
      return { page: "dev" };
    case "chart":
      return { page: "chart", state: parseChartQuery(rest.join("?")) };
    default:
      return { page: "providers" };
  }
}

export function providerHref(pubkey: string): string {
  return `#/providers/${encodeURIComponent(pubkey)}`;
}

export function backtestHref(id: string): string {
  return `#/backtests/${encodeURIComponent(id)}`;
}
