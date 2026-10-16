import { createAuth } from "./auth";
import { providers } from "./fixture";
import { route } from "./routes";

/** Example response of every contract endpoint, from the mock: the reference `apps/sa-api` must match key for key. */
export function contractDump(): Record<string, unknown> {
  const get = (path: string, query = ""): unknown => route(path, new URLSearchParams(query)).body;
  const market = "instrument=BTCUSDT&tf=1h&end_ms=1791878400000&limit=3";
  const pks = providers.map((p) => p.pubkey);
  const first = pks[0] ?? "";
  // sign-in flow with a deterministic clock and nonces: the three POST bodies and the signed-in /api/me
  const auth = createAuth({
    now: () => new Date("2026-10-13T09:00:00.000Z"),
    random: (() => {
      let n = 0;
      return () => `mock${(n++).toString(16).padStart(28, "0")}`;
    })(),
    origin: "http://127.0.0.1:8787",
  });
  const wallet = "0x5A2b00000000000000000000000000000000c0de";
  const nonce = auth.handle("/api/auth/nonce", { address: wallet }, null)?.body as {
    message: string;
  };
  const verify = auth.handle(
    "/api/auth/verify",
    { message: nonce.message, signature: `0x${"ab".repeat(65)}` },
    null,
  );
  const cookie = verify?.setCookie?.split(";")[0] ?? "";
  return {
    "GET /api/providers": get("/api/providers"),
    "GET /api/providers/{pubkey}": get(`/api/providers/${first}`),
    // every provider's record: the gappy one and the private one carry the optional lists
    "GET /api/providers/{pubkey}/record": pks.map((pk) => get(`/api/providers/${pk}/record`)),
    "GET /api/providers/{pubkey}/signals": pks.map((pk) =>
      get(`/api/providers/${pk}/signals`, "limit=3"),
    ),
    "GET /api/dev/providers/{pubkey}/releases": pks.map((pk) =>
      get(`/api/dev/providers/${pk}/releases`, "limit=3"),
    ),
    "GET /api/backtests": get("/api/backtests"),
    "GET /api/backtests/{id}": get(
      `/api/backtests/${(get("/api/backtests") as { id: string }[])[0]?.id}`,
    ),
    "GET /api/market/symbols": get("/api/market/symbols", "q=ETH"),
    "GET /api/market/candles": get("/api/market/candles", market),
    "GET /api/market/indicators": get(
      "/api/market/indicators",
      `${market}&spec=${encodeURIComponent("ema(20),rsi(14),bbands(20,2),sma(5),atr(14)")}`,
    ),
    // a file download: the body is the file text, the headers are Content-Disposition: attachment
    "GET /api/providers/{pubkey}/export?format=bundle": get(
      `/api/providers/${first}/export`,
      "format=bundle",
    ),
    "GET /api/providers/{pubkey}/export?format=csv": get(
      `/api/providers/${first}/export`,
      "format=csv",
    ),
    "GET /api/me": get("/api/me"),
    "GET /api/me/signals": get("/api/me/signals"),
    "GET /api/me (signed in)": route("/api/me", new URLSearchParams(), auth.sessionAddress(cookie))
      .body,
    "POST /api/auth/nonce": nonce,
    "POST /api/auth/verify": verify?.body,
    "POST /api/auth/logout": auth.handle("/api/auth/logout", {}, cookie)?.body,
    "GET /api/governance/proposals": get("/api/governance/proposals"),
    "GET /api/network": get("/api/network"),
  };
}
