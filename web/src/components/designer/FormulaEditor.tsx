import { useId, useRef } from "react";
import { caretLine, type NodeIssue } from "../../lib/designer/errors";

export interface FormulaEditorProps {
  label: string;
  value: string;
  /** The schema's description: the functions and names the expression language accepts. */
  help?: string;
  issues: readonly NodeIssue[];
  /** The issues were computed for an earlier text; shown dimmed until the next answer. */
  stale: boolean;
  onChange: (value: string) => void;
}

/** A condition or formula: the text box, and under it each server error with a caret at its column (SEP-5.8). */
export function FormulaEditor({ label, value, help, issues, stale, onChange }: FormulaEditorProps) {
  const id = useId();
  const area = useRef<HTMLTextAreaElement>(null);
  const jump = (column: number | null) => {
    const el = area.current;
    if (!el) return;
    el.focus();
    if (column !== null) el.setSelectionRange(column, Math.min(column + 1, el.value.length));
  };
  return (
    <div className="fe">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        ref={area}
        className={issues.length ? "fe-bad" : ""}
        value={value}
        rows={3}
        spellCheck={false}
        aria-invalid={issues.length > 0}
        aria-describedby={issues.length ? `${id}-err` : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {issues.length > 0 && (
        <ul id={`${id}-err`} className={`fe-errors${stale ? " fe-stale" : ""}`}>
          {issues.map((i) => (
            <li key={`${i.column}:${i.message}`}>
              {i.column !== null && (
                <pre aria-hidden="true" className="fe-caret">
                  {value}
                  {"\n"}
                  {caretLine(value, i.column)}
                </pre>
              )}
              <button type="button" className="link" onClick={() => jump(i.column)}>
                {i.column === null ? "Problem" : `Column ${i.column}`}
              </button>
              : {i.message}
            </li>
          ))}
        </ul>
      )}
      {help && (
        <details className="small muted">
          <summary>Allowed functions and names</summary>
          <p>{help}</p>
        </details>
      )}
    </div>
  );
}
