/**
 * readAhead — the play head over a buffer of spoken sentences.
 *
 * The server streams `agent.sentence` frames faster than they can be spoken
 * (chunked TTS lands the first audio in ~130ms, a sentence takes seconds to
 * say). So at any moment the client holds sentences AHEAD of the one playing.
 * The cockpit shows them: buffered sentences render dim before they are
 * spoken, so you can read ahead of the voice and skip what you have read.
 *
 * The one law: **skipping never drops a frame.** Right arrow moves the play
 * head, it does not shift the queue. Everything that arrived is still in the
 * buffer and still in the transcript afterwards, which is why every function
 * here returns a new state of the SAME length (`insert` excepted, which grows
 * it by one).
 *
 * Pure, synchronous, no React and no audio. `App.tsx` maps a head move onto
 * the audio element; vitest drives this file directly.
 */

export interface BufferedSentence {
  /** Server sentence ordinal (`seq`, or `index` on the older contract). */
  seq: number;
  text: string;
  /** Resolved, playable URL. Empty for a sentence whose audio never landed. */
  audioUrl: string;
  /** True when the sentence's audio already played through agent.chunk. */
  chunked?: boolean;
}

export interface ReadAheadState {
  /** Arrival-independent: always sorted ascending by `seq`. */
  sentences: BufferedSentence[];
  /**
   * Index into `sentences` of the sentence being spoken now. `-1` before the
   * first sentence lands. Never exceeds `sentences.length` — at
   * `sentences.length` the buffer is drained and nothing is speaking.
   */
  head: number;
}

export const EMPTY_READ_AHEAD: ReadAheadState = { sentences: [], head: -1 };

/** Where a sentence sits relative to the voice. Drives its styling. */
export type SentencePhase = "spoken" | "speaking" | "buffered";

export function phaseOf(state: ReadAheadState, index: number): SentencePhase {
  if (index < state.head) return "spoken";
  if (index === state.head) return "speaking";
  return "buffered";
}

/** The sentences that arrived but have not been spoken yet. */
export function bufferedAhead(state: ReadAheadState): BufferedSentence[] {
  return state.sentences.slice(Math.max(state.head, 0) + (state.head < 0 ? 0 : 1));
}

export function currentSentence(state: ReadAheadState): BufferedSentence | null {
  return state.sentences[state.head] ?? null;
}

/**
 * Insert an arrived sentence in `seq` order.
 *
 * Out-of-order arrival is real (two synthesis workers, one slower), so the
 * buffer sorts on insert rather than trusting arrival order. A duplicate
 * `seq` replaces its payload in place instead of appearing twice, and the
 * play head follows the sentence it was on — inserting BEHIND the head must
 * not silently re-point the head at a different sentence.
 */
export function insertSentence(
  state: ReadAheadState,
  sentence: BufferedSentence,
): ReadAheadState {
  const existing = state.sentences.findIndex((s) => s.seq === sentence.seq);
  if (existing >= 0) {
    const sentences = [...state.sentences];
    sentences[existing] = { ...sentences[existing], ...sentence };
    return { sentences, head: state.head };
  }
  const headSeq = state.sentences[state.head]?.seq;
  const sentences = [...state.sentences, sentence].sort((a, b) => a.seq - b.seq);
  const head =
    headSeq === undefined
      ? state.head
      : sentences.findIndex((s) => s.seq === headSeq);
  return { sentences, head };
}

/** Move the head one sentence forward. Clamps at the end of the buffer. */
export function skipForward(state: ReadAheadState): ReadAheadState {
  if (state.sentences.length === 0) return state;
  const next = Math.min(state.head + 1, state.sentences.length - 1);
  return next === state.head ? state : { sentences: state.sentences, head: next };
}

/** Move the head one sentence back. Clamps at the first sentence. */
export function skipBack(state: ReadAheadState): ReadAheadState {
  if (state.sentences.length === 0) return state;
  const next = Math.max(state.head - 1, 0);
  return next === state.head ? state : { sentences: state.sentences, head: next };
}

/**
 * Natural end of a sentence's audio: the head advances into the buffer, or
 * past its end when nothing is buffered (head === length, nothing speaking,
 * no sentence lost).
 */
export function advance(state: ReadAheadState): ReadAheadState {
  if (state.sentences.length === 0) return state;
  return {
    sentences: state.sentences,
    head: Math.min(state.head + 1, state.sentences.length),
  };
}

/** First arrival: park the head on the first sentence if it is not placed. */
export function startIfIdle(state: ReadAheadState): ReadAheadState {
  if (state.head >= 0 || state.sentences.length === 0) return state;
  return { sentences: state.sentences, head: 0 };
}

/** A barge clears the turn. This is the only function allowed to drop frames. */
export function clearForBarge(): ReadAheadState {
  return EMPTY_READ_AHEAD;
}
