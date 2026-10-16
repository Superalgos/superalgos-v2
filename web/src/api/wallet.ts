// Sign in with Ethereum (EIP-4361): the server builds the message, the wallet signs it, the server
// verifies and sets an HttpOnly session cookie. No key or secret ever reaches this code.

export interface Eip1193 {
  request(args: { method: string; params?: unknown[] }): Promise<unknown>;
}

export interface AuthApi {
  authNonce(address: string): Promise<{ nonce: string; message: string }>;
  authVerify(message: string, signature: string): Promise<{ address: string }>;
}

export function walletAvailable(w: object | undefined): boolean {
  const eth = (w as { ethereum?: unknown } | undefined)?.ethereum;
  return typeof eth === "object" && eth !== null;
}

/** personal_sign takes the UTF-8 message as 0x-prefixed hex (what every wallet accepts). */
export function utf8Hex(text: string): string {
  let out = "0x";
  for (const b of new TextEncoder().encode(text)) out += b.toString(16).padStart(2, "0");
  return out;
}

/** Turn a wallet or API failure into a message that says what happened and what to do. */
export function walletErrorMessage(e: unknown): string {
  const code = typeof e === "object" && e !== null ? (e as { code?: unknown }).code : undefined;
  const msg = e instanceof Error ? e.message : String(e);
  if (code === 4001) {
    return "You rejected the request in the wallet. Press 'Sign in with wallet' and approve it to continue.";
  }
  if (code === -32002) {
    return "The wallet already has a pending request. Open the wallet, finish or dismiss it, then try again.";
  }
  return `Wallet sign-in failed: ${msg}`;
}

/** The full flow. Returns the address the server verified. */
export async function signInWithWallet(eth: Eip1193, auth: AuthApi): Promise<string> {
  const accounts = await eth.request({ method: "eth_requestAccounts" });
  const address = Array.isArray(accounts) ? accounts[0] : undefined;
  if (typeof address !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(address)) {
    throw new Error(
      `the wallet returned no usable account (got ${JSON.stringify(accounts)}). Unlock the wallet and select an account.`,
    );
  }
  const { message } = await auth.authNonce(address);
  const signature = await eth.request({
    method: "personal_sign",
    params: [utf8Hex(message), address],
  });
  if (typeof signature !== "string" || !signature.startsWith("0x")) {
    throw new Error(
      `the wallet returned an invalid signature (${JSON.stringify(signature)}). Try again.`,
    );
  }
  const session = await auth.authVerify(message, signature);
  return session.address;
}
