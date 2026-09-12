/**
 * ArchieGlyph — the notch/menu-bar glyph. Archie's small form, and only
 * that form: this component always forces `data-size="small"`, because it
 * always renders under `ARCHIE_SMALL_BELOW` (40px) — the point where, per
 * AgentWorth's own measurement, the hand-held torch stops reading as a
 * drawing and the SVG swaps to a lit disc with a soft halo at the paw and a
 * bigger nose (`agentworth/packages/ui/brand/archie/README.md`, "The size
 * rule").
 *
 * State IS the light, using the same `data-lamp` convention AgentWorth
 * already uses for sleeping/error poses:
 *
 *   idle       lit steady   (data-lamp="on",  no beat)
 *   listening  lit bright   (data-lamp="on",  no beat, just brighter)
 *   speaking   lit, beating in time with word arrival (data-lamp="on")
 *   error      off          (data-lamp="off" — the halo hides, the lens
 *                            fills with --archie-body, matching every other
 *                            "off" pose in archie.css)
 *
 * The beat is not a CSS loop — a loop cannot know when a sentence actually
 * lands. Pass `wordTimes` (the same shape as the server's `WordTime`, see
 * `web/src/ws.ts`) and this component schedules one brief "kick" per word,
 * timed off `start_ms`. With no `wordTimes` it falls back to one kick at
 * t=0 so "speaking" still visibly differs from "idle" the instant it turns
 * on. `prefers-reduced-motion: reduce` keeps the state change (idle vs.
 * listening vs. speaking vs. error still look different) and drops the beat
 * itself — see `../brand/archie-glyph.css`.
 *
 * Rules enforced here, not just by convention:
 *   - Bare. `front-sit.svg` already ships `data-accessory="none"`; this
 *     component never sets it to "lamp" or "goggles".
 *   - Inline SVG only, never an `<img>` — same reason as `Archie.tsx`: the
 *     colourways are CSS custom properties an `<img>` can't see.
 *   - Colours come only from `../brand/archie/archie.css` (vendored) via the
 *     colourway class; `archie-glyph.css` adds motion, never a new fill.
 * `qa/test_archie_glyph.py` snapshots one render per state and asserts the
 * four accessibility labels are distinct.
 */

import React, { useEffect, useRef } from "react";

import frontSit from "../brand/archie/front-sit.svg?raw";
import "../brand/archie/archie.css";
import "../brand/archie-glyph.css";

export type ArchieGlyphState = "idle" | "listening" | "speaking" | "error";

/** Structurally the same shape as `web/src/ws.ts`'s `WordTime` (owned by
 * lane 2) — kept as a local type instead of an import so this file has no
 * hard dependency on a file another lane is actively editing. */
export interface ArchieGlyphWordTime {
  start_ms: number;
}

export interface ArchieGlyphProps {
  state: ArchieGlyphState;
  /** C3 (default) everywhere; C4 inside dense chrome (the HUD, the inspector). */
  colourway?: "C3" | "C4";
  /** Rendered pixel size. Clamped below 40 — this is always the small form. */
  size?: number;
  /** Word arrival times for the speaking beat. Shape matches ws.ts's WordTime. */
  wordTimes?: ArchieGlyphWordTime[];
  className?: string;
}

const ARIA_LABEL: Record<ArchieGlyphState, string> = {
  idle: "Archie — idle",
  listening: "Archie — listening",
  speaking: "Archie — speaking",
  error: "Archie — error",
};

/** Always the small form's threshold minus one — see Archie.tsx's own
 * ARCHIE_SMALL_BELOW. Kept as a literal here rather than importing it, for
 * the same reduce-coupling reason as ArchieGlyphWordTime above. */
const MAX_GLYPH_SIZE = 39;

function buildMarkup(size: number, state: ArchieGlyphState): string {
  const clamped = Math.min(size, MAX_GLYPH_SIZE);
  const lamp = state === "error" ? "off" : "on";
  return frontSit
    .replace(/\swidth="[^"]*"/, ` width="${clamped}"`)
    .replace(/\sheight="[^"]*"/, ` height="${clamped}"`)
    .replace(/\sdata-size="[^"]*"/, ` data-size="small"`)
    .replace(/\sdata-lamp="[^"]*"/, ` data-lamp="${lamp}"`)
    .replace(/\saria-label="[^"]*"/, ` aria-label="${ARIA_LABEL[state]}"`);
}

export const ArchieGlyph: React.FC<ArchieGlyphProps> = ({
  state,
  colourway = "C3",
  size = 24,
  wordTimes,
  className = "",
}) => {
  const rootRef = useRef<HTMLSpanElement | null>(null);

  const markup = React.useMemo(() => buildMarkup(size, state), [size, state]);

  useEffect(() => {
    if (state !== "speaking") return undefined;
    const prefersReduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (prefersReduced) return undefined; // state change already applied; beat dropped.

    const el = rootRef.current;
    if (!el) return undefined;

    const beats = wordTimes && wordTimes.length > 0 ? wordTimes : [{ start_ms: 0 }];
    const timers: number[] = [];
    for (const beat of beats) {
      const onId = window.setTimeout(() => {
        el.classList.add("archie-glyph-kick");
        const offId = window.setTimeout(() => el.classList.remove("archie-glyph-kick"), 140);
        timers.push(offId);
      }, Math.max(0, beat.start_ms));
      timers.push(onId);
    }
    return () => {
      timers.forEach((id) => window.clearTimeout(id));
      el.classList.remove("archie-glyph-kick");
    };
  }, [state, wordTimes]);

  const classes = ["archie-glyph", `archie-glyph-${state}`, `archie-c${colourway.slice(1)}`, className]
    .filter(Boolean)
    .join(" ");

  return <span ref={rootRef} className={classes} dangerouslySetInnerHTML={{ __html: markup }} />;
};
