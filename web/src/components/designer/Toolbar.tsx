import { useRef } from "react";
import { LAYOUT_MODES, type LayoutMode } from "../../lib/designer/layouts";
import type { ValidationState } from "../../lib/designer/validator";

export interface ToolbarProps {
  examples: readonly string[];
  fileName: string;
  dirty: boolean;
  validation: ValidationState;
  canUndo: boolean;
  canRedo: boolean;
  onExample: (file: string) => void;
  onNew: () => void;
  onOpenFile: (file: File) => void;
  onOpenView: (file: File) => void;
  onSave: () => void;
  onSaveView: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onResetLayout: () => void;
  /** Layout switch (view setting only); hidden when not given. */
  layoutMode?: LayoutMode;
  onLayoutMode?: (mode: LayoutMode) => void;
  /** Clears every Radial setting (spread, distance, pins); shown in Radial mode. */
  onResetRadial?: () => void;
}

export function ValidationBadge({ v }: { v: ValidationState }) {
  switch (v.status) {
    case "ok":
      return <span className="badge ok">valid</span>;
    case "invalid":
      return (
        <span className="badge bad">
          {v.errors.length} problem{v.errors.length === 1 ? "" : "s"}
        </span>
      );
    case "pending":
      return <span className="badge">checking…</span>;
    case "unavailable":
      return (
        <span className="badge warn" title={v.message}>
          not validated
        </span>
      );
    default:
      return <span className="badge">not checked</span>;
  }
}

export function Toolbar(p: ToolbarProps) {
  const fileIn = useRef<HTMLInputElement>(null);
  const viewIn = useRef<HTMLInputElement>(null);
  return (
    <div className="tb" role="toolbar" aria-label="Designer">
      <label className="sr" htmlFor="tb-example">
        Open an example
      </label>
      <select
        id="tb-example"
        value=""
        onChange={(e) => e.target.value && p.onExample(e.target.value)}
      >
        <option value="">Open example…</option>
        {p.examples.map((f) => (
          <option key={f} value={f}>
            {f}
          </option>
        ))}
      </select>
      <button type="button" onClick={() => fileIn.current?.click()}>
        Open file…
      </button>
      <input
        ref={fileIn}
        type="file"
        accept=".json,application/json"
        hidden
        aria-label="Open a strategy JSON file"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) p.onOpenFile(f);
          e.target.value = "";
        }}
      />
      <button type="button" onClick={p.onNew}>
        New
      </button>
      <span className="tb-sep" />
      <button type="button" onClick={p.onUndo} disabled={!p.canUndo}>
        Undo
      </button>
      <button type="button" onClick={p.onRedo} disabled={!p.canRedo}>
        Redo
      </button>
      <span className="tb-sep" />
      <button type="button" className="primary" onClick={p.onSave}>
        Save
      </button>
      <button
        type="button"
        onClick={p.onSaveView}
        title="Download the node positions as a separate view file"
      >
        Save view
      </button>
      <button type="button" onClick={() => viewIn.current?.click()}>
        Load view…
      </button>
      <input
        ref={viewIn}
        type="file"
        accept=".json,application/json"
        hidden
        aria-label="Open a view (layout) file"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) p.onOpenView(f);
          e.target.value = "";
        }}
      />
      <button type="button" onClick={p.onResetLayout}>
        Auto layout
      </button>
      {p.layoutMode && p.onLayoutMode && (
        <fieldset className="tb-seg" aria-label="Layout">
          {LAYOUT_MODES.map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={p.layoutMode === m}
              onClick={() => p.onLayoutMode?.(m)}
            >
              {m === "cards" ? "Cards" : "Radial"}
            </button>
          ))}
        </fieldset>
      )}
      {p.layoutMode === "radial" && p.onResetRadial && (
        <button
          type="button"
          onClick={p.onResetRadial}
          title="Clear every spread, distance and pin set in the Radial layout"
        >
          Reset radial layout
        </button>
      )}
      <span className="tb-name">
        {p.fileName}
        {p.dirty ? " ●" : ""}
      </span>
      <ValidationBadge v={p.validation} />
    </div>
  );
}
