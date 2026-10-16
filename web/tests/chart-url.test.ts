import { describe, expect, test } from "bun:test";
import { chartHref, parseChartQuery } from "../src/lib/market";
import { parseHash } from "../src/lib/route";

const kinds = (hash: string) => {
  const r = parseHash(hash);
  if (r.page !== "chart") throw new Error(`expected the chart route for ${hash}`);
  return r.state.ind.map((s) => `${s.kind}:${s.params.join(",")}`);
};

describe("ind= in the chart URL", () => {
  test("raw comma list in one ind value", () => {
    expect(kinds("#/chart?instrument=ETHUSDT&tf=1h&ind=ema(20),bbands(20,2),rsi(14)")).toEqual([
      "ema:20",
      "bbands:20,2",
      "rsi:14",
    ]);
  });
  test("percent-encoded comma list in one ind value", () => {
    expect(kinds("#/chart?tf=1h&ind=ema%2820%29%2Cbbands%2820%2C2%29%2Crsi%2814%29")).toEqual([
      "ema:20",
      "bbands:20,2",
      "rsi:14",
    ]);
  });
  test("single raw and single encoded", () => {
    expect(kinds("#/chart?ind=ema(20)")).toEqual(["ema:20"]);
    expect(kinds("#/chart?ind=ema%2820%29")).toEqual(["ema:20"]);
  });
  test("repeated ind params, mixed forms", () => {
    expect(kinds("#/chart?ind=ema(20)&ind=rsi%2814%29&ind=ema(20)")).toEqual(["ema:20", "rsi:14"]);
  });
  test("double-encoded values still parse", () => {
    expect(parseChartQuery("ind=ema%252820%2529").ind).toEqual([{ kind: "ema", params: [20] }]);
  });
  test("invalid pieces are dropped, valid ones kept", () => {
    expect(kinds("#/chart?ind=ema(20),nope(3),rsi(1),sma(50)")).toEqual(["ema:20", "sma:50"]);
  });
  test("what the Add button writes round-trips", () => {
    const href = chartHref({ instrument: "ETHUSDT", ind: [{ kind: "bbands", params: [20, 2] }] });
    expect(kinds(href)).toEqual(["bbands:20,2"]);
  });
});
