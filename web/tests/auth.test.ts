import { describe, expect, test } from "bun:test";
import { ApiError, postJson } from "../src/api/client";
import {
  type Eip1193,
  signInWithWallet,
  utf8Hex,
  walletAvailable,
  walletErrorMessage,
} from "../src/api/wallet";
import { COOKIE, createAuth, siweMessage } from "../mock/auth";
import { meFor } from "../mock/fixture";
import { route } from "../mock/routes";

const ADDR = "0x5A2b00000000000000000000000000000000c0de";
const SIG = `0x${"ab".repeat(65)}`;

function auth() {
  let n = 0;
  return createAuth({
    now: () => new Date("2026-10-13T09:00:00.000Z"),
    random: () => `n${n++}`,
    origin: "http://127.0.0.1:8787",
  });
}

describe("wallet helpers", () => {
  test("walletAvailable", () => {
    expect(walletAvailable({ ethereum: {} })).toBe(true);
    expect(walletAvailable({})).toBe(false);
    expect(walletAvailable({ ethereum: null })).toBe(false);
    expect(walletAvailable(undefined)).toBe(false);
  });
  test("utf8Hex encodes bytes, not code units", () => {
    expect(utf8Hex("Hi")).toBe("0x4869");
    expect(utf8Hex("é")).toBe("0xc3a9");
    expect(utf8Hex("")).toBe("0x");
  });
  test("error messages say what to do", () => {
    expect(walletErrorMessage({ code: 4001 })).toContain("rejected");
    expect(walletErrorMessage({ code: -32002 })).toContain("pending");
    expect(walletErrorMessage(new Error("boom"))).toBe("Wallet sign-in failed: boom");
    expect(walletErrorMessage("x")).toBe("Wallet sign-in failed: x");
  });
});

describe("signInWithWallet", () => {
  test("nonce -> personal_sign(hex message, address) -> verify", async () => {
    const calls: { method: string; params?: unknown[] }[] = [];
    const eth: Eip1193 = {
      request: async (a) => {
        calls.push(a);
        return a.method === "eth_requestAccounts" ? [ADDR] : SIG;
      },
    };
    const seen: string[] = [];
    const address = await signInWithWallet(eth, {
      authNonce: async (a) => {
        seen.push(`nonce:${a}`);
        return { nonce: "n0", message: "hello" };
      },
      authVerify: async (m, s) => {
        seen.push(`verify:${m}:${s}`);
        return { address: ADDR };
      },
    });
    expect(address).toBe(ADDR);
    expect(seen).toEqual([`nonce:${ADDR}`, `verify:hello:${SIG}`]);
    expect(calls[1]).toEqual({ method: "personal_sign", params: ["0x68656c6c6f", ADDR] });
  });
  test("no account and bad signature fail with a message", async () => {
    const dummy = { authNonce: async () => ({ nonce: "", message: "" }), authVerify: async () => ({ address: "" }) };
    await expect(signInWithWallet({ request: async () => [] }, dummy)).rejects.toThrow("no usable account");
    const badSig: Eip1193 = {
      request: async (a) => (a.method === "eth_requestAccounts" ? [ADDR] : "nope"),
    };
    await expect(signInWithWallet(badSig, dummy)).rejects.toThrow("invalid signature");
  });
});

describe("postJson", () => {
  test("sends JSON with same-origin credentials", async () => {
    let init: RequestInit | undefined;
    const out = await postJson<{ ok: boolean }>("/x", { a: 1 }, async (_p, i) => {
      init = i;
      return Response.json({ ok: true });
    });
    expect(out.ok).toBe(true);
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.body).toBe('{"a":1}');
  });
  test("errors carry path, status and the server's message", async () => {
    const p = postJson("/api/auth/verify", {}, async () => Response.json({ error: "bad nonce" }, { status: 401 }));
    await expect(p).rejects.toBeInstanceOf(ApiError);
    await expect(p).rejects.toThrow("POST /api/auth/verify failed with status 401: bad nonce");
    await expect(postJson("/x", {}, async () => new Response("no", { status: 500 }))).rejects.toThrow("status 500");
    await expect(
      postJson("/x", {}, async () => {
        throw new Error("down");
      }),
    ).rejects.toThrow("could not reach the server (down)");
  });
});

describe("mock auth", () => {
  test("full flow sets an HttpOnly session and /api/me follows it; logout clears it", () => {
    const a = auth();
    const nonce = a.handle("/api/auth/nonce", { address: ADDR }, null);
    expect(nonce?.status).toBe(200);
    const { message, nonce: n } = nonce?.body as { message: string; nonce: string };
    expect(message).toBe(siweMessage("http://127.0.0.1:8787", ADDR, n, "2026-10-13T09:00:00.000Z"));
    expect(message).toContain("wants you to sign in with your Ethereum account");
    const v = a.handle("/api/auth/verify", { message, signature: SIG }, null);
    expect(v?.status).toBe(200);
    expect(v?.body).toEqual({ address: ADDR });
    expect(v?.setCookie).toContain("HttpOnly");
    const cookie = (v?.setCookie ?? "").split(";")[0] ?? "";
    expect(a.sessionAddress(cookie)).toBe(ADDR);
    const me = route("/api/me", new URLSearchParams(), a.sessionAddress(cookie)).body as { sa_address: string; signed_in: boolean };
    expect(me).toMatchObject({ sa_address: ADDR, signed_in: true });
    expect(route("/api/me", new URLSearchParams()).body).toMatchObject({ signed_in: false });
    const out = a.handle("/api/auth/logout", {}, `x=1; ${cookie}`);
    expect(out?.setCookie).toContain("Max-Age=0");
    expect(a.sessionAddress(cookie)).toBeNull();
    expect(a.sessionAddress(null)).toBeNull();
    expect(cookie.startsWith(`${COOKIE}=`)).toBe(true);
  });
  test("rejects bad input and replayed nonces with a reason", () => {
    const a = auth();
    expect(a.handle("/api/auth/nonce", { address: "nope" }, null)?.status).toBe(400);
    expect(a.handle("/api/auth/verify", { message: 1 }, null)?.status).toBe(400);
    const { message } = a.handle("/api/auth/nonce", { address: ADDR }, null)?.body as { message: string };
    expect(a.handle("/api/auth/verify", { message, signature: "0x12" }, null)?.status).toBe(401);
    expect(a.handle("/api/auth/verify", { message, signature: SIG }, null)?.status).toBe(200);
    const again = a.handle("/api/auth/verify", { message, signature: SIG }, null);
    expect(again?.status).toBe(401);
    expect(JSON.stringify(again?.body)).toContain("nonce");
    expect(a.handle("/api/auth/other", {}, null)).toBeNull();
    expect(a.handle("/api/auth/nonce", "str", null)?.status).toBe(400);
  });
  test("meFor", () => {
    expect(meFor(null).signed_in).toBe(false);
    expect(meFor(ADDR)).toMatchObject({ signed_in: true, sa_address: ADDR });
  });
});
