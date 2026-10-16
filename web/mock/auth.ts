// Mock of the EIP-4361 sign-in endpoints. It does NOT verify signatures (no crypto in the mock): it
// checks the message was issued by this server (known nonce, same address) and the signature is hex.

export const COOKIE = "sa_session";

export interface AuthDeps {
  now: () => Date;
  random: () => string;
  origin: string;
}

export interface Reply {
  status: number;
  body: unknown;
  setCookie?: string;
}

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function siweMessage(origin: string, address: string, nonce: string, issuedAt: string) {
  const host = new URL(origin).host;
  return [
    `${host} wants you to sign in with your Ethereum account:`,
    address,
    "",
    "Sign in to the Superalgos v2 dashboard. This proves you control this address; it moves no funds.",
    "",
    `URI: ${origin}`,
    "Version: 1",
    "Chain ID: 1",
    `Nonce: ${nonce}`,
    `Issued At: ${issuedAt}`,
  ].join("\n");
}

export function createAuth(deps: AuthDeps) {
  const nonces = new Map<string, string>(); // nonce -> address
  const sessions = new Map<string, string>(); // token -> address
  const fail = (status: number, error: string): Reply => ({ status, body: { error } });

  return {
    /** Address of the session carried by a Cookie header, if any. */
    sessionAddress(cookie: string | null | undefined): string | null {
      for (const part of (cookie ?? "").split(";")) {
        const [k, ...v] = part.trim().split("=");
        if (k === COOKIE) return sessions.get(v.join("=")) ?? null;
      }
      return null;
    },
    handle(path: string, body: unknown, cookie: string | null | undefined): Reply | null {
      const b = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
      if (path === "/api/auth/nonce") {
        const address = b.address;
        if (typeof address !== "string" || !ADDRESS.test(address)) {
          return fail(
            400,
            `address must be a 0x-prefixed 40-hex-digit EVM address, got ${JSON.stringify(address)}`,
          );
        }
        const nonce = deps.random();
        nonces.set(nonce, address.toLowerCase());
        return {
          status: 200,
          body: {
            nonce,
            message: siweMessage(deps.origin, address, nonce, deps.now().toISOString()),
          },
        };
      }
      if (path === "/api/auth/verify") {
        const { message, signature } = b;
        if (typeof message !== "string" || typeof signature !== "string") {
          return fail(400, "body must be {message: string, signature: string}");
        }
        const nonce = /^Nonce: (\S+)$/m.exec(message)?.[1];
        const address = message.split("\n")[1];
        const known = nonce ? nonces.get(nonce) : undefined;
        if (!nonce || !address || known !== address.toLowerCase()) {
          return fail(401, "unknown or reused nonce: request a new one with POST /api/auth/nonce");
        }
        if (!/^0x[0-9a-fA-F]{130}$/.test(signature)) {
          return fail(401, "signature must be 0x followed by 130 hex digits (65 bytes)");
        }
        nonces.delete(nonce);
        const token = deps.random();
        sessions.set(token, address);
        return {
          status: 200,
          body: { address },
          setCookie: `${COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/`,
        };
      }
      if (path === "/api/auth/logout") {
        for (const part of (cookie ?? "").split(";")) {
          const [k, ...v] = part.trim().split("=");
          if (k === COOKIE) sessions.delete(v.join("="));
        }
        return {
          status: 200,
          body: {},
          setCookie: `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`,
        };
      }
      return null;
    },
  };
}
