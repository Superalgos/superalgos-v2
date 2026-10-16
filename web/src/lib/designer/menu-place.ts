/**
 * Where a context menu goes so it stays fully inside the viewport (SEP-5.17). Pure: sizes in,
 * a position out.
 */
export interface Placement {
  left: number;
  top: number;
  /** Set only when the menu is taller than the viewport: it then scrolls inside. */
  maxHeight?: number;
}

export const MENU_MARGIN = 8;

function axis(at: number, size: number, view: number, margin: number): number {
  let pos = at;
  // would run past the far edge: flip so the menu ends at the click point
  if (pos + size > view - margin) pos = at - size;
  // still outside (or the click is beyond the edge): clamp
  return Math.max(margin, Math.min(pos, view - margin - size));
}

export function placeMenu(
  x: number,
  y: number,
  width: number,
  height: number,
  viewW: number,
  viewH: number,
  margin = MENU_MARGIN,
): Placement {
  const left = axis(x, width, viewW, margin);
  const room = viewH - 2 * margin;
  if (height > room) return { left, top: margin, maxHeight: Math.max(room, 0) };
  return { left, top: axis(y, height, viewH, margin) };
}
