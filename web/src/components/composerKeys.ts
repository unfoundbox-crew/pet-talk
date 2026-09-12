/**
 * The prompt composer's keyboard contract, as one pure function.
 *
 * Enter sends. Shift+Enter (and Option+Enter, which macOS users reach for out
 * of habit) inserts a newline. Cmd+Enter and Ctrl+Enter send too, so the
 * muscle memory from every other composer keeps working. An IME's commit
 * keystroke sends nothing — `isComposing` is true while a candidate is open,
 * and eating that Enter would fire a turn on half a word.
 *
 * It lives outside the component on purpose: the web suite runs under plain
 * Node with no jsdom (see web/src/devSurface.test.tsx), so a decision that
 * only exists inside a React handler is a decision no test can reach. The
 * textarea's whole `onKeyDown` body is a call to `handleComposerKeyDown`.
 */

/** Just the fields read from a keydown — React's synthetic event satisfies it. */
export interface ComposerKeyEventLike {
  key: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  nativeEvent?: { isComposing?: boolean };
  preventDefault?: () => void;
}

/**
 * What a keystroke means to the composer.
 *
 * - `send`    — fire the turn
 * - `newline` — let the textarea insert a line break
 * - `empty`   — Enter with nothing to send: swallowed, no turn, no blank line
 * - `pass`    — not ours; the textarea handles it
 */
export type ComposerIntent = "send" | "newline" | "empty" | "pass";

export function composerIntent(e: ComposerKeyEventLike): ComposerIntent {
  if (e.key !== "Enter") return "pass";
  // An IME candidate window is open: this Enter commits the candidate.
  if (e.nativeEvent?.isComposing) return "pass";
  if (e.shiftKey || e.altKey) return "newline";
  return "send";
}

/**
 * The composer textarea's entire keydown behaviour. Returns what it did so a
 * test — and a future reader — can see the decision, not infer it.
 *
 * `onSend` is called at most once, always with trimmed text, never with an
 * empty string.
 */
export function handleComposerKeyDown(
  e: ComposerKeyEventLike,
  text: string,
  onSend: (text: string) => void,
): ComposerIntent {
  const intent = composerIntent(e);
  if (intent !== "send") return intent;
  // Past this point the keystroke is ours either way: a bare Enter must never
  // leave a stray newline in the box, whether or not there was text to send.
  e.preventDefault?.();
  const trimmed = text.trim();
  if (!trimmed) return "empty";
  onSend(trimmed);
  return "send";
}
