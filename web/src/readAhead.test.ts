import { describe, expect, it } from "vitest";
import {
  EMPTY_READ_AHEAD,
  advance,
  bufferedAhead,
  currentSentence,
  insertSentence,
  phaseOf,
  skipBack,
  skipForward,
  startIfIdle,
} from "./readAhead";

function three() {
  let s = EMPTY_READ_AHEAD;
  s = insertSentence(s, { seq: 0, text: "one", audioUrl: "/a/0" });
  s = insertSentence(s, { seq: 1, text: "two", audioUrl: "/a/1" });
  s = insertSentence(s, { seq: 2, text: "three", audioUrl: "/a/2" });
  return startIfIdle(s);
}

describe("read-ahead buffer ordering", () => {
  it("sorts out-of-order arrivals by seq without dropping any", () => {
    let s = EMPTY_READ_AHEAD;
    s = insertSentence(s, { seq: 2, text: "three", audioUrl: "/a/2" });
    s = insertSentence(s, { seq: 0, text: "one", audioUrl: "/a/0" });
    s = insertSentence(s, { seq: 1, text: "two", audioUrl: "/a/1" });
    expect(s.sentences.map((x) => x.text)).toEqual(["one", "two", "three"]);
  });

  it("keeps the head on the same sentence when one arrives behind it", () => {
    let s = EMPTY_READ_AHEAD;
    s = insertSentence(s, { seq: 5, text: "late", audioUrl: "/a/5" });
    s = startIfIdle(s);
    expect(currentSentence(s)?.text).toBe("late");
    s = insertSentence(s, { seq: 1, text: "earlier", audioUrl: "/a/1" });
    expect(s.sentences.map((x) => x.text)).toEqual(["earlier", "late"]);
    expect(currentSentence(s)?.text).toBe("late");
  });

  it("replaces a duplicate seq in place instead of listing it twice", () => {
    let s = three();
    s = insertSentence(s, { seq: 1, text: "two", audioUrl: "/a/1b", chunked: true });
    expect(s.sentences).toHaveLength(3);
    expect(s.sentences[1].audioUrl).toBe("/a/1b");
  });

  it("labels the phase of each sentence relative to the voice", () => {
    const s = advance(three()); // head on seq 1
    expect(phaseOf(s, 0)).toBe("spoken");
    expect(phaseOf(s, 1)).toBe("speaking");
    expect(phaseOf(s, 2)).toBe("buffered");
    expect(bufferedAhead(s).map((x) => x.text)).toEqual(["three"]);
  });
});

describe("arrow-skip moves the play head, never the queue", () => {
  it("right arrow moves the head forward and drops nothing", () => {
    const s = three();
    const after = skipForward(s);
    expect(after.head).toBe(1);
    expect(currentSentence(after)?.text).toBe("two");
    expect(after.sentences).toHaveLength(3);
    expect(after.sentences.map((x) => x.text)).toEqual(["one", "two", "three"]);
  });

  it("skipping to the end and back leaves every sentence in the buffer", () => {
    let s = three();
    s = skipForward(s);
    s = skipForward(s);
    expect(s.head).toBe(2);
    s = skipForward(s); // clamps: nothing left to skip to
    expect(s.head).toBe(2);
    s = skipBack(s);
    s = skipBack(s);
    expect(s.head).toBe(0);
    s = skipBack(s); // clamps at the first sentence
    expect(s.head).toBe(0);
    expect(s.sentences.map((x) => x.seq)).toEqual([0, 1, 2]);
  });

  it("a skipped sentence stays readable as spoken, not deleted", () => {
    const s = skipForward(three());
    expect(phaseOf(s, 0)).toBe("spoken");
    expect(s.sentences[0].text).toBe("one");
  });

  it("does nothing on an empty buffer", () => {
    expect(skipForward(EMPTY_READ_AHEAD)).toBe(EMPTY_READ_AHEAD);
    expect(skipBack(EMPTY_READ_AHEAD)).toBe(EMPTY_READ_AHEAD);
  });

  it("advance past the last sentence drains without losing history", () => {
    let s = three();
    s = advance(advance(advance(s)));
    expect(s.head).toBe(3);
    expect(currentSentence(s)).toBeNull();
    expect(s.sentences).toHaveLength(3);
  });
});
