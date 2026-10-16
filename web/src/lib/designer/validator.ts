/** Debounced server-side validation of the whole document (`POST /api/strategy/validate`). */
import type { Json } from "./doc";
import type { Issue } from "./errors";

export interface ValidateResponse {
  ok: boolean;
  errors: Issue[];
}

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

/** One validate round trip. Throws with the status and what to do when the server cannot answer. */
export async function postValidate(
  doc: Json,
  fetcher: Fetcher = fetch,
  signal?: AbortSignal,
): Promise<ValidateResponse> {
  let res: Response;
  try {
    res = await fetcher("/api/strategy/validate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(doc),
      signal,
    });
  } catch (e) {
    throw new Error(
      `validation server unreachable (${e instanceof Error ? e.message : String(e)}). Start the API or 'bun run mock'; the document is not validated.`,
    );
  }
  if (!res.ok) {
    throw new Error(
      `POST /api/strategy/validate answered ${res.status}. The document is not validated; check the API log.`,
    );
  }
  const body = (await res.json()) as Partial<ValidateResponse>;
  if (typeof body.ok !== "boolean" || !Array.isArray(body.errors)) {
    throw new Error(
      "POST /api/strategy/validate returned an unexpected body (expected {ok, errors}); the API and the dashboard disagree on the contract.",
    );
  }
  return { ok: body.ok, errors: body.errors };
}

export type ValidationState =
  | { status: "idle"; errors: Issue[] }
  /** A request is scheduled or in flight; `errors` are the last answer, now possibly stale. */
  | { status: "pending"; errors: Issue[] }
  | { status: "ok"; errors: Issue[] }
  | { status: "invalid"; errors: Issue[] }
  | { status: "unavailable"; errors: Issue[]; message: string };

export interface Timers {
  set(fn: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

const realTimers: Timers = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export interface Validator {
  /** Schedule a check of `doc` after the debounce; a newer call replaces an older one. */
  request(doc: Json): void;
  /** Check now (used before saving). */
  flush(): Promise<ValidationState>;
  state(): ValidationState;
  /** Stop delivering states and drop pending work. `resume` undoes it (React StrictMode mounts, unmounts and mounts again). */
  dispose(): void;
  resume(): void;
}

export function createValidator(opts: {
  validate: (doc: Json, signal: AbortSignal) => Promise<ValidateResponse>;
  onState: (s: ValidationState) => void;
  delayMs?: number;
  timers?: Timers;
}): Validator {
  const timers = opts.timers ?? realTimers;
  const delay = opts.delayMs ?? 400;
  let state: ValidationState = { status: "idle", errors: [] };
  let handle: unknown;
  let seq = 0;
  let abort: AbortController | null = null;
  let latest: Json | null = null;
  let disposed = false;

  const set = (s: ValidationState) => {
    state = s;
    if (!disposed) opts.onState(s);
  };

  const run = async (): Promise<ValidationState> => {
    if (latest === null) return state;
    const mine = ++seq;
    abort?.abort();
    abort = new AbortController();
    const prev = state.errors;
    try {
      const r = await opts.validate(latest, abort.signal);
      if (mine !== seq) return state; // a newer document superseded this answer
      set({ status: r.ok ? "ok" : "invalid", errors: r.errors });
    } catch (e) {
      if (mine !== seq) return state;
      set({
        status: "unavailable",
        errors: prev,
        message: e instanceof Error ? e.message : String(e),
      });
    }
    return state;
  };

  return {
    request(doc) {
      latest = doc;
      if (handle !== undefined) timers.clear(handle);
      set({ status: "pending", errors: state.errors });
      handle = timers.set(() => {
        handle = undefined;
        void run();
      }, delay);
    },
    async flush() {
      if (handle !== undefined) {
        timers.clear(handle);
        handle = undefined;
      }
      return run();
    },
    state: () => state,
    resume() {
      disposed = false;
    },
    dispose() {
      disposed = true;
      if (handle !== undefined) timers.clear(handle);
      abort?.abort();
      seq++;
    },
  };
}
