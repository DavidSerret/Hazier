// Zone name declutter. Names that would overlap on screen are hidden, the selected zone's name
// first, then the others in list order. Run when the map stops moving, not every frame.

export type LabelBox = {
  id: string;
  x: number; // screen position of the label anchor, CSS pixels
  y: number;
  text: string;
  priority: number; // higher wins
};

const CHAR_WIDTH = 0.62; // average glyph width as a share of the font size, bold system font
const PADDING = 4;

/** Ids of the labels to draw: a greedy pick by priority, skipping any that overlap one picked. */
export function visibleLabels(labels: LabelBox[], fontSize = 12): Set<string> {
  const order = labels
    .map((label, index) => ({ label, index }))
    .sort((a, b) => b.label.priority - a.label.priority || a.index - b.index);
  const placed: { left: number; right: number; top: number; bottom: number }[] = [];
  const shown = new Set<string>();
  for (const { label } of order) {
    const half = (label.text.length * fontSize * CHAR_WIDTH) / 2 + PADDING;
    // The name hangs below its anchor (anchor is the top of the text).
    const box = {
      left: label.x - half,
      right: label.x + half,
      top: label.y,
      bottom: label.y + fontSize + 2 * PADDING,
    };
    const clash = placed.some(
      (p) => box.left < p.right && box.right > p.left && box.top < p.bottom && box.bottom > p.top,
    );
    if (!clash) {
      placed.push(box);
      shown.add(label.id);
    }
  }
  return shown;
}
