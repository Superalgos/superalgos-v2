# Strategy designer

The designer (`#/designer`) edits a strategy document as a graph. It has two views of the same document, switched in the toolbar: **Radial** (the default) and **Cards**. The choice is remembered in the browser (`localStorage` key `sa-designer-layout`). Layout never changes the strategy: moving, spreading or collapsing nodes does not edit the strategy file and does not appear in Undo.

## Run it

```sh
cd web && bun install && bun run build     # the demo serves web/dist
cd .. && uv run sa demo                    # http://127.0.0.1:8787, open #/designer
```

Without the API: `cd web && bun run mock` and `bun run dev` (Vite prints the URL). Pick an example under "Open example…" or open your own `.json` strategy.

## Radial view

The strategy is at the centre and every level of the tree is a ring around it: stages, events, situations, conditions; orders and phases with their formulas and events.

- Each node is its v1 icon (the type of node), with its title above and its type below. Hover a node, or select it, to see its condition or formula text.
- Rings: red dashed = a problem on this node; white with a turquoise halo = selected; orange dotted = a problem somewhere below it.
- Lines from parent to child are grey and dashed, and the dashes travel toward the parent. They stop if your system asks for reduced motion.
- **Collapse**: the small button on a node (`−` / `+N`) or a double click folds its subtree; `+N` is the number of nodes hidden below it. "Collapse all / Expand all" (top right) folds everything below the stages. A strategy with more than 40 nodes opens with its situations collapsed. Collapse state is not saved; choosing a problem in the Problems list opens the path to its node.

### Placing nodes relative to their parent

A node's place is an angle and a distance from its parent. The angle is measured from the parent's outward direction (from the grandparent to the parent; straight up for the strategy). So moving a node moves its whole subtree with it. With no settings the layout is automatic; settings are overrides.

Right-click a node: under the usual add / remove actions is a **Radial layout** section.

- **Spread: auto / 360° / 180° / 90° / 45°** (nodes with children): fan the children evenly over that arc, centred on the outward direction. Distance grows if needed so fanned siblings stay at least 150 px apart. Pinned children are left out of the fan.
- **Distance: ×0.25 / ×0.5 / ×1 / ×1.5 / ×2** (not the strategy): multiplies the distance to the parent. Ignored while the node is pinned.
- **Pin here / Unpin**: Pin fixes the node at its current angle and distance. **Dragging** a node does the same at the drop position (a drag shorter than 4 px counts as a click). The strategy node cannot be moved.
- **Reset layout of this branch**: clears the settings of the node and everything below it, collapsed nodes included.

The toolbar button **Reset radial layout** clears every radial setting. The context menu is kept inside the window: it opens upward or to the left near the bottom or right edge, and scrolls if it is taller than the window.

## Cards view

The earlier editor view: one card per node, tidy-tree positions you can drag freely. "Auto layout" resets those positions.

## What is stored where

**Never in the strategy file.** The strategy holds only the strategy (SEP-5.10); a test checks that its text is byte-identical before and after changing the layout.

- **Automatically**: the browser's `localStorage`, key `sa-designer-view:<strategy name>`, written about 300 ms after a layout change. Same content as a view file.
- **Save view / Load view…**: a separate `<name>.view.json`, version 2:

```json
{
  "view_version": 2,
  "strategy": "ladder",
  "positions": { "$.stages.open": { "x": 120, "y": 40 } },
  "radial": {
    "$.stages.open.orders[id=\"level_02\"]": { "spread": 90, "distance": 1.5, "pin": { "angle": 35, "distance": 260 } }
  }
}
```

`positions` are the Cards positions, keyed by JSON path. `radial` is keyed by a stable key: list items are identified by their own `id` (orders, manage rules, phases) or `name` (situations) when it is unique among their siblings, otherwise by their index. Inserting an order above a configured one keeps its settings on the same order; renaming its `id` loses them. `angle` and `spread` are degrees, `distance` is a factor, `pin.distance` is pixels. Version 1 view files (no `radial`) still load. Settings for nodes that no longer exist are dropped the next time the view is saved.

## Mouse and keyboard

| Action | Effect |
|---|---|
| Click a node | Select it; the Properties panel edits it |
| Click empty canvas | Select the strategy |
| Right-click a node | Context menu: add / remove / reorder as the schema allows, plus Radial layout options in Radial |
| Double-click a node (Radial) | Collapse or expand its subtree |
| Drag a node | Cards: move it. Radial: pin it (its subtree follows) |
| Scroll / drag empty canvas | Zoom / pan |
| Delete or Backspace | Remove the selected node, when the schema allows it |
| Cmd/Ctrl+Z, Cmd/Ctrl+Shift+Z | Undo / redo document edits (layout is not part of it) |
| Cmd/Ctrl+S | Save the strategy |
| Escape | Close the context menu |

## Notes and limits

- Event icons are a judgement: events use the matching v1 icon, "Close when" uses the close-stage event icon, the manage rule uses `managed-tasks`, any other event falls back to `execution-started-event`. The icons are in `web/src/assets/v1-icons/` (`SOURCE.md` lists their origin).
- Radial gets large for big strategies (about 3000 px across for the 93-node ladder) and relies on zoom and fit; collapse orders to shrink it.
- "Goto" edges (phase to a later phase) are plain children, not drawn as links.
- Fit view re-runs after every collapse or expand.
- Typing performance: the layout is cached on the structure of the visible graph, so editing a condition does not recompute it.

Code: `web/src/lib/designer/layouts.ts` (radial layout, collapse, cache), `radial.ts` (per-node settings), `view.ts` (view file), `menu-place.ts` (context menu placement), `web/src/components/designer/V1Canvas.tsx`.
