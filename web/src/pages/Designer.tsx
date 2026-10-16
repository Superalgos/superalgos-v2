import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { BacktestPanel } from "../components/designer/BacktestPanel";
import { Canvas } from "../components/designer/Canvas";
import { ContextMenu, type MenuItem } from "../components/designer/ContextMenu";
import { Properties } from "../components/designer/Properties";
import { Toolbar } from "../components/designer/Toolbar";
import { type Json, type JsonObject, parseDoc, serialize } from "../lib/designer/doc";
import { fileNameFor, saveText } from "../lib/designer/download";
import { type Issue, indexIssues, mapIssues } from "../lib/designer/errors";
import { EXAMPLES, NEW_STRATEGY } from "../lib/designer/examples";
import { buildGraph, type Positions } from "../lib/designer/graph";
import { isLayoutMode, type LayoutMode } from "../lib/designer/layouts";
import {
  clearBranch,
  DISTANCES,
  patchSetting,
  pruneRadial,
  type RadialSettings,
  SPREADS,
  stableKeys,
} from "../lib/designer/radial";
import { type Action, actionsFor } from "../lib/designer/rules";
import { initState, reduce } from "../lib/designer/store";
import {
  createValidator,
  type Fetcher,
  postValidate,
  type ValidationState,
} from "../lib/designer/validator";
import {
  type KeyValueStore,
  loadViewFile,
  parseView,
  saveView,
  serializeView,
} from "../lib/designer/view";

export interface DesignerProps {
  /** Injected in tests; the browser's `fetch` and `localStorage` otherwise. */
  fetcher?: Fetcher;
  storage?: KeyValueStore;
  debounceMs?: number;
  pollMs?: number;
  /** Start from this document instead of the first example (tests). */
  initial?: { doc: JsonObject; fileName: string };
}

function browserStorage(): KeyValueStore {
  try {
    return window.localStorage;
  } catch {
    return { getItem: () => null, setItem: () => {} };
  }
}

const LAYOUT_KEY = "sa-designer-layout";

type Tab = "properties" | "backtest" | "problems";

