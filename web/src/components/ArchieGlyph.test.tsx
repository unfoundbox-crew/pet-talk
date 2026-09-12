// Tests for ArchieGlyph (PLAN-2026-09-12 §C, Lane 5a). Rendered via
// react-dom/server's renderToStaticMarkup — no jsdom needed, and it matches
// how qa/test_archie_glyph.py drives this file from Python (a plain Node
// script, no browser). useEffect (the speaking beat's timers) never runs
// under static server rendering, so these snapshots cover exactly what a
// screen reader or a first paint would see: the per-state markup, not the
// beat animation.
import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { ArchieGlyph, ArchieGlyphState } from "./ArchieGlyph";

const STATES: ArchieGlyphState[] = ["idle", "listening", "speaking", "error"];

describe("ArchieGlyph — small form snapshots", () => {
  for (const state of STATES) {
    it(`renders the ${state} state`, () => {
      const markup = renderToStaticMarkup(<ArchieGlyph state={state} />);
      expect(markup).toMatchSnapshot();
    });
  }

  it("always forces the small form, never the full drawing", () => {
    for (const state of STATES) {
      const markup = renderToStaticMarkup(<ArchieGlyph state={state} />);
      expect(markup).toContain('data-size="small"');
    }
  });

  it("arrives bare — never with an accessory", () => {
    for (const state of STATES) {
      const markup = renderToStaticMarkup(<ArchieGlyph state={state} />);
      expect(markup).toContain('data-accessory="none"');
    }
  });

  it("is inline SVG, never an <img>", () => {
    const markup = renderToStaticMarkup(<ArchieGlyph state="idle" />);
    expect(markup).toContain("<svg");
    expect(markup).not.toContain("<img");
  });

  it("lamp is off only for error, on for every other state", () => {
    for (const state of STATES) {
      const markup = renderToStaticMarkup(<ArchieGlyph state={state} />);
      const expected = state === "error" ? 'data-lamp="off"' : 'data-lamp="on"';
      expect(markup).toContain(expected);
    }
  });

  it("carries a distinct accessibility label per state", () => {
    const labels = STATES.map((state) => {
      const markup = renderToStaticMarkup(<ArchieGlyph state={state} />);
      const match = markup.match(/aria-label="([^"]*)"/);
      expect(match).not.toBeNull();
      return match![1];
    });
    expect(new Set(labels).size).toBe(STATES.length);
    for (const label of labels) {
      expect(label.startsWith("Archie — ")).toBe(true);
    }
  });

  it("colourway defaults to C3 and accepts C4", () => {
    const c3 = renderToStaticMarkup(<ArchieGlyph state="idle" />);
    const c4 = renderToStaticMarkup(<ArchieGlyph state="idle" colourway="C4" />);
    expect(c3).toContain("archie-c3");
    expect(c4).toContain("archie-c4");
  });
});
