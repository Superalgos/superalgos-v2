import { describe, expect, test } from "bun:test";
import { ApiError, getJson } from "../src/api/client";
import { api } from "../src/api/client";
import { route } from "../mock/routes";
import { contractDump } from "../mock/contract";
import { backtests, providers, recordFor, rng, signalsFor, walk } from "../mock/fixture";

const q = (s = "") => new URLSearchParams(s);

describe("getJson", () => {
  test("parses ok responses", async () => {
    const f = async () => new Response(JSON.stringify({ a: 1 }));
    expect(await getJson<{ a: number }>("/x", f)).toEqual({ a: 1 });
  });
  test("non-2xx raises ApiError naming path and status", async () => {
    const f = async () => new Response("no", { status: 502 });
    const err = await getJson("/api/x", f).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toContain("/api/x");
    expect(err.message).toContain("502");
  });
  test("network failure gets actionable message", async () => {
    const f = async () => {
      throw new Error("refused");
    };
    await expect(getJson("/api/y", f)).rejects.toThrow(/refused.*mock/);
    const g = async () => {
      throw "weird";
    };
    await expect(getJson("/api/y", g)).rejects.toThrow(/weird/);
  });
  test("api helpers hit the contract paths", async () => {
    const seen: string[] = [];
    const orig = globalThis.fetch;
    globalThis.fetch = (async (u: string) => {
      seen.push(u);
      return new Response("[]");
    }) as unknown as typeof fetch;
    try {
      await api.providers();
      await api.provider("pk");
      await api.record("a b");
      await api.signals("pk", 5);
      await api.signals("pk");
      await api.backtests();
      await api.backtest("id");
      await api.me();
      await api.mySignals(7);
      await api.devReleases("pk", 3);
      await api.proposals();
      await api.network();
    } finally {
      globalThis.fetch = orig;
    }
    expect(seen).toEqual([
      "/api/providers",
      "/api/providers/pk",
      "/api/providers/a%20b/record",
      "/api/providers/pk/signals?limit=5",
      "/api/providers/pk/signals?limit=50",
      "/api/backtests",
      "/api/backtests/id",
      "/api/me",
      "/api/me/signals?limit=7",
      "/api/dev/providers/pk/releases?limit=3",
      "/api/governance/proposals",
      "/api/network",
    ]);
  });
});

describe("mock routes", () => {
  test("every contract endpoint answers 200", () => {
    const pk = providers[0]?.pubkey ?? "";
    for (const p of [
      "/api/providers",
      `/api/providers/${pk}`,
      `/api/providers/${pk}/record`,
      `/api/providers/${pk}/signals`,
      `/api/dev/providers/${pk}/releases`,
      "/api/me/signals",
      "/api/backtests",
      `/api/backtests/${backtests[0]?.id}`,
      "/api/me",
      "/api/governance/proposals",
      "/api/network",
    ]) {
      expect(route(p, q()).status).toBe(200);
    }
  });
  test("404s", () => {
    expect(route("/api/providers/nope", q()).status).toBe(404);
    expect(route("/api/providers/nope/record", q()).status).toBe(404);
    expect(route("/api/providers/nope/signals", q()).status).toBe(404);
    expect(route("/api/backtests/nope", q()).status).toBe(404);
    expect(route("/other", q()).status).toBe(404);
    expect(route("/api/zzz", q()).status).toBe(404);
  });
  test("limit is honoured and bad limit falls back", () => {
    const pk = providers[0]?.pubkey ?? "";
    expect((route(`/api/providers/${pk}/signals`, q("limit=3")).body as unknown[]).length).toBe(3);
    expect((route(`/api/providers/${pk}/signals`, q("limit=abc")).body as unknown[]).length).toBe(14);
  });
});

describe("fixture", () => {
  test("deterministic", () => {
    expect(rng(1)()).toBe(rng(1)());
    expect(walk(3, 10, 0, 0.01)).toEqual(walk(3, 10, 0, 0.01));
  });
  test("private provider has unrevealed commitments, gappy one has gaps", () => {
    const priv = providers.find((p) => p.mode === "private");
    expect(recordFor(priv?.pubkey ?? "")?.unrevealed_commitments.length).toBeGreaterThan(0);
    expect(recordFor(providers[1]?.pubkey ?? "")?.gaps.length).toBeGreaterThan(0);
    expect(recordFor("zzz")).toBeUndefined();
    expect(signalsFor("zzz", 3)).toBeUndefined();
  });
});

describe("contract", () => {
  test("mock/contract.json is the current mock output (run `bun run contract` after changing the fixture)", async () => {
    const committed = await Bun.file(new URL("../mock/contract.json", import.meta.url)).json();
    expect(committed).toEqual(JSON.parse(JSON.stringify(contractDump())));
  });
  test("record_summary carries signatures_ok on every provider", () => {
    for (const p of providers) expect(typeof p.record_summary.signatures_ok).toBe("boolean");
  });
});

describe("mock: per-party views", () => {
  test("a provider's signals carry a status and no release; the dev route carries the releases", () => {
    const pk = providers[0]?.pubkey ?? "";
    const plain = route(`/api/providers/${pk}/signals`, q()).body as Record<string, unknown>[];
    expect(plain.length).toBeGreaterThan(0);
    for (const s of plain) {
      expect(s.release).toBeUndefined();
      expect(s.status).toBe(s.anchored_ms === null ? "waiting_for_time_stamp" : "out");
    }
    const dev = route(`/api/dev/providers/${pk}/releases`, q()).body as { release: unknown[] }[];
    expect(dev.every((s) => Array.isArray(s.release))).toBe(true);
    expect(route("/api/dev/providers/nope/releases", q()).status).toBe(404);
  });
  test("my signals come newest first and say what happened", () => {
    const rows = route("/api/me/signals", q("limit=3")).body as { received_ms: number; outcome: string }[];
    expect(rows).toHaveLength(3);
    expect(rows.map((r) => r.received_ms)).toEqual(rows.map((r) => r.received_ms).sort((a, b) => b - a));
    expect(new Set(rows.map((r) => r.outcome)).size).toBeGreaterThan(0);
  });
});
