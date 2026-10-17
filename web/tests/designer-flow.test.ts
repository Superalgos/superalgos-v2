import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import {
  INSTRUMENTS,
  type JobStatus,
  getJob,
  openOnChartHref,
  pollJob,
  runBacktest,
  windowDays,
} from "../src/lib/designer/backtest";
import { type Json, serialize } from "../src/lib/designer/doc";
import { EXAMPLES } from "../src/lib/designer/examples";
import { buildGraph } from "../src/lib/designer/graph";
import { HISTORY_LIMIT, initHistory, push, redo, undo } from "../src/lib/designer/history";
import { actionsFor, applyAction } from "../src/lib/designer/rules";
import {
  type Timers,
  type ValidateResponse,
  type ValidationState,
  createValidator,
  postValidate,
} from "../src/lib/designer/validator";
import {
  loadView,
  parseView,
  remapPositions,
  saveView,
  serializeView,
  viewKey,
} from "../src/lib/designer/view";

const json = (body: unknown, status = 200) => Response.json(body, { status });
const doc = EXAMPLES[0]?.doc as Json;

describe("validator", () => {
  function fakeTimers() {
    const q: { fn: () => void; ms: number; id: number }[] = [];
    let n = 0;
    const timers: Timers = {
      set: (fn, ms) => {
        q.push({ fn, ms, id: ++n });
        return n;
      },
      clear: (h) => {
        const i = q.findIndex((x) => x.id === h);
        if (i >= 0) q.splice(i, 1);
      },
    };
    return { q, timers };
  }

  test("debounces: only the last document is sent, after the delay", async () => {
    const { q, timers } = fakeTimers();
    const sent: Json[] = [];
    const states: ValidationState[] = [];
    const v = createValidator({
      validate: async (d) => {
        sent.push(d);
        return { ok: true, errors: [] };
      },
      onState: (s) => states.push(s),
      timers,
      delayMs: 123,
    });
    v.request({ a: 1 });
    v.request({ a: 2 });
    v.request({ a: 3 });
    expect(q).toHaveLength(1);
    expect(q[0]?.ms).toBe(123);
    expect(sent).toHaveLength(0);
    expect(v.state().status).toBe("pending");
    q[0]?.fn();
    await v.flush();
    expect(sent).toEqual([{ a: 3 }, { a: 3 }]);
    expect(v.state().status).toBe("ok");
    expect(states[0]?.status).toBe("pending");
  });

  test("a stale answer never overwrites a newer one", async () => {
    const { q, timers } = fakeTimers();
    const gates: ((r: ValidateResponse) => void)[] = [];
    const v = createValidator({
      validate: () => new Promise((res) => gates.push(res)),
      onState: () => {},
      timers,
    });
    v.request({ n: 1 });
    q.shift()?.fn();
    v.request({ n: 2 });
    q.shift()?.fn();
    gates[1]?.({ ok: false, errors: [{ path: "$", column: null, message: "second" }] });
    await Promise.resolve();
    gates[0]?.({ ok: true, errors: [] });
    await Promise.resolve();
    await Promise.resolve();
    expect(v.state()).toEqual({
      status: "invalid",
      errors: [{ path: "$", column: null, message: "second" }],
    });
  });

  test("a failing server leaves an 'unavailable' state that keeps the last errors", async () => {
    const { timers } = fakeTimers();
    let fail = false;
    const v = createValidator({
      validate: async () => {
        if (fail) throw new Error("down");
        return { ok: false, errors: [{ path: "$.name", column: null, message: "m" }] };
      },
      onState: () => {},
      timers,
    });
    v.request({});
    await v.flush();
    fail = true;
    const s = await v.flush();
    expect(s).toMatchObject({ status: "unavailable", message: "down" });
    expect(s.errors).toHaveLength(1);
  });

  test("an error that is not an Error is reported as text", async () => {
    const { timers } = fakeTimers();
    const v = createValidator({
      validate: async () => {
        throw "plain";
      },
      onState: () => {},
      timers,
    });
    v.request({});
    expect(await v.flush()).toMatchObject({ status: "unavailable", message: "plain" });
  });

  test("flush without a document, and dispose, are safe", async () => {
    const { q, timers } = fakeTimers();
    const states: ValidationState[] = [];
    const v = createValidator({
      validate: async () => ({ ok: true, errors: [] }),
      onState: (s) => states.push(s),
      timers,
    });
    expect((await v.flush()).status).toBe("idle");
    v.request({});
    v.dispose();
    expect(q).toHaveLength(0);
    const n = states.length;
    await v.flush();
    expect(states.length).toBe(n);
    v.resume();
    v.request({});
    expect(states.length).toBeGreaterThan(n);
  });

  test("real timers debounce too", async () => {
    let calls = 0;
    const v = createValidator({
      validate: async () => {
        calls++;
        return { ok: true, errors: [] };
      },
      onState: () => {},
      delayMs: 5,
    });
    v.request({});
    v.request({});
    await new Promise((r) => setTimeout(r, 400));
    expect(calls).toBe(1);
    v.dispose();
  });

  test("postValidate: the body is the document, errors keep path and column", async () => {
    let seen: { url: string; init?: RequestInit } | null = null;
    const r = await postValidate(doc, async (url, init) => {
      seen = { url, init };
      return json({
        ok: false,
        errors: [{ path: "$.stages.open.when.any[0].all[0]", column: 5, message: "unexpected" }],
      });
    });
    expect(r.errors[0]?.column).toBe(5);
    const s = seen as unknown as { url: string; init: RequestInit };
    expect(s.url).toBe("/api/strategy/validate");
    expect(JSON.parse(String(s.init.body))).toEqual(doc as object);
  });

  test("postValidate: unreachable, non-200 and odd bodies say what to do", async () => {
    await expect(
      postValidate(doc, async () => {
        throw new Error("ECONNREFUSED");
      }),
    ).rejects.toThrow(/unreachable \(ECONNREFUSED\).*bun run mock/);
    await expect(postValidate(doc, async () => json({}, 413))).rejects.toThrow(/answered 413/);
    await expect(postValidate(doc, async () => json({ nope: 1 }))).rejects.toThrow(
      /unexpected body/,
    );
  });
});

