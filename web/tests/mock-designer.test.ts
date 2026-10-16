import { describe, expect, test } from "bun:test";
import { createDesignerMock, validateDocument, checkExpression } from "../mock/designer";
import { type Json, setAt } from "../src/lib/designer/doc";
import { EXAMPLES } from "../src/lib/designer/examples";
import { contractDump } from "../mock/contract";

const ex = EXAMPLES[0]?.doc as Json;

describe("mock validate", () => {
  test("every shipped example passes", () => {
    for (const e of EXAMPLES) expect(validateDocument(e.doc), e.file).toEqual({ ok: true, errors: [] });
  });

  test("expression errors carry the column", () => {
    expect(checkExpression("close > ema(close, 20")).toEqual({ column: 11, message: "unclosed '('" });
    expect(checkExpression("close > ")).toEqual({ column: 8, message: "expression ends after '>'; expected a value" });
    expect(checkExpression("close ) 1")).toEqual({ column: 6, message: "unmatched ')'" });
    expect(checkExpression("foo(1)")).toEqual({ column: 0, message: "unknown function 'foo'" });
    expect(checkExpression("close > bar")).toEqual({ column: 8, message: "unknown name 'bar'" });
    expect(checkExpression("close @ 1")).toEqual({ column: 6, message: "unexpected character '@'" });
    expect(checkExpression('close > ema(close, 20, "4h")')).toBeNull();
    expect(checkExpression("1.5 * atr(14)")).toBeNull();
  });

  test("a bad condition is reported at its JSON path and column", () => {
    const doc = setAt(ex, ["stages", "open", "when", "any", 0, "all", 0], "cross_above(ema(close, 20), ");
    const r = validateDocument(doc);
    expect(r.ok).toBe(false);
    expect(r.errors[0]).toMatchObject({ path: "$.stages.open.when.any[0].all[0]", column: 11 });
  });

  test("structure problems come first and are listed with paths", () => {
    expect(validateDocument(3)).toMatchObject({ ok: false, errors: [{ path: "$" }] });
    const r = validateDocument({ name: "x", timeframe: "9y", stages: {} });
    expect(r.errors.map((e) => e.path).sort()).toEqual(["$.stages.open", "$.timeframe"]);
  });
});

describe("mock backtest jobs", () => {
  const body = { strategy: ex, instrument: "BINANCE:BTCUSDT", timeframe: "4h", start: "2025-01-01", end: "2025-02-01" };

  test("run, poll, done with a backtest id the chart can open", () => {
    let t = 1000;
    const h = createDesignerMock({ now: () => t, jobMs: 500 });
    const started = h("POST", "/api/backtests/run", body);
    expect(started?.status).toBe(202);
    const id = (started?.body as { id: string }).id;
    expect(h("GET", `/api/backtests/jobs/${id}`, null)?.body).toMatchObject({ state: "running", backtest_id: null });
    expect(h("POST", "/api/backtests/run", body)?.status).toBe(409);
    t += 500;
    const done = h("GET", `/api/backtests/jobs/${id}`, null)?.body as { state: string; backtest_id: string };
    expect(done.state).toBe("done");
    expect(done.backtest_id).toBeTruthy();
    expect(h("POST", "/api/backtests/run", body)?.status).toBe(202);
  });

  test("403 for proxied requests, 422 for a bad request, 404 for an unknown job", () => {
    const h = createDesignerMock();
    const proxied = h("POST", "/api/backtests/run", body, new Headers({ "x-forwarded-for": "1.2.3.4" }));
    expect(proxied?.status).toBe(403);
    expect((proxied?.body as { error: string }).error).toContain("loopback");
    const bad = h("POST", "/api/backtests/run", { ...body, start: "2025-03-01", instrument: "X", timeframe: "9y" });
    expect(bad?.status).toBe(422);
    expect((bad?.body as { errors: { path: string }[] }).errors.map((e) => e.path)).toEqual(["request.instrument", "request.end", "request.timeframe"]);
    const broken = h("POST", "/api/backtests/run", { ...body, strategy: 3, start: "x", end: "y" });
    expect((broken?.body as { errors: { path: string }[] }).errors.map((e) => e.path)).toContain("$");
    expect(h("GET", "/api/backtests/jobs/nope", null)?.status).toBe(404);
    expect(h("GET", "/api/providers", null)).toBeNull();
  });

  test("validate is served and the contract dump is unchanged by the designer routes", () => {
    const h = createDesignerMock();
    expect(h("POST", "/api/strategy/validate", ex)?.body).toEqual({ ok: true, errors: [] });
    expect(Object.keys(contractDump()).some((k) => k.includes("strategy") || k.includes("jobs"))).toBe(false);
  });
});
