/**
 * Finding 10, Problem A: skip is otherwise purely local — the server keeps
 * synthesizing sentences the user has already skipped past. App.tsx's
 * `skip` callback now sends a plain `barge` (the wire has no `resume_from`
 * support today — see the report; `ClientFrame`'s `resume_from` field is
 * typed but unused). `sendBargeFrame` is the exact call `skip` makes; this
 * tests it against a socket stub so the assertion does not need to render
 * the whole app.
 */
import { describe, expect, it, vi } from "vitest";
import { sendBargeFrame, type ClientFrame } from "./ws";

describe("sendBargeFrame", () => {
  it("sends a plain barge frame for the given turn", () => {
    const sent: ClientFrame[] = [];
    const socketStub = { send: vi.fn((frame: ClientFrame) => sent.push(frame)) };

    sendBargeFrame(socketStub, "turn-abc123");

    expect(socketStub.send).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([{ type: "barge", turn_id: "turn-abc123" }]);
  });

  it("never sends resume_from — the server does not read it yet", () => {
    const sent: ClientFrame[] = [];
    const socketStub = { send: vi.fn((frame: ClientFrame) => sent.push(frame)) };

    sendBargeFrame(socketStub, "turn-xyz");

    expect(sent[0]).not.toHaveProperty("resume_from");
  });
});
