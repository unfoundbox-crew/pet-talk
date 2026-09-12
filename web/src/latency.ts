/**
 * latency — the per-turn latency bar's numbers, and where they come from.
 *
 * `qa/budgets.json` is the single source of truth for every budget in this
 * product (its own header says so). The bar imports that file directly rather
 * than carrying a second copy, so a budget change moves the UI's breach line
 * in the same commit as the QA gate's.
 *
 * Pure functions only: no React, no timers. The component renders what these
 * return; vitest asserts them against the same JSON the gate reads.
 */

import budgets from "../../qa/budgets.json";

/** Measured stage of one turn, in ms. `null` = not measured on this turn. */
export interface TurnTiming {
  /** user.stop → agent.stall: how long before she acknowledged. */
  stallMs: number | null;
  /** user.stop → first frame carrying playable audio (chunk or sentence). */
  firstAudioMs: number | null;
}

export const EMPTY_TIMING: TurnTiming = { stallMs: null, firstAudioMs: null };

/**
 * The two budgets the bar draws against, read off `qa/budgets.json`.
 *
 * `stall_ms` is a top-level budget. First-audio's written budget lives in the
 * measured block (`measured.first_audio_ms.budget`, the value the latest
 * measurement pass judged itself against); `turn_p50_ms` is the fallback so a
 * budgets.json without a measured block still draws a bar.
 */
export const STALL_BUDGET_MS: number = budgets.stall_ms;

export const FIRST_AUDIO_BUDGET_MS: number =
  (budgets.measured?.first_audio_ms as { budget?: number } | undefined)?.budget ??
  budgets.turn_p50_ms;

/** How long the bar stays after `agent.done` before it fades out. */
export const LATENCY_BAR_HOLD_MS = 4000;

export interface LatencySegment {
  key: "stall" | "first_audio";
  /** Developer-facing label. Never rendered on the user surface. */
  label: string;
  ms: number;
  budgetMs: number;
  /** 0–100. Pegs at 100 on breach — a breach is "outside", not "how far". */
  pct: number;
  breach: boolean;
  /** The class the bar fill wears. `is-breach` is the only red in the bar. */
  className: string;
}

export const SEGMENT_CLASS = "pt-lat-fill";
export const BREACH_CLASS = "is-breach";

function segment(
  key: LatencySegment["key"],
  label: string,
  ms: number,
  budgetMs: number,
): LatencySegment {
  const breach = ms > budgetMs;
  return {
    key,
    label,
    ms,
    budgetMs,
    pct: breach ? 100 : Math.max(0, Math.min(100, (ms / budgetMs) * 100)),
    breach,
    className: breach ? `${SEGMENT_CLASS} ${BREACH_CLASS}` : SEGMENT_CLASS,
  };
}

/**
 * The bar's segments for one finished turn. A stage that was not measured is
 * absent rather than drawn as zero — an unmeasured number is not a fast one.
 */
export function segmentsFor(timing: TurnTiming): LatencySegment[] {
  const out: LatencySegment[] = [];
  if (timing.stallMs !== null) {
    out.push(segment("stall", "stall", timing.stallMs, STALL_BUDGET_MS));
  }
  if (timing.firstAudioMs !== null) {
    out.push(
      segment("first_audio", "first audio", timing.firstAudioMs, FIRST_AUDIO_BUDGET_MS),
    );
  }
  return out;
}

/** True when any measured stage of the turn missed its budget. */
export function turnBreached(timing: TurnTiming): boolean {
  return segmentsFor(timing).some((s) => s.breach);
}
