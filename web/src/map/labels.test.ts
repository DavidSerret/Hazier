import { describe, expect, it } from "vitest";
import { visibleLabels, type LabelBox } from "./labels";

const label = (id: string, x: number, y: number, priority = 0): LabelBox => ({
  id,
  x,
  y,
  text: id,
  priority,
});

describe("visibleLabels", () => {
  it("keeps labels that do not overlap", () => {
    const shown = visibleLabels([label("aaaa", 50, 50), label("bbbb", 300, 50), label("cccc", 50, 200)]);
    expect([...shown].sort()).toEqual(["aaaa", "bbbb", "cccc"]);
  });
  it("hides the later of two overlapping labels", () => {
    expect([...visibleLabels([label("first", 100, 100), label("second", 110, 104)])]).toEqual(["first"]);
  });
  it("lets the highest priority win, whatever the order", () => {
    const shown = visibleLabels([label("first", 100, 100), label("selected", 110, 104, 10)]);
    expect([...shown]).toEqual(["selected"]);
  });
  it("keeps a hidden label's neighbour: only overlapping labels are removed", () => {
    // a and b overlap, b and c overlap, a and c do not: a wins, b goes, c stays.
    const shown = visibleLabels([label("aaaaaaaa", 100, 100), label("bbbbbbbb", 160, 100), label("cccccccc", 220, 100)]);
    expect([...shown].sort()).toEqual(["aaaaaaaa", "cccccccc"]);
  });
  it("separates labels once the map is zoomed in", () => {
    const close = [label("Singapore", 500, 300), label("Johor Bahru", 508, 296)];
    expect(visibleLabels(close).size).toBe(1);
    const zoomed = [label("Singapore", 500, 300), label("Johor Bahru", 700, 240)];
    expect(visibleLabels(zoomed).size).toBe(2);
  });
  it("handles no labels", () => {
    expect(visibleLabels([]).size).toBe(0);
  });
});