export function DesignerPage({ fetcher, storage, debounceMs, pollMs, initial }: DesignerProps) {
  const store = useMemo(() => storage ?? browserStorage(), [storage]);
  const [state, dispatch] = useReducer(reduce, undefined, () => {
    const first = initial ?? {
      doc: (EXAMPLES[0] as (typeof EXAMPLES)[number]).doc as JsonObject,
      fileName: (EXAMPLES[0] as (typeof EXAMPLES)[number]).file,
    };
    const v = loadViewFile(store, String(first.doc.name ?? ""));
    return initState(first.doc, first.fileName, v.positions, undefined, v.radial);
  });
  const doc = state.history.present;
  const graph = useMemo(() => buildGraph(doc), [doc]);
  const keys = useMemo(() => stableKeys(doc, graph), [doc, graph]);
  const [validation, setValidation] = useState<ValidationState>({ status: "idle", errors: [] });
  const [extra, setExtra] = useState<Issue[]>([]);
  const [tab, setTab] = useState<Tab>("properties");
  const [menu, setMenu] = useState<{
    id: string;
    x: number;
    y: number;
    pin?: () => void;
  } | null>(null);
  const [focus, setFocus] = useState<{ id: string; nonce: number } | null>(null);
  const [fit, setFit] = useState(0);
  const [layoutMode, setLayoutMode] = useState<LayoutMode>(() => {
    const saved = store.getItem(LAYOUT_KEY);
    return isLayoutMode(saved) ? saved : "radial";
  });
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmSave, setConfirmSave] = useState<ValidationState | null>(null);

  const validatorRef = useRef<ReturnType<typeof createValidator> | null>(null);
  if (validatorRef.current === null) {
    validatorRef.current = createValidator({
      validate: (d, signal) => postValidate(d, fetcher, signal),
      onState: setValidation,
      delayMs: debounceMs,
    });
  }
  const validator = validatorRef.current;
  useEffect(() => {
    validator.resume();
    return () => validator.dispose();
  }, [validator]);
  useEffect(() => {
    validator.request(doc);
    setExtra([]);
  }, [doc, validator]);

  // node positions go to the view store (debounced), never into the document
  const name = typeof doc.name === "string" ? doc.name : "";
  useEffect(() => {
    const t = setTimeout(() => {
      const keep = Object.fromEntries(
        Object.entries(state.positions).filter(([id]) => graph.byId.has(id)),
      );
      const live = new Set(keys.values());
      saveView(store, name, keep, pruneRadial(state.radial, live));
    }, 300);
    return () => clearTimeout(t);
  }, [state.positions, state.radial, graph, keys, store, name]);

  const issues = useMemo(
    () => mapIssues([...validation.errors, ...extra], graph),
    [validation.errors, extra, graph],
  );
  const errors = useMemo(() => indexIssues(issues, graph), [issues, graph]);
  const node = graph.byId.get(state.selected) ?? graph.nodes[0];
  const dirty = serialize(doc) !== state.baseline;
  const stale = validation.status === "pending";

  // The canvas catches up after the keystroke: it reads deferred copies, so typing is never
  // queued behind a re-render of every node. Properties and the menu keep the live ones.
  const canvasGraph = useDeferredValue(graph);
  const canvasKeys = useDeferredValue(keys);
  const canvasErrors = useDeferredValue(errors);
  const onRadialChange = useCallback(
    (radial: RadialSettings) => dispatch({ type: "radial", radial }),
    [],
  );
  const onCanvasMenu = useCallback(
    (id: string, x: number, y: number, ops?: { pin: () => void }) =>
      setMenu({ id, x, y, pin: ops?.pin }),
    [],
  );
  const onMove = useCallback(
    (positions: Positions) => dispatch({ type: "positions", positions }),
    [],
  );
  const onMoveEnd = useCallback(() => {}, []);

  const act = useCallback((a: Action) => dispatch({ type: "action", action: a }), []);
  const selectedActions = useMemo(() => (node ? actionsFor(doc, node.path) : []), [doc, node]);

  const select = useCallback((id: string) => {
    dispatch({ type: "select", id });
    setMenu(null);
  }, []);

  const guardUnsaved = (): boolean =>
    !dirty || window.confirm("This strategy has unsaved changes. Discard them?");

  const load = (d: JsonObject, fileName: string, text?: string) => {
    const v = loadViewFile(store, String(d.name ?? ""));
    dispatch({ type: "load", doc: d, fileName, positions: v.positions, radial: v.radial, text });
    setNotice(null);
    setConfirmSave(null);
    setTab("properties");
    setFit((n) => n + 1);
  };

  const doSave = (valid: boolean) => {
    const file = fileNameFor(name, valid);
    saveText(file, serialize(doc));
    dispatch({ type: "saved", fileName: file });
    setConfirmSave(null);
    setNotice(
      valid
        ? `Saved ${file}.`
        : `Saved ${file} with problems: the file name says so. Fix the problems and save again.`,
    );
  };

  const save = async () => {
    const v = await validator.flush();
    if (v.status === "ok") doSave(true);
    else setConfirmSave(v);
  };

  // keyboard: undo/redo/save/delete, unless typing in a field
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandler.current = (e) => {
    const t = e.target as HTMLElement | null;
    const typing =
      !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT");
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "s") {
      e.preventDefault();
      void save();
    } else if (mod && e.key.toLowerCase() === "z" && !typing) {
      e.preventDefault();
      dispatch({ type: e.shiftKey ? "redo" : "undo" });
    } else if (!typing && (e.key === "Delete" || e.key === "Backspace")) {
      const rm = selectedActions.find((a) => a.type === "remove");
      if (rm) {
        e.preventDefault();
        act(rm);
      }
    }
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => keyHandler.current(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  const problems = issues.length;

  /** Radial layout entries of the right-click menu for node `id` (view settings only). */
  const radialItems = (id: string): MenuItem[] => {
    const n = graph.byId.get(id);
    const key = keys.get(id);
    if (!n || key === undefined) return [];
    const cur = state.radial[key] ?? {};
    const set = (patch: Parameters<typeof patchSetting>[2]) =>
      dispatch({ type: "radial", radial: patchSetting(state.radial, key, patch) });
    const items: MenuItem[] = [];
    if (n.children.length > 0) {
      items.push({
        label: "Spread: auto",
        checked: !cur.spread,
        onPick: () => set({ spread: null }),
      });
      for (const s of SPREADS)
        items.push({
          label: `Spread: ${s}°`,
          checked: cur.spread === s,
          onPick: () => set({ spread: s }),
        });
    }
    if (n.parent !== null) {
      for (const f of DISTANCES)
        items.push({
          label: `Distance: ×${f}`,
          checked: (cur.distance ?? 1) === f,
          onPick: () => set({ distance: f }),
        });
      if (cur.pin) items.push({ label: "Unpin", onPick: () => set({ pin: null }) });
      else if (menu?.pin) items.push({ label: "Pin here", onPick: menu.pin });
    }
    items.push({
      label: "Reset layout of this branch",
      onPick: () =>
        dispatch({ type: "radial", radial: clearBranch(state.radial, graph, keys, id) }),
    });
    return items;
  };
  const menuNode = menu ? graph.byId.get(menu.id) : undefined;

  return (
    <div className="designer">
      <Toolbar
        examples={EXAMPLES.map((e) => e.file)}
        fileName={state.fileName}
        dirty={dirty}
        validation={validation}
        canUndo={state.history.past.length > 0}
        canRedo={state.history.future.length > 0}
        onExample={(file) => {
          const ex = EXAMPLES.find((e) => e.file === file);
          if (ex && guardUnsaved()) load(structuredClone(ex.doc) as JsonObject, ex.file);
        }}
        onNew={() =>
          guardUnsaved() && load(structuredClone(NEW_STRATEGY) as JsonObject, "new-strategy.json")
        }
        onOpenFile={async (file) => {
          if (!guardUnsaved()) return;
          const text = await file.text();
          const r = parseDoc(text, file.name);
          if (r.ok) load(r.doc, file.name, text);
          else setNotice(r.error);
        }}
        onOpenView={async (file) => {
          const v = parseView(await file.text());
          if (!v)
            return setNotice(
              `${file.name} is not a designer view file (expected {"view_version": 2, "strategy", "positions", "radial"}, or a version 1 file). Pick a file saved with "Save view".`,
            );
          dispatch({ type: "positions", positions: v.positions });
          dispatch({ type: "radial", radial: v.radial });
          setNotice(
            v.strategy === name
              ? `Loaded the layout from ${file.name}.`
              : `Loaded ${file.name}, made for "${v.strategy}"; nodes that do not match are placed automatically.`,
          );
        }}
        onSave={() => void save()}
        onSaveView={() => {
          const keep = Object.fromEntries(
            Object.entries(state.positions).filter(([id]) => graph.byId.has(id)),
          );
          saveText(
            `${fileNameFor(name, true).replace(/\.json$/, "")}.view.json`,
            serializeView({
              strategy: name,
              positions: keep,
              radial: pruneRadial(state.radial, new Set(keys.values())),
            }),
          );
        }}
        layoutMode={layoutMode}
        onLayoutMode={(m) => {
          setLayoutMode(m);
          setFit((n) => n + 1);
          try {
            store.setItem(LAYOUT_KEY, m);
          } catch {
            // a view setting; not being able to remember it is harmless
          }
        }}
        onResetRadial={() => dispatch({ type: "radial", radial: {} })}
        onUndo={() => dispatch({ type: "undo" })}
        onRedo={() => dispatch({ type: "redo" })}
        onResetLayout={() => {
          dispatch({ type: "resetLayout" });
          setFit((n) => n + 1);
        }}
      />

      {notice && (
        <p className="notice" role="status">
          {notice}{" "}
          <button type="button" className="link" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </p>
      )}
      {confirmSave && (
        <div className="error" role="alertdialog" aria-label="Save with problems">
          <strong>
            {confirmSave.status === "unavailable"
              ? "The document could not be validated."
              : `The document has ${confirmSave.errors.length} problem(s).`}
          </strong>{" "}
          {confirmSave.status === "unavailable" && <span>{confirmSave.message} </span>}
          It will be saved as <code>{fileNameFor(name, false)}</code> so nobody mistakes it for a
          valid strategy.
          <div>
            <button type="button" onClick={() => doSave(false)}>
              Save anyway as {fileNameFor(name, false)}
            </button>{" "}
            <button type="button" onClick={() => setConfirmSave(null)}>
              Keep editing
            </button>
          </div>
        </div>
      )}

      <div className="dz">
        <div className="canvas" data-testid="canvas">
          <Canvas
            graph={canvasGraph}
            positions={state.positions}
            selected={state.selected}
            errors={canvasErrors}
            focus={focus}
            fit={fit}
            mode={layoutMode}
            onSelect={select}
            radial={state.radial}
            keys={canvasKeys}
            onRadialChange={onRadialChange}
            onContextMenu={onCanvasMenu}
            onMove={onMove}
            onMoveEnd={onMoveEnd}
          />
          {menu && menuNode && (
            <ContextMenu
              x={menu.x}
              y={menu.y}
              title={menuNode.title}
              actions={actionsFor(doc, menuNode.path)}
              extras={layoutMode === "radial" ? radialItems(menuNode.id) : undefined}
              extraTitle="Radial layout"
              onPick={act}
              onClose={() => setMenu(null)}
            />
          )}
        </div>
        <aside className="side" aria-label="Inspector">
          <div role="tablist" className="tabs">
            {(["properties", "backtest", "problems"] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                onClick={() => setTab(t)}
              >
                {t === "problems"
                  ? `Problems (${problems})`
                  : t === "backtest"
                    ? "Backtest"
                    : "Properties"}
              </button>
            ))}
          </div>
          {tab === "properties" && node && (
            <Properties
              doc={doc}
              graph={graph}
              node={node}
              issues={issues.filter((i) => i.nodeId === node.id)}
              stale={stale}
              actions={selectedActions}
              onAction={act}
              onField={(path, key, value) =>
                dispatch({ type: "field", path, key, value, now: Date.now() })
              }
              onText={(path, value) => dispatch({ type: "text", path, value, now: Date.now() })}
            />
          )}
          {tab === "backtest" && (
            <BacktestPanel
              doc={doc as Json}
              timeframe={typeof doc.timeframe === "string" ? doc.timeframe : "1h"}
              fetcher={fetcher}
              pollMs={pollMs}
              onIssues={setExtra}
              onTimeframe={(tf) =>
                dispatch({ type: "field", path: [], key: "timeframe", value: tf, now: Date.now() })
              }
            />
          )}
          {tab === "problems" && (
            <div>
              <h2>Problems</h2>
              {validation.status === "unavailable" && <p className="error">{validation.message}</p>}
              {problems === 0 && validation.status !== "unavailable" && (
                <p className="muted">
                  {validation.status === "ok"
                    ? "None: the server accepts this document."
                    : "None reported."}
                </p>
              )}
              <ul className="problems">
                {issues.map((i) => (
                  <li key={`${i.path}:${i.column}:${i.message}`}>
                    <button
                      type="button"
                      className="link"
                      onClick={() => {
                        select(i.nodeId);
                        setTab("properties");
                        setFocus({ id: i.nodeId, nonce: Date.now() });
                      }}
                    >
                      <code>{i.path}</code>
                      {i.column !== null && ` col ${i.column}`}
                    </button>
                    : {i.message}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
