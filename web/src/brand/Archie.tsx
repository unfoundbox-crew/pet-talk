/**
 * Archie — the full hound, ported from AgentWorth's `packages/ui/Archie.tsx`.
 *
 * Same component, same props, same rules — only the import paths changed, to
 * point at the vendored copies in `./archie/` (see that folder's README).
 * pet-talk does not get its own mascot; this brings AgentWorth's in.
 *
 * Placement carries over unchanged from AgentWorth's board (see
 * `agentworth/docs/DESIGN.md` section "Archie"): the full (non-small) hound
 * belongs on pet-talk's empty state, first run, and the app icon — never on
 * the receipt chip, never twice on one screen. He arrives bare (the default
 * accessory is "none") and colourway C3 is the default; C4 is for dense
 * chrome (the HUD, the inspector) where he must not out-shout the data.
 *
 * Inline SVG only, never an `<img>` — the colourways are CSS custom
 * properties and a document's custom properties do not reach inside an
 * image. The SVG files in `./archie/` stay the only place the drawing
 * exists; this component patches their markup rather than re-authoring it.
 *
 * `qa/test_archie_glyph.py` greps `web/src` for every place this component
 * (or the raw SVG files) is used, and fails if any surface renders it more
 * than once per screen or renders it on the receipt chip.
 */

import React from "react";

import frontSit from "./archie/front-sit.svg?raw";
import threeQuarter from "./archie/three-quarter.svg?raw";
import sideScent from "./archie/side-scent.svg?raw";
import digging from "./archie/digging.svg?raw";
import fetching from "./archie/fetching.svg?raw";
import dropping from "./archie/dropping.svg?raw";
import sleeping from "./archie/sleeping.svg?raw";
import errorPose from "./archie/error.svg?raw";

import "./archie/archie.css";

export type ArchiePose =
  | "front-sit"
  | "three-quarter"
  | "side-scent"
  | "digging"
  | "fetching"
  | "dropping"
  | "sleeping"
  | "error";

export type ArchieAccessory = "lamp" | "goggles" | "none";
export type ArchieColourway = "C1" | "C2" | "C3" | "C4";
export type ArchieMotion = "dig" | "run" | "found" | "error";

export const ARCHIE_ACCESSORIES: ArchieAccessory[] = ["lamp", "goggles", "none"];
export const ARCHIE_COLOURWAYS: ArchieColourway[] = ["C1", "C2", "C3", "C4"];

/** Bare, per AgentWorth's board. The head gear is a switch, not how he arrives. */
export const ARCHIE_DEFAULT_ACCESSORY: ArchieAccessory = "none";
/** C3 is pet-talk's default too — it is AgentWorth's, and pet-talk gets no new palette. */
export const ARCHIE_DEFAULT_COLOURWAY: ArchieColourway = "C3";

/**
 * Below this, the torch stops being a drawing and starts being a smudge.
 * Matches AgentWorth's own measured threshold exactly — this is the same
 * constant, not a re-derivation, and `ArchieGlyph.tsx` (pet-talk's own small
 * notch form) always renders under it.
 */
export const ARCHIE_SMALL_BELOW = 40;

const SOURCES: Record<ArchiePose, string> = {
  "front-sit": frontSit,
  "three-quarter": threeQuarter,
  "side-scent": sideScent,
  digging,
  fetching,
  dropping,
  sleeping,
  error: errorPose,
};

export function isAccessory(value: unknown): value is ArchieAccessory {
  return typeof value === "string" && (ARCHIE_ACCESSORIES as string[]).includes(value);
}

export function isColourway(value: unknown): value is ArchieColourway {
  return typeof value === "string" && (ARCHIE_COLOURWAYS as string[]).includes(value);
}

export interface ArchieProps {
  pose: ArchiePose;
  size?: number;
  accessory?: ArchieAccessory;
  colourway?: ArchieColourway;
  motion?: ArchieMotion;
  className?: string;
  /** Overrides the pose file's own aria-label. Pass "" for a decorative figure. */
  label?: string;
}

/**
 * One Archie pose, inlined rather than sourced through an `<img>`. See the
 * module doc comment above for the placement rules this must respect.
 */
export const Archie: React.FC<ArchieProps> = ({
  pose,
  size = 160,
  accessory = ARCHIE_DEFAULT_ACCESSORY,
  colourway = ARCHIE_DEFAULT_COLOURWAY,
  motion,
  className = "",
  label,
}) => {
  const markup = React.useMemo(() => {
    let svg = SOURCES[pose]
      .replace(/\swidth="[^"]*"/, ` width="${size}"`)
      .replace(/\sheight="[^"]*"/, ` height="${size}"`)
      .replace(/\sdata-accessory="[^"]*"/, ` data-accessory="${accessory}"`)
      .replace(
        /\sdata-size="[^"]*"/,
        ` data-size="${size < ARCHIE_SMALL_BELOW ? "small" : "full"}"`,
      );
    if (label !== undefined) {
      svg = label
        ? svg.replace(/\saria-label="[^"]*"/, ` aria-label="${label}"`)
        : svg.replace(/\srole="img"/, ' aria-hidden="true"').replace(/\saria-label="[^"]*"/, "");
    }
    return svg;
  }, [pose, size, accessory, label]);

  const classes = [
    "archie-figure",
    `archie-c${colourway.slice(1)}`,
    motion ? `archie-${motion}` : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return <span className={classes} dangerouslySetInnerHTML={{ __html: markup }} />;
};
