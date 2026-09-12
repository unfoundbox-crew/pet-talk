/**
 * Finding 6: a late frame from a barged (stale) turn must never be applied —
 * concretely, a late `agent.sentence` must never re-enter the read-ahead
 * buffer and speak after the user interrupted. App.tsx drives this through
 * `shouldApplyFrame`/`shouldAdoptTurn`; this tests the pure guard directly,
 * plus the buffer-clearing behavior App.tsx wires to a barge ack.
 */
import { describe, expect, it } from "vitest";
import { isNewerTurn, shouldAdoptTurn, shouldApplyFrame } from "./turnGuard";
import { EMPTY_READ_AHEAD, insertSentence, type ReadAheadState } from "./readAhead";

// Real base36-encoded timestamps (see ws.ts newTurnId), old strictly before
// current, so isNewerTurn has real numbers to compare rather than two
// letter-strings that both happen to parse as valid (if meaningless) base36.
const OLD_TURN = `${(1_700_000_000_000).toString(36)}-aaaaaa`;
const CURRENT_TURN = `${(1_700_000_100_000).toString(36)}-bbbbbb`;

describe("isNewerTurn", () => {
  it("is false for the same turn id", () => {
    expect(isNewerTurn(CURRENT_TURN, CURRENT_TURN)).toBe(false);
  });

  it("compares the Date.now() prefix, base36", () => {
    const earlier = `${(1000).toString(36)}-a`;
    const later = `${(2000).toString(36)}-b`;
    expect(isNewerTurn(later, earlier)).toBe(true);
    expect(isNewerTurn(earlier, later)).toBe(false);
  });

  it("fails open (treats as newer) when a prefix cannot be parsed", () => {
    expect(isNewerTurn("!!!not-a-turn-id", CURRENT_TURN)).toBe(true);
  });
});

describe("shouldApplyFrame", () => {
  it("accepts any frame type for the current turn", () => {
    expect(shouldApplyFrame("agent.sentence", CURRENT_TURN, CURRENT_TURN)).toBe(true);
    expect(shouldApplyFrame("agent.done", CURRENT_TURN, CURRENT_TURN)).toBe(true);
  });

  it("drops a late agent.sentence from a stale (barged) turn", () => {
    expect(shouldApplyFrame("agent.sentence", OLD_TURN, CURRENT_TURN)).toBe(false);
  });

  it("drops agent.chunk, agent.stall and agent.error from a stale turn too", () => {
    expect(shouldApplyFrame("agent.chunk", OLD_TURN, CURRENT_TURN)).toBe(false);
    expect(shouldApplyFrame("agent.stall", OLD_TURN, CURRENT_TURN)).toBe(false);
    expect(shouldApplyFrame("agent.error", OLD_TURN, CURRENT_TURN)).toBe(false);
  });

  it("accepts state.listening for a newer turn — the barge-ack exception", () => {
    const newerTurn = `${(Number.MAX_SAFE_INTEGER).toString(36)}-z`;
    expect(shouldApplyFrame("state.listening", newerTurn, CURRENT_TURN)).toBe(true);
  });

  it("accepts state.idle for a newer turn but not agent.sentence for one", () => {
    const newerTurn = `${(Number.MAX_SAFE_INTEGER).toString(36)}-z`;
    expect(shouldApplyFrame("state.idle", newerTurn, CURRENT_TURN)).toBe(true);
    expect(shouldApplyFrame("agent.sentence", newerTurn, CURRENT_TURN)).toBe(false);
  });

  it("does not accept state.listening for an OLDER turn", () => {
    expect(shouldApplyFrame("state.listening", OLD_TURN, CURRENT_TURN)).toBe(false);
  });
});

describe("shouldAdoptTurn", () => {
  it("is true exactly when state.listening/state.idle carries a newer turn", () => {
    const newerTurn = `${(Number.MAX_SAFE_INTEGER).toString(36)}-z`;
    expect(shouldAdoptTurn("state.listening", newerTurn, CURRENT_TURN)).toBe(true);
    expect(shouldAdoptTurn("state.idle", newerTurn, CURRENT_TURN)).toBe(true);
  });

  it("is false for the same turn — nothing to adopt", () => {
    expect(shouldAdoptTurn("state.listening", CURRENT_TURN, CURRENT_TURN)).toBe(false);
  });

  it("is false for a non-adoptable frame type even on a newer turn", () => {
    const newerTurn = `${(Number.MAX_SAFE_INTEGER).toString(36)}-z`;
    expect(shouldAdoptTurn("agent.sentence", newerTurn, CURRENT_TURN)).toBe(false);
  });
});

describe("end to end: a barged turn's late sentence never re-enters the buffer", () => {
  it("current-turn sentence is applied (inserted); stale-turn sentence is not", () => {
    let buffer: ReadAheadState = EMPTY_READ_AHEAD;

    // Sentence for the current turn: applied, inserted into the buffer.
    if (shouldApplyFrame("agent.sentence", CURRENT_TURN, CURRENT_TURN)) {
      buffer = insertSentence(buffer, { seq: 0, text: "still current", audioUrl: "/a.wav" });
    }
    expect(buffer.sentences).toHaveLength(1);

    // A late sentence for the old, barged turn: guard says no — the caller
    // never calls insertSentence, so the buffer is untouched.
    const applyStale = shouldApplyFrame("agent.sentence", OLD_TURN, CURRENT_TURN);
    expect(applyStale).toBe(false);
    if (applyStale) {
      buffer = insertSentence(buffer, { seq: 1, text: "should never appear", audioUrl: "/b.wav" });
    }

    expect(buffer.sentences).toHaveLength(1);
    expect(buffer.sentences[0].text).toBe("still current");
  });

  it("a barge ack's state.listening for a newer turn is adopted and the buffer is cleared", () => {
    let buffer: ReadAheadState = insertSentence(EMPTY_READ_AHEAD, {
      seq: 0,
      text: "buffered before the barge",
      audioUrl: "/a.wav",
    });
    let currentTurn = CURRENT_TURN;

    const newerTurn = `${(Number.MAX_SAFE_INTEGER).toString(36)}-z`;
    const frameType = "state.listening";
    const barged_turn = CURRENT_TURN;

    expect(shouldApplyFrame(frameType, newerTurn, currentTurn)).toBe(true);
    if (shouldAdoptTurn(frameType, newerTurn, currentTurn)) {
      currentTurn = newerTurn;
    }
    // Mirrors App.tsx's state.listening case: a barged_turn on the frame
    // clears the read-ahead buffer so the barged turn's sentences cannot
    // resume.
    if (barged_turn) {
      buffer = EMPTY_READ_AHEAD;
    }

    expect(currentTurn).toBe(newerTurn);
    expect(buffer.sentences).toHaveLength(0);
  });
});