describe("backtest flow (SEP-5.7)", () => {
  const req = {
    strategy: doc,
    instrument: "BINANCE:BTCUSDT",
    timeframe: "4h",
    start: "2025-01-01",
    end: "2025-02-01",
  };
  const job = (state: JobStatus["state"], extra: Partial<JobStatus> = {}): JobStatus => ({
    id: "j1",
    state,
    started_ms: 1,
    finished_ms: state === "running" ? null : 2,
    backtest_id: null,
    error: null,
    ...extra,
  });

  test("a started job is polled until done, then links to the chart", async () => {
    const calls: string[] = [];
    const states = [job("running"), job("running"), job("done", { backtest_id: "j1" })];
    const fetcher = async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url === "/api/backtests/run") return json(job("running"), 202);
      return json(states.shift());
    };
    const started = await runBacktest(req, fetcher);
    expect(started.kind).toBe("started");
    const seen: string[] = [];
    const sleeps: number[] = [];
    const done = await pollJob("j1", {
      fetcher,
      onUpdate: (j) => seen.push(j.state),
      sleep: async (ms) => void sleeps.push(ms),
      intervalMs: 250,
    });
    expect(seen).toEqual(["running", "running", "done"]);
    expect(sleeps).toEqual([250, 250]);
    expect(calls).toEqual([
      "POST /api/backtests/run",
      "GET /api/backtests/jobs/j1",
      "GET /api/backtests/jobs/j1",
      "GET /api/backtests/jobs/j1",
    ]);
    expect(openOnChartHref(done, "BINANCE:BTCUSDT", "4h")).toBe(
      "#/chart?instrument=BTCUSDT&tf=4h&bt=j1",
    );
    // the chart has no 30m: it falls back to its default and then follows the report
    expect(openOnChartHref(done, "BINANCE:ETHUSDT", "30m")).toBe(
      "#/chart?instrument=ETHUSDT&tf=1h&bt=j1",
    );
  });

  test("the request body carries strategy, instrument, timeframe and the window", async () => {
    let body: Record<string, unknown> = {};
    await runBacktest(req, async (_u, init) => {
      body = JSON.parse(String(init?.body));
      return json(job("running"), 202);
    });
    expect(Object.keys(body).sort()).toEqual(["end", "instrument", "start", "strategy", "timeframe"]);
    expect(body.strategy).toEqual(doc as object);
  });

  test("403 shows the server's loopback reason", async () => {
    const reason =
      "backtests are allowed only on a loopback-bound API: the API is bound to 0.0.0.0, not loopback; start it with --host 127.0.0.1";
    const out = await runBacktest(req, async () => json({ error: reason }, 403));
    expect(out).toMatchObject({ kind: "refused", status: 403 });
    expect(out.kind === "refused" && out.reason).toContain(reason);
    expect(out.kind === "refused" && out.reason).toMatch(
      /^Backtests are not allowed from this page/,
    );
  });

  test("409 names the running job, 422 returns its issues, other statuses are explained", async () => {
    const busy = await runBacktest(req, async () =>
      json({ error: "a backtest is running", job: "abc" }, 409),
    );
    expect(busy).toMatchObject({ kind: "refused", status: 409, runningJob: "abc" });
    const bad = await runBacktest(req, async () =>
      json(
        {
          error: "invalid",
          errors: [{ path: "request.start", column: null, message: "after end" }],
        },
        422,
      ),
    );
    expect(bad.kind === "refused" && bad.errors[0]?.path).toBe("request.start");
    const odd = await runBacktest(req, async () => new Response("<html>", { status: 502 }));
    expect(odd.kind === "refused" && odd.reason).toContain("(502): HTTP 502");
    const down = await runBacktest(req, async () => {
      throw new Error("refused");
    });
    expect(down).toMatchObject({ kind: "unreachable" });
    expect(down.kind === "unreachable" && down.reason).toContain("bun run mock");
  });

  test("failed and timed-out jobs have no chart link; unknown jobs explain", async () => {
    expect(openOnChartHref(job("failed", { error: "boom" }), "BTCUSDT", "1h")).toBeNull();
    expect(openOnChartHref(job("done"), "BTCUSDT", "1h")).toBeNull();
    await expect(getJob("zz", async () => json({ error: "no job" }, 404))).rejects.toThrow(
      /answered 404: no job.*run the backtest again/,
    );
    await expect(getJob("zz", async () => new Response("x", { status: 500 }))).rejects.toThrow(
      /answered 500/,
    );
  });

  test("polling can be cancelled and is bounded", async () => {
    const ac = new AbortController();
    ac.abort();
    await expect(
      pollJob("j1", { signal: ac.signal, fetcher: async () => json(job("running")) }),
    ).rejects.toThrow(/cancelled/);
    await expect(
      pollJob("j1", {
        maxPolls: 2,
        sleep: async () => {},
        fetcher: async () => json(job("running")),
      }),
    ).rejects.toThrow(/still running after 2 polls/);
  });

  test("window and instruments", () => {
    expect(windowDays("2025-01-01", "2025-02-01")).toBe(31);
    expect(windowDays("2025-02-01", "2025-01-01")).toBeNull();
    expect(windowDays("x", "2025-01-01")).toBeNull();
    expect(INSTRUMENTS).toContain("BINANCE:BTCUSDT");
  });
});

