// Gapless playback of chunked TTS audio (SPEC 4.2.1).
//
// The server sends one `agent.chunk` per synthesis clause, each a complete WAV,
// while the rest of the sentence is still being synthesized. Playing them with
// `new Audio()` one after another puts a click and a few tens of milliseconds
// of silence between every clause — the element's own load-and-start is not
// schedulable. So each chunk is decoded and scheduled on one AudioContext
// timeline instead: chunk N+1 starts exactly where chunk N ends, and the
// sentence sounds like one utterance.
//
// A chunk that arrives after its slot has already passed (a slow decode, a
// stalled socket) starts at `currentTime` rather than in the past, which is a
// audible gap and honest, never a silently dropped clause.
//
// Barge: `stop()` kills every scheduled source immediately and forgets the
// queue. The server has already cancelled synthesis; this is the playback half.

/** One decoded chunk, in arrival order. */
interface Scheduled {
  seq: number;
  chunkNo: number;
  source: AudioBufferSourceNode;
  startAt: number;
  endAt: number;
}

export interface ChunkPlayerOptions {
  /** Shared context, so the player does not fight the rest of the app for the
   * browser's per-page AudioContext budget. One is made on demand otherwise. */
  context?: AudioContext;
  /** Seconds of slack before the first chunk, to absorb decode jitter on a
   * slow machine. Kept small: this is latency the listener pays. */
  leadSeconds?: number;
  /** Called when playback of the whole queue has drained. */
  onDrained?: () => void;
  /** Called with the named reason when a chunk could not be played. */
  onError?: (reason: string, detail: string) => void;
}

/** Client-side mirror of the server's chunk cap (SPEC 4.2.1). A chunk whose
 * decoded payload exceeds this is dropped rather than played — the server
 * refuses over-cap chunks on its side; this is the client half of that. */
export const MAX_CHUNK_BYTES = 512 * 1024;

/** base64 -> ArrayBuffer, without blowing the call stack on a long chunk. */
function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

export class ChunkPlayer {
  private ctx: AudioContext | null;
  private readonly ownsContext: boolean;
  private readonly leadSeconds: number;
  private readonly onDrained?: () => void;
  private readonly onError?: (reason: string, detail: string) => void;

  /** End of the audio scheduled so far, on the context clock. */
  private cursor = 0;
  private live: Scheduled[] = [];
  /** Serializes decode+schedule so two chunks cannot both claim the cursor. */
  private tail: Promise<void> = Promise.resolve();
  private stopped = false;
  private gain: GainNode | null = null;

  constructor(opts: ChunkPlayerOptions = {}) {
    this.ctx = opts.context ?? null;
    this.ownsContext = !opts.context;
    this.leadSeconds = opts.leadSeconds ?? 0.02;
    this.onDrained = opts.onDrained;
    this.onError = opts.onError;
  }

  /** True while any chunk is scheduled or playing. */
  get playing(): boolean {
    return this.live.length > 0;
  }

  private context(): AudioContext {
    if (!this.ctx) {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      this.ctx = new Ctor();
    }
    return this.ctx;
  }

  /**
   * Queue one `agent.chunk` for playback. Chunks play in the order they are
   * enqueued, which is the order the server sent them — `chunk_no` is not used
   * to reorder, because the WS delivers in order and a reorder buffer would add
   * latency to defend against something that cannot happen.
   *
   * Returns when the chunk is scheduled, not when it finishes playing.
   */
  async enqueue(frame: {
    seq: number;
    chunk_no: number;
    audio_b64: string;
    final: boolean;
  }): Promise<void> {
    const run = this.tail.then(() => this.schedule(frame));
    // Swallow here so one bad chunk cannot poison every later chunk's chain;
    // `schedule` has already reported the named reason.
    this.tail = run.catch(() => undefined);
    return run;
  }

  private async schedule(frame: {
    seq: number;
    chunk_no: number;
    audio_b64: string;
    final: boolean;
  }): Promise<void> {
    if (this.stopped) return;
    const ctx = this.context();
    if (ctx.state === "suspended") {
      // Autoplay policy: a context made before a user gesture starts suspended
      // and every scheduled source would play into the void.
      await ctx.resume().catch(() => undefined);
    }

    const raw = base64ToArrayBuffer(frame.audio_b64);
    if (raw.byteLength > MAX_CHUNK_BYTES) {
      console.warn(
        `[pet-talk] tts_chunk_too_large: seq=${frame.seq} chunk_no=${frame.chunk_no} bytes=${raw.byteLength} cap=${MAX_CHUNK_BYTES}`,
      );
      this.onError?.(
        "tts_chunk_too_large",
        `seq=${frame.seq} chunk_no=${frame.chunk_no} bytes=${raw.byteLength}`,
      );
      return;
    }

    let buffer: AudioBuffer;
    try {
      buffer = await ctx.decodeAudioData(raw);
    } catch (e) {
      this.onError?.(
        "chunk_decode_failed",
        `seq=${frame.seq} chunk_no=${frame.chunk_no}: ${String(e)}`,
      );
      return;
    }
    if (this.stopped) return;

    if (!this.gain) {
      this.gain = ctx.createGain();
      this.gain.connect(ctx.destination);
    }
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);

    const now = ctx.currentTime;
    // First chunk of a fresh run, or a chunk whose slot has already gone by.
    const startAt = this.cursor > now ? this.cursor : now + this.leadSeconds;
    const endAt = startAt + buffer.duration;
    this.cursor = endAt;

    const entry: Scheduled = {
      seq: frame.seq,
      chunkNo: frame.chunk_no,
      source,
      startAt,
      endAt,
    };
    this.live.push(entry);
    source.onended = () => {
      this.live = this.live.filter((s) => s !== entry);
      if (this.live.length === 0 && !this.stopped) this.onDrained?.();
    };
    source.start(startAt);
  }

  /**
   * Stop everything now and forget the queue. Safe to call when nothing is
   * playing, and safe to call twice. The player is reusable afterwards.
   */
  stop(): void {
    this.stopped = true;
    for (const s of this.live) {
      try {
        s.source.onended = null;
        s.source.stop();
      } catch {
        // Already finished; stopping a finished source throws in some engines.
      }
    }
    this.live = [];
    this.cursor = 0;
    this.tail = Promise.resolve();
    this.stopped = false;
  }

  /** Release the AudioContext, if this player made it. */
  async close(): Promise<void> {
    this.stop();
    this.gain?.disconnect();
    this.gain = null;
    if (this.ownsContext && this.ctx) {
      await this.ctx.close().catch(() => undefined);
      this.ctx = null;
    }
  }
}
