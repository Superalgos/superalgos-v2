import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { type Placement, placeMenu } from "../../lib/designer/menu-place";
import type { Action } from "../../lib/designer/rules";

/** An extra, view-only entry (Radial layout settings); `checked` marks the current choice. */
export interface MenuItem {
  label: string;
  checked?: boolean;
  onPick: () => void;
}

export interface ContextMenuProps {
  x: number;
  y: number;
  title: string;
  actions: readonly Action[];
  /** Entries under the document actions, headed by `extraTitle`. */
  extras?: readonly MenuItem[];
  extraTitle?: string;
  onPick: (a: Action) => void;
  onClose: () => void;
}

export function ContextMenu({
  x,
  y,
  title,
  actions,
  extras = [],
  extraTitle,
  onPick,
  onClose,
}: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<Placement>({ left: x, top: y });
  // measure after layout and move the menu inside the viewport before it is painted
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setPlace(
      placeMenu(x, y, el.offsetWidth, el.scrollHeight, window.innerWidth, window.innerHeight),
    );
  }, [x, y]);
  useEffect(() => {
    ref.current?.querySelector("button")?.focus();
    const down = (e: Event) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("keydown", key);
    };
  }, [onClose]);
  return (
    <div
      ref={ref}
      className="cm"
      role="menu"
      aria-label={`Actions for ${title}`}
      style={{
        left: place.left,
        top: place.top,
        ...(place.maxHeight === undefined ? {} : { maxHeight: place.maxHeight, overflowY: "auto" }),
      }}
    >
      <div className="cm-title">{title}</div>
      {actions.length === 0 && extras.length === 0 && (
        <div className="cm-empty">Nothing can be added or removed here.</div>
      )}
      {actions.map((a) => (
        <button
          key={`${a.type}:${a.label}`}
          type="button"
          role="menuitem"
          onClick={() => {
            onPick(a);
            onClose();
          }}
        >
          {a.label}
        </button>
      ))}
      {extras.length > 0 && extraTitle && <div className="cm-title">{extraTitle}</div>}
      {extras.map((m) => (
        <button
          key={m.label}
          type="button"
          role="menuitemradio"
          aria-checked={m.checked ?? false}
          onClick={() => {
            m.onPick();
            onClose();
          }}
        >
          {m.checked ? "✓ " : ""}
          {m.label}
        </button>
      ))}
    </div>
  );
}
