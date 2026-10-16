import { describe, expect, test } from "bun:test";
import fc from "fast-check";
import { MENU_MARGIN, placeMenu } from "../src/lib/designer/menu-place";

const W = 1000;
const H = 700;
const M = MENU_MARGIN;

describe("context menu placement (SEP-5.17)", () => {
  test("room all round: opens at the click point", () => {
    expect(placeMenu(100, 120, 200, 300, W, H)).toEqual({ left: 100, top: 120 });
  });

  test("near the right edge: flips left of the click point", () => {
    expect(placeMenu(900, 100, 200, 300, W, H)).toEqual({ left: 700, top: 100 });
  });

  test("near the bottom edge: flips up", () => {
    expect(placeMenu(100, 600, 200, 300, W, H)).toEqual({ left: 100, top: 300 });
  });

  test("near the left edge: stays at the margin, not off screen", () => {
    expect(placeMenu(-50, 100, 200, 300, W, H).left).toBe(M);
  });

  test("near the top edge: stays at the margin", () => {
    expect(placeMenu(100, -20, 200, 300, W, H).top).toBe(M);
  });

  test("bottom-right corner: flips both ways", () => {
    expect(placeMenu(990, 690, 200, 300, W, H)).toEqual({ left: 790, top: 390 });
  });

  test("flipping would leave the left edge: clamps inside", () => {
    // click at x=150 in a 180px viewport with a 200px menu: neither side fits; pinned to the margin
    expect(placeMenu(150, 10, 200, 100, 180, H).left).toBe(M);
  });

  test("taller than the viewport: pinned to the top and scrolls inside", () => {
    expect(placeMenu(100, 400, 200, 900, W, H)).toEqual({ left: 100, top: M, maxHeight: H - 2 * M });
  });

  test("a zero-height viewport does not give a negative maxHeight", () => {
    expect(placeMenu(0, 0, 10, 50, W, 0).maxHeight).toBe(0);
  });

  test("property: the menu is inside the viewport at every click point", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: -100, max: 1400 }),
        fc.integer({ min: -100, max: 1000 }),
        fc.integer({ min: 100, max: 400 }),
        fc.integer({ min: 40, max: 1200 }),
        (x, y, w, h) => {
          const p = placeMenu(x, y, w, h, W, H);
          const shown = p.maxHeight ?? h;
          expect(p.left).toBeGreaterThanOrEqual(M);
          expect(p.left + w).toBeLessThanOrEqual(W - M);
          expect(p.top).toBeGreaterThanOrEqual(M);
          expect(p.top + shown).toBeLessThanOrEqual(H - M);
        },
      ),
      { numRuns: 200 },
    );
  });
});
