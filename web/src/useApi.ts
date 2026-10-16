import { useEffect, useRef, useState } from "react";

export type Remote<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ok"; data: T };

export interface UseApiOptions {
  /**
   * Re-run `load` every this many ms while the tab is visible, and once immediately when it becomes
   * visible again. A refresh never goes back to "loading" and never replaces shown data with an
   * error. Omit for a one-shot load.
   */
  refreshMs?: number;
}

/** Opt-in for pages that should stay current: poll every 15 s. */
export const LIVE: UseApiOptions = { refreshMs: 15_000 };

const LOADING = { state: "loading" } as const;

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function same(a: unknown, b: unknown): boolean {
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

/** Load data when `key` changes. `load` may change identity freely between renders. */
export function useApi<T>(
  load: () => Promise<T>,
  key = "",
  { refreshMs }: UseApiOptions = {},
): Remote<T> {
  const [res, setRes] = useState<{ key: string; remote: Remote<T> }>({
    key,
    remote: { state: "loading" },
  });
  const loader = useRef(load);
  loader.current = load;
  // `gen` is bumped whenever the key changes or the hook unmounts: any response that was started
  // under an older value is dropped. `busy` is true while a request (initial or refresh) is in flight.
  const gen = useRef(0);
  const busy = useRef(false);

  useEffect(() => {
    gen.current += 1;
    const mine = gen.current;
    busy.current = true;
    setRes({ key, remote: { state: "loading" } });
    loader.current().then(
      (data) => {
        if (gen.current !== mine) return;
        busy.current = false;
        setRes({ key, remote: { state: "ok", data } });
      },
      (e: unknown) => {
        if (gen.current !== mine) return;
        busy.current = false;
        setRes({ key, remote: { state: "error", message: message(e) } });
      },
    );
    return () => {
      gen.current += 1;
      busy.current = false;
    };
  }, [key]);

  useEffect(() => {
    if (!refreshMs || refreshMs <= 0) return;
    const refresh = () => {
      if (busy.current || document.visibilityState === "hidden") return;
      const mine = gen.current;
      busy.current = true;
      loader.current().then(
        (data) => {
          if (gen.current !== mine) return;
          busy.current = false;
          setRes((cur) =>
            cur.key === key && cur.remote.state === "ok" && same(cur.remote.data, data)
              ? cur // unchanged: keep the reference so nothing downstream re-renders or redraws
              : { key, remote: { state: "ok", data } },
          );
        },
        (e: unknown) => {
          if (gen.current !== mine) return;
          busy.current = false;
          // keep the last good data; only an errored or empty state takes the new error
          setRes((cur) =>
            cur.key === key && cur.remote.state === "ok"
              ? cur
              : { key, remote: { state: "error", message: message(e) } },
          );
        },
      );
    };
    const timer = setInterval(refresh, refreshMs);
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [key, refreshMs]);

  // In the render where `key` changed, the effect has not run yet: the held result belongs to the
  // previous key and must not be presented as the answer for this one.
  return res.key === key ? res.remote : LOADING;
}
