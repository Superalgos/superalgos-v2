import type { Json, JsonObject, Path } from "../../lib/designer/doc";
import { getAt, isObject } from "../../lib/designer/doc";
import type { NodeIssue } from "../../lib/designer/errors";
import type { DNode, Graph } from "../../lib/designer/graph";
import type { Action } from "../../lib/designer/rules";
import { type PropSpec, propsOf, schemaAt } from "../../lib/designer/schema";
import { FormulaEditor } from "./FormulaEditor";

export interface PropertiesProps {
  doc: Json;
  graph: Graph;
  node: DNode;
  issues: readonly NodeIssue[];
  stale: boolean;
  actions: readonly Action[];
  onAction: (a: Action) => void;
  onField: (path: Path, key: string, value: Json | undefined) => void;
  onText: (path: Path, value: string) => void;
}

function FieldIssues({ issues, field }: { issues: readonly NodeIssue[]; field: string }) {
  const mine = issues.filter((i) => i.field === field || i.field?.startsWith(`${field}.`));
  if (mine.length === 0) return null;
  return (
    <ul className="fe-errors">
      {mine.map((i) => (
        <li key={i.message}>{i.message}</li>
      ))}
    </ul>
  );
}

/** The phase ids a `goto.to` may name: the phases of the list the goto lives in (`...phases[i].goto[j]`). */
function siblingPhaseIds(doc: Json, path: Path): string[] {
  const list = getAt(doc, path.slice(0, -3));
  return Array.isArray(list)
    ? list.flatMap((p) => (isObject(p) && typeof p.id === "string" ? [p.id] : []))
    : [];
}

function Field(props: {
  spec: PropSpec;
  value: Json | undefined;
  path: Path;
  doc: Json;
  issues: readonly NodeIssue[];
  def: string;
  onField: PropertiesProps["onField"];
}) {
  const { spec, value, path, def, onField } = props;
  const s = spec.resolved.schema;
  const id = `f-${path.join("-")}-${spec.key}`;
  const clearable = !spec.required;
  const range = [s.minimum ?? s.exclusiveMinimum, s.maximum ?? s.exclusiveMaximum];
  let input: React.ReactNode;
  if (def === "Goto" && spec.key === "to") {
    const ids = siblingPhaseIds(props.doc, path);
    const cur = typeof value === "string" ? value : "";
    input = (
      <select id={id} value={cur} onChange={(e) => onField(path, spec.key, e.target.value)}>
        {!ids.includes(cur) && <option value={cur}>{cur || "(choose a phase)"}</option>}
        {ids.map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
    );
  } else if (s.enum) {
    input = (
      <select
        id={id}
        value={typeof value === "string" ? value : ""}
        onChange={(e) =>
          onField(path, spec.key, e.target.value === "" ? undefined : e.target.value)
        }
      >
        {clearable && <option value="">(default)</option>}
        {s.enum.map((v) => (
          <option key={String(v)} value={String(v)}>
            {String(v)}
          </option>
        ))}
      </select>
    );
  } else if (s.type === "boolean") {
    input = (
      <input
        id={id}
        type="checkbox"
        checked={value === true}
        onChange={(e) => onField(path, spec.key, e.target.checked)}
      />
    );
  } else if (s.type === "number" || s.type === "integer") {
    input = (
      <input
        id={id}
        type="number"
        step="any"
        value={typeof value === "number" ? value : ""}
        onChange={(e) => {
          const t = e.target.value;
          if (t === "") return onField(path, spec.key, clearable ? undefined : 0);
          const n = Number(t);
          if (Number.isFinite(n)) onField(path, spec.key, n);
        }}
      />
    );
  } else if (s.const !== undefined) {
    input = <code>{String(s.const)}</code>;
  } else {
    input = (
      <input
        id={id}
        type="text"
        maxLength={s.maxLength}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onField(path, spec.key, e.target.value)}
      />
    );
  }
  return (
    <div className="pf">
      <label htmlFor={id}>{spec.key}</label>
      {input}
      {(range[0] !== undefined || range[1] !== undefined) && (
        <span className="small muted">
          {" "}
          {range[0] ?? "…"} to {range[1] ?? "…"}
        </span>
      )}
      {spec.resolved.description && <div className="small muted">{spec.resolved.description}</div>}
      <FieldIssues issues={props.issues} field={spec.key} />
    </div>
  );
}

export function Properties(p: PropertiesProps) {
  const { node, doc } = p;
  const value = getAt(doc, node.path);
  const r = schemaAt(node.path);
  const own = p.issues.filter((i) => i.field === null);
  const header = (
    <>
      <h2>{node.title}</h2>
      <p className="small muted">
        <code>{node.id}</code>
      </p>
    </>
  );
  if (node.kind === "condition" || node.kind === "formula") {
    return (
      <div>
        {header}
        <FormulaEditor
          label={
            node.kind === "condition"
              ? "Condition (true / false expression)"
              : `${node.title} (numeric formula)`
          }
          value={typeof value === "string" ? value : ""}
          help={r?.schema.description ?? r?.description}
          issues={own}
          stale={p.stale}
          onChange={(v) => p.onText(node.path, v)}
        />
        <ActionList actions={p.actions} onAction={p.onAction} />
      </div>
    );
  }
  const specs = r ? propsOf(r).filter((s) => s.kind === "scalar") : [];
  const obj: JsonObject = isObject(value) ? value : {};
  return (
    <div>
      {header}
      {r?.description && <p className="small muted">{r.description}</p>}
      {own.length > 0 && (
        <ul className="fe-errors">
          {own.map((i) => (
            <li key={i.message}>{i.message}</li>
          ))}
        </ul>
      )}
      {specs.map((s) => (
        <Field
          key={s.key}
          spec={s}
          value={obj[s.key]}
          path={node.path}
          doc={doc}
          issues={p.issues}
          def={node.def}
          onField={p.onField}
        />
      ))}
      {p.issues
        .filter(
          (i) =>
            i.field !== null &&
            !specs.some((s) => i.field === s.key || i.field?.startsWith(`${s.key}.`)),
        )
        .map((i) => (
          <p key={`${i.field}:${i.message}`} className="fe-errors">
            {i.field}: {i.message}
          </p>
        ))}
      <ActionList actions={p.actions} onAction={p.onAction} />
    </div>
  );
}

function ActionList({
  actions,
  onAction,
}: {
  actions: readonly Action[];
  onAction: (a: Action) => void;
}) {
  if (actions.length === 0) return null;
  return (
    <div className="pa">
      <h3>Structure</h3>
      {actions.map((a) => (
        <button key={`${a.type}:${a.label}`} type="button" onClick={() => onAction(a)}>
          {a.label}
        </button>
      ))}
    </div>
  );
}
