/**
 * Self-reload when the server reports a different dashboard build than the one this tab runs.
 *
 * The build id is stamped into index.html at build time (<meta name="sa-build">, see vite.config.ts)
 * and the API repeats it in the X-SA-Build header of every response. A tab that sees another id is
 * running stale code against a newer API, so it reloads, at most once per reported id: the id it
 * reloaded for is kept in sessionStorage, so a reload that lands on the same old build (cache) or a
 * server that keeps reporting something odd cannot loop. No id on either side (dev server, mock,
 * API without a dashboard) means no checks; unusable storage means no reload, never a loop.
 */

export const BUILD_HEADER = "x-sa-build";
const STORAGE_KEY = "sa-build-reloaded";

export interface BuildEnv {
  /** id this tab was built with, or null when unknown */
  current: string | null;
  reload: () => void;
  storage: Pick<Storage, "getItem" | "setItem"> | null;
}

/** Returns a function to call with every served id (null when the response carried none). */
export function buildWatcher(env: BuildEnv): (served: string | null) => boolean {
  return (served) => {
    if (!env.current || !served || served === env.current) return false;
    try {
      if (!env.storage || env.storage.getItem(STORAGE_KEY) === served) return false;
      env.storage.setItem(STORAGE_KEY, served);
    } catch {
      return false;
    }
    env.reload();
    return true;
  };
}

function browserEnv(): BuildEnv {
  let storage: BuildEnv["storage"] = null;
  try {
    storage = window.sessionStorage;
  } catch {
    // storage blocked: stay on the current build
  }
  return {
    current: document.querySelector('meta[name="sa-build"]')?.getAttribute("content") ?? null,
    reload: () => window.location.reload(),
    storage,
  };
}

/** Look at a response from our own server; reloads the page if it reports a newer build. */
export function observeBuild(res: Pick<Response, "headers">): void {
  if (typeof document === "undefined") return;
  buildWatcher(browserEnv())(res.headers?.get(BUILD_HEADER) ?? null);
}

/** One cheap request (HEAD of the page itself) for tabs that have no polling left, e.g. after a crash. */
export async function checkBuild(fetcher: typeof fetch = fetch): Promise<void> {
  try {
    observeBuild(await fetcher("/", { method: "HEAD", cache: "no-store" }));
  } catch {
    // offline or server restarting: try again on the next tick
  }
}
