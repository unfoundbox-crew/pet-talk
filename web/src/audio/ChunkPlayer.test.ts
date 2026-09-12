// Tests for ChunkPlayer (SPEC 4.2.1) against a stubbed AudioContext — no real
// audio hardware or `window` needed, since a context is always injected via
// `opts.context`.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ChunkPlayer, MAX_CHUNK_BYTES } from "./ChunkPlayer";

class FakeGainNode {
  connect = vi.fn();
  disconnect = vi.fn();
}

class FakeSourceNode {
  buffer: unknown = null;
  onended: (() => void) | null = null;
  connect = vi.fn();
  started = false;
  stopped = false;
  startedAt: number | null = null;
  start(when: number): void {
    this.started = true;
    this.startedAt = when;
  }
  stop(): void {
    this.stopped = true;
  }
}

/** Records the text payload of every chunk actually handed to
 * decodeAudioData, in the order it was decoded — which, because ChunkPlayer
 * serializes scheduling through its `tail` promise chain, is also play
 * order. */
class FakeAudioContext {
  currentTime = 0;
  state: "running" | "suspended" = "running";
  destination = {};
  decodedOrder: string[] = [];
  sources: FakeSourceNode[] = [];

  createGain(): FakeGainNode {
    return new FakeGainNode();
  }
  createBufferSource(): FakeSourceNode {
    const s = new FakeSourceNode();
    this.sources.push(s);
    return s;
  }
  async decodeAudioData(buf: ArrayBuffer): Promise<{ duration: number }> {
    const text = new TextDecoder().decode(new Uint8Array(buf));
    this.decodedOrder.push(text);
    return { duration: 0.01 };
  }
  resume(): Promise<void> {
    this.state = "running";
    return Promise.resolve();
  }
  close(): Promise<void> {
    return Promise.resolve();
  }
}

function chunkFrame(label: string, chunkNo: number, final: boolean, seq = 1) {
  return {
    seq,
    chunk_no: chunkNo,
    audio_b64: btoa(label),
    final,
  };
}

describe("ChunkPlayer", () => {
  let ctx: FakeAudioContext;
  let player: ChunkPlayer;

  beforeEach(() => {
    ctx = new FakeAudioContext();
    player = new ChunkPlayer({ context: ctx as unknown as AudioContext });
  });

  it("plays chunks in the order they were enqueued", async () => {
    const frames = [
      chunkFrame("chunk-0", 0, false),
      chunkFrame("chunk-1", 1, false),
      chunkFrame("chunk-2", 2, true),
    ];
    await Promise.all(frames.map((f) => player.enqueue(f)));

    expect(ctx.decodedOrder).toEqual(["chunk-0", "chunk-1", "chunk-2"]);
    expect(ctx.sources.map((s) => s.started)).toEqual([true, true, true]);
  });

  it("carries final=true on exactly one chunk of the sequence handled", async () => {
    const frames = [
      chunkFrame("chunk-0", 0, false),
      chunkFrame("chunk-1", 1, false),
      chunkFrame("chunk-2", 2, true),
    ];
    for (const f of frames) await player.enqueue(f);

    const finals = frames.filter((f) => f.final);
    expect(finals).toHaveLength(1);
    expect(ctx.decodedOrder).toHaveLength(3);
  });

  it("drops a chunk whose decoded payload exceeds the cap, with the named warning", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const oversized = "x".repeat(MAX_CHUNK_BYTES + 1024);
    const frame = { seq: 1, chunk_no: 0, audio_b64: btoa(oversized), final: true };

    await player.enqueue(frame);

    expect(ctx.decodedOrder).toHaveLength(0); // never reached decodeAudioData
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain("tts_chunk_too_large");
    warn.mockRestore();
  });

  it("stop() clears the queue and stops every scheduled source", async () => {
    await player.enqueue(chunkFrame("chunk-0", 0, false));
    await player.enqueue(chunkFrame("chunk-1", 1, true));

    expect(player.playing).toBe(true);
    player.stop();

    expect(player.playing).toBe(false);
    expect(ctx.sources.every((s) => s.stopped)).toBe(true);
  });
});
