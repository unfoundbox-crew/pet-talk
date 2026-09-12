/**
 * The composer's keyboard contract, written before the fix.
 *
 * The footer hint says "Enter ↵" and the Send button worked, so Enter must
 * send. The decision lives in one pure function so it can be asserted
 * without a DOM: this repo's web tests run under plain Node (no jsdom, no
 * testing-library — see web/src/devSurface.test.tsx), and
 * `renderToStaticMarkup` cannot dispatch a keydown. The textarea's whole
 * `onKeyDown` body is `handleComposerKeyDown(...)`, so this is the real code
 * path, not a parallel copy of it.
 */
import { describe, expect, it, vi } from "vitest";

import { composerIntent, handleComposerKeyDown, type ComposerKeyEventLike } from "./composerKeys";

/** A keydown the way React hands one to onKeyDown, minus everything unread. */
const keyEvent = (over: Partial<ComposerKeyEventLike> = {}): ComposerKeyEventLike => ({
  key: "Enter",
  shiftKey: false,
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  preventDefault: vi.fn(),
  ...over,
});

describe("composerIntent — what a keystroke means", () => {
  it("reads a bare Enter as send", () => {
    expect(composerIntent(keyEvent())).toBe("send");
  });

  it("reads Cmd+Enter and Ctrl+Enter as send too", () => {
    expect(composerIntent(keyEvent({ metaKey: true }))).toBe("send");
    expect(composerIntent(keyEvent({ ctrlKey: true }))).toBe("send");
  });

  it("reads Shift+Enter and Option+Enter as a newline", () => {
    expect(composerIntent(keyEvent({ shiftKey: true }))).toBe("newline");
    expect(composerIntent(keyEvent({ altKey: true }))).toBe("newline");
  });

  it("leaves every other key alone", () => {
    for (const key of ["a", "Escape", "ArrowUp", "Tab", " "]) {
      expect(composerIntent(keyEvent({ key }))).toBe("pass");
    }
  });

  it("lets an IME commit its candidate instead of sending", () => {
    // Japanese/Chinese input: the first Enter commits the candidate and must
    // not also fire the turn.
    expect(composerIntent(keyEvent({ nativeEvent: { isComposing: true } }))).toBe("pass");
  });
});

describe("handleComposerKeyDown — Enter sends", () => {
  it("calls the send handler with the trimmed text", () => {
    const send = vi.fn();
    const e = keyEvent();
    expect(handleComposerKeyDown(e, "  is the gate green?  ", send)).toBe("send");
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("is the gate green?");
    // The newline must not also land in the box.
    expect(e.preventDefault).toHaveBeenCalledTimes(1);
  });

  it("sends on Cmd+Enter as well", () => {
    const send = vi.fn();
    expect(handleComposerKeyDown(keyEvent({ metaKey: true }), "ship it", send)).toBe("send");
    expect(send).toHaveBeenCalledWith("ship it");
  });

  it("does NOT send on Shift+Enter — that inserts a newline", () => {
    const send = vi.fn();
    const e = keyEvent({ shiftKey: true });
    expect(handleComposerKeyDown(e, "first line", send)).toBe("newline");
    expect(send).not.toHaveBeenCalled();
    // Default behaviour is the newline, so nothing is prevented.
    expect(e.preventDefault).not.toHaveBeenCalled();
  });

  it("does NOT send empty or whitespace-only text", () => {
    for (const text of ["", "   ", "\n", "\t \n"]) {
      const send = vi.fn();
      const e = keyEvent();
      expect(handleComposerKeyDown(e, text, send)).toBe("empty");
      expect(send).not.toHaveBeenCalled();
      // Enter on an empty box does nothing at all — not even a blank line.
      expect(e.preventDefault).toHaveBeenCalledTimes(1);
    }
  });

  it("does not send on a key that is not Enter", () => {
    const send = vi.fn();
    const e = keyEvent({ key: "a" });
    expect(handleComposerKeyDown(e, "hello", send)).toBe("pass");
    expect(send).not.toHaveBeenCalled();
    expect(e.preventDefault).not.toHaveBeenCalled();
  });
});
