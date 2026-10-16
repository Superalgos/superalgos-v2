/** The designer's editing state as a pure reducer: document with undo, selection, node positions. */
import {
  getAt,
  type Json,
  type JsonObject,
  type Path,
  parsePath,
  pathToId,
  serialize,
  setAt,
} from "./doc";
import type { Positions } from "./graph";
import { amend, type History, initHistory, push, redo, undo } from "./history";
import type { RadialSettings } from "./radial";
import { type Action, applyAction, setField } from "./rules";
import { remapPositions } from "./view";

export interface DesignerState {
  history: History<JsonObject>;
  /** Id (JSON path) of the selected node. */
  selected: string;
  /** Node positions the user set; everything else is laid out automatically. Saved to the view, not the document. */
  positions: Positions;
  /** Radial layout settings by stable node key; view data like `positions`, never in the document. */
  radial: RadialSettings;
  fileName: string;
  /** Text of the document as opened or last saved: "unsaved changes" is `serialize(doc) !== baseline`. */
  baseline: string;
  lastEdit: { key: string; at: number } | null;
}

export type DesignerEvent =
  | {
      type: "load";
      doc: JsonObject;
      fileName: string;
      positions?: Positions;
      radial?: RadialSettings;
      text?: string;
    }
  | { type: "select"; id: string }
  | { type: "action"; action: Action }
  | { type: "field"; path: Path; key: string; value: Json | undefined; now: number }
  | { type: "text"; path: Path; value: string; now: number }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "positions"; positions: Positions }
  | { type: "resetLayout" }
  | { type: "radial"; radial: RadialSettings }
  | { type: "saved"; fileName: string };

/** Typing in the same field within this many ms is merged into one undo step. */
export const COALESCE_MS = 1000;

export function initState(
  doc: JsonObject,
  fileName: string,
  positions: Positions = {},
  text?: string,
  radial: RadialSettings = {},
): DesignerState {
  return {
    history: initHistory(doc),
    selected: "$",
    positions,
    radial,
    fileName,
    baseline: text ?? serialize(doc),
    lastEdit: null,
  };
}

function settle(s: DesignerState, history: History<JsonObject>): DesignerState {
  const sel = parsePath(s.selected);
  const alive = sel !== null && getAt(history.present, sel) !== undefined;
  return { ...s, history, selected: alive ? s.selected : "$", lastEdit: null };
}

function edit(
  s: DesignerState,
  next: Json,
  key: string,
  now: number,
  coalesce: boolean,
): DesignerState {
  const doc = next as JsonObject;
  const same = coalesce && s.lastEdit?.key === key && now - s.lastEdit.at < COALESCE_MS;
  const history = same ? amend(s.history, doc) : push(s.history, doc);
  return { ...s, history, lastEdit: { key, at: now } };
}

export function reduce(s: DesignerState, e: DesignerEvent): DesignerState {
  switch (e.type) {
    case "load":
      return initState(e.doc, e.fileName, e.positions, e.text, e.radial);
    case "select":
      return { ...s, selected: e.id };
    case "action": {
      const r = applyAction(s.history.present, e.action);
      return {
        ...s,
        history: push(s.history, r.doc as JsonObject),
        selected: pathToId(r.select),
        positions: r.view ? remapPositions(s.positions, r.view) : s.positions,
        lastEdit: null,
      };
    }
    case "field":
      return edit(
        s,
        setField(s.history.present, e.path, e.key, e.value),
        `${pathToId(e.path)}.${e.key}`,
        e.now,
        true,
      );
    case "text":
      return edit(s, setAt(s.history.present, e.path, e.value), pathToId(e.path), e.now, true);
    case "undo":
      return settle(s, undo(s.history));
    case "redo":
      return settle(s, redo(s.history));
    case "positions":
      return { ...s, positions: e.positions };
    case "radial":
      return { ...s, radial: e.radial };
    case "resetLayout":
      return { ...s, positions: {} };
    case "saved":
      return { ...s, fileName: e.fileName, baseline: serialize(s.history.present) };
  }
}