describe("history", () => {
  test("undo and redo walk the snapshots; a new edit clears redo", () => {
    let h = initHistory(1);
    h = push(h, 2);
    h = push(h, 3);
    h = undo(h);
    expect(h.present).toBe(2);
    h = redo(h);
    expect(h.present).toBe(3);
    h = undo(undo(h));
    h = push(h, 9);
    expect(h.future).toEqual([]);
    expect(redo(h)).toBe(h);
    expect(undo(initHistory(0)).present).toBe(0);
    expect(push(h, 9)).toBe(h);
  });

  test("the past is bounded", () => {
    let h = initHistory(0);
    for (let i = 1; i <= HISTORY_LIMIT + 50; i++) h = push(h, i);
    expect(h.past).toHaveLength(HISTORY_LIMIT);
  });
});

describe("view file (SEP-5.10)", () => {
  const pos = { "$.stages.open": { x: 1.4, y: 2.6 }, "$.stages.close": { x: 3, y: 4 } };

  test("layout round trips through its own file and storage, never the strategy", () => {
    const text = serializeView({ strategy: "s", positions: pos });
    expect(parseView(text)).toEqual({
      view_version: 2,
      strategy: "s",
      positions: { "$.stages.close": { x: 3, y: 4 }, "$.stages.open": { x: 1, y: 3 } },
      radial: {},
    });
    const mem = new Map<string, string>();
    const store = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    saveView(store, "s", pos);
    expect([...mem.keys()]).toEqual([viewKey("s")]);
    expect(loadView(store, "s")["$.stages.open"]).toEqual({ x: 1, y: 3 });
    expect(loadView(store, "other")).toEqual({});
    const before = serialize(doc);
    saveView(store, "s", pos);
    expect(serialize(doc)).toBe(before);
  });

  test("bad view files and blocked storage give an empty layout", () => {
    expect(parseView("{")).toBeNull();
    expect(parseView("3")).toBeNull();
    expect(parseView('{"view_version":3,"strategy":"s","positions":{}}')).toBeNull();
    expect(parseView('{"view_version":1,"strategy":"s","positions":3}')).toBeNull();
    expect(
      parseView('{"view_version":1,"strategy":"s","positions":{"a":{"x":"1"},"b":{"x":1,"y":2}}}')
        ?.positions,
    ).toEqual({ b: { x: 1, y: 2 } });
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadView(broken, "s")).toEqual({});
    expect(() => saveView(broken, "s", pos)).not.toThrow();
    expect(loadView({ getItem: () => "garbage", setItem: () => {} }, "s")).toEqual({});
  });

  test("positions follow their nodes when a list is edited", () => {
    const p = {
      "$.stages.open.orders[0]": { x: 0, y: 0 },
      "$.stages.open.orders[1]": { x: 1, y: 1 },
      "$.stages.open.orders[2].rate": { x: 2, y: 2 },
      "$.stages.close": { x: 9, y: 9 },
    };
    const arr = ["stages", "open", "orders"];
    expect(Object.keys(remapPositions(p, { kind: "insert", array: arr, index: 1 })).sort()).toEqual([
      "$.stages.close",
      "$.stages.open.orders[0]",
      "$.stages.open.orders[2]",
      "$.stages.open.orders[3].rate",
    ]);
    const removed = remapPositions(p, { kind: "remove", array: arr, index: 1 });
    expect(removed["$.stages.open.orders[1].rate"]).toEqual({ x: 2, y: 2 });
    expect(Object.keys(removed)).toHaveLength(3);
    const moved = remapPositions(p, { kind: "move", array: arr, from: 0, to: 2 });
    expect(moved["$.stages.open.orders[2]"]).toEqual({ x: 0, y: 0 });
    expect(moved["$.stages.open.orders[0]"]).toEqual({ x: 1, y: 1 });
    expect(moved["$.stages.open.orders[1].rate"]).toEqual({ x: 2, y: 2 });
    const moved2 = remapPositions(p, { kind: "move", array: arr, from: 2, to: 0 });
    expect(moved2["$.stages.open.orders[0].rate"]).toEqual({ x: 2, y: 2 });
    expect(moved2["$.stages.open.orders[1]"]).toEqual({ x: 0, y: 0 });
    const dropped = remapPositions(p, { kind: "drop", prefix: ["stages", "open"] });
    expect(Object.keys(dropped)).toEqual(["$.stages.close"]);
    expect(remapPositions({ nonsense: { x: 1, y: 1 } }, { kind: "drop", prefix: [] })).toEqual({});
  });

  test("a move is a permutation: positions are neither lost nor merged", () => {
    fc.assert(
      fc.property(fc.integer({ min: 2, max: 8 }), fc.nat(7), fc.nat(7), (n, a, b) => {
        const from = a % n;
        const to = b % n;
        const p: Record<string, { x: number; y: number }> = {};
        for (let i = 0; i < n; i++) p[`$.stages.open.orders[${i}]`] = { x: i, y: i };
        const out = remapPositions(p, {
          kind: "move",
          array: ["stages", "open", "orders"],
          from,
          to,
        });
        return (
          Object.keys(out).length === n &&
          new Set(Object.values(out).map((v) => v.x)).size === n &&
          out[`$.stages.open.orders[${to}]`]?.x === from
        );
      }),
    );
  });

  test("editing through actions keeps every saved position valid", () => {
    const g0 = buildGraph(doc);
    const saved = Object.fromEntries(g0.nodes.map((n) => [n.id, { x: 1, y: 1 }]));
    const act = actionsFor(doc, ["stages", "open", "when"]).find((a) => a.label === "Add situation");
    const r = act ? applyAction(doc, act) : null;
    const next = r?.view ? remapPositions(saved, r.view) : saved;
    const g1 = buildGraph(r?.doc as Json);
    expect(Object.keys(next).every((k) => g1.byId.has(k))).toBe(true);
  });
});
