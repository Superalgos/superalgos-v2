import { useState } from "react";
import { api } from "../api/client";
import { type Eip1193, signInWithWallet, walletAvailable, walletErrorMessage } from "../api/wallet";
import { shortKey } from "../lib/format";

/** Shown only when a wallet (`window.ethereum`) exists. `onChange` fires after sign-in or sign-out. */
export function WalletSignIn({
  signedIn,
  address,
  onChange,
}: {
  signedIn: boolean;
  address: string;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!walletAvailable(window)) return null;
  const eth = (window as unknown as { ethereum: Eip1193 }).ethereum;
  const run = async (job: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await job();
      onChange();
    } catch (e) {
      setError(walletErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="wallet">
      {signedIn ? (
        <>
          <span>
            Signed in as <code title={address}>{shortKey(address, 8, 6)}</code>
          </span>
          <button type="button" disabled={busy} onClick={() => run(() => api.authLogout())}>
            Sign out
          </button>
        </>
      ) : (
        <button type="button" disabled={busy} onClick={() => run(() => signInWithWallet(eth, api))}>
          {busy ? "Waiting for wallet…" : "Sign in with wallet"}
        </button>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </div>
  );
}
