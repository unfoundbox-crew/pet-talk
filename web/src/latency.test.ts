import { describe, expect, it } from "vitest";
import budgets from "../../qa/budgets.json";
import {
  BREACH_CLASS,
  FIRST_AUDIO_BUDGET_MS,
  LATENCY_BAR_HOLD_MS,
  STALL_BUDGET_MS,
  segmentsFor,
  turnBreached,
} from "./latency";

describe("the latency bar reads its budgets off qa/budgets.json", () => {
  it("takes the stall budget from the file, not a literal", () => {
    expect(STALL_BUDGET_MS).toBe(budgets.stall_ms);
  });

  it("takes the first-audio budget from the measured block, else turn_p50_ms", () => {
    const written = (budgets.measured?.first_audio_ms as { budget?: number } | undefined)
      ?.budget;
    expect(FIRST_AUDIO_BUDGET_MS).toBe(written ?? budgets.turn_p50_ms);
  });

  it("holds for 4s before fading", () => {
    expect(LATENCY_BAR_HOLD_MS).toBe(4000);
  });
});

describe("breach colouring", () => {
  it("is not breached one ms under budget and pegs the fill proportionally", () => {
    const [stall] = segmentsFor({
      stallMs: STALL_BUDGET_MS - 1,
      firstAudioMs: null,
    });
    expect(stall.breach).toBe(false);
    expect(stall.className).not.toContain(BREACH_CLASS);
    expect(stall.pct).toBeCloseTo(((STALL_BUDGET_MS - 1) / STALL_BUDGET_MS) * 100, 5);
  });

  it("is breached one ms over budget and pegs at 100%", () => {
    const [stall] = segmentsFor({
      stallMs: STALL_BUDGET_MS + 1,
      firstAudioMs: null,
    });
    expect(stall.breach).toBe(true);
    expect(stall.className).toContain(BREACH_CLASS);
    expect(stall.pct).toBe(100);
  });

  it("treats exactly-at-budget as inside the budget", () => {
    const [stall] = segmentsFor({ stallMs: STALL_BUDGET_MS, firstAudioMs: null });
    expect(stall.breach).toBe(false);
  });

  it("scores the two stages against their own budgets", () => {
    const segs = segmentsFor({
      stallMs: STALL_BUDGET_MS + 250,
      firstAudioMs: FIRST_AUDIO_BUDGET_MS - 250,
    });
    expect(segs.map((s) => s.key)).toEqual(["stall", "first_audio"]);
    expect(segs[0].breach).toBe(true);
    expect(segs[1].breach).toBe(false);
    expect(segs[0].budgetMs).toBe(budgets.stall_ms);
    expect(turnBreached({ stallMs: STALL_BUDGET_MS + 250, firstAudioMs: 1 })).toBe(true);
  });

  it("omits a stage that was not measured instead of drawing it as zero", () => {
    expect(segmentsFor({ stallMs: null, firstAudioMs: null })).toEqual([]);
    const segs = segmentsFor({ stallMs: null, firstAudioMs: 120 });
    expect(segs).toHaveLength(1);
    expect(segs[0].key).toBe("first_audio");
    expect(turnBreached({ stallMs: null, firstAudioMs: null })).toBe(false);
  });
});
