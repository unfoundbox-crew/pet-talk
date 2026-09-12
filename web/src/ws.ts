// Typed WS client for the pet-talk frame protocol (TECH-SPEC §4).
// Backend WS lives at VITE_WS_URL, default ws://127.0.0.1:8089.
// (SpacePilot daemon is :8088 — do NOT collide.)

export const WS_URL =
  import.meta.env.VITE_WS_URL ?? "ws://127.0.0.1:8089/ws";

// ---- Studio token (server/auth.py contract) ----
// Every mutating HTTP route and the WS handshake require ``X-Studio-Token``.
// Browsers cannot set headers on `new WebSocket()`, so the WS form is
// `?token=`. Resolution order: build-time env VITE_STUDIO_TOKEN, else what
// the user typed into the Settings modal's "Connect" field (persisted to
// localStorage so it survives a reload), else nothing — a request made with
// no token gets the server's 401 / close 4401, which the UI turns into a
// visible banner rather than a silent retry.
const STUDIO_TOKEN_STORAGE_KEY = "pet_talk_studio_token";

export function studioToken(): string {
  const envToken = import.meta.env.VITE_STUDIO_TOKEN;
  if (envToken) return String(envToken);
  try {
    return localStorage.getItem(STUDIO_TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

/** Persists the token the user typed into the Settings "Connect" field. */
export function setStudioToken(token: string): void {
  try {
    if (token) localStorage.setItem(STUDIO_TOKEN_STORAGE_KEY, token);
    else localStorage.removeItem(STUDIO_TOKEN_STORAGE_KEY);
  } catch {
    // localStorage unavailable (private mode) — token still works this
    // page-load via VITE_STUDIO_TOKEN, just won't survive a reload.
  }
}

/** Spread onto every mutating fetch's `headers`. Empty when unset — the
 * server's 401 is what surfaces the missing-token banner. */
export function studioTokenHeader(): Record<string, string> {
  const token = studioToken();
  return token ? { "X-Studio-Token": token } : {};
}

function wsUrlWithToken(url: string): string {
  const token = studioToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

export type AgentState = "idle" | "listening" | "thinking" | "speaking";

/** One word's span in a sentence's audio. `estimated` is false only where the
 * backend returned real timing — see SPEC 5.2. */
export interface WordTime {
  word: string;
  start_ms: number;
  end_ms: number;
  estimated: boolean;
}

export type PersonaId = string;

// ---- Eyes (WAVE3 §1 / TECH-DESIGN Phase 4) ----
export type EyesKind = "screenshot" | "image" | "pdf";
export type EyesTask = "transcribe" | "describe";

/** Server-side cap; the client refuses oversize files before it ever sends. */
export const EYES_MAX_BYTES = 8 * 1024 * 1024;

/** Optional AX grounding the server may staple onto state.thinking. */
export interface ScreenGrounding {
  app?: string;
  window?: string;
  selection?: string | null;
  path?: string | null;
}

// ---- Frames: client -> server ----
export type ClientFrame =
  | {
      type: "user.start";
      turn_id: string;
      chunk?: string;
      persona?: PersonaId;
      voice?: string;
      speed?: number;
      custom_voice?: string;
      custom_speed?: number;
      custom_tone?: string;
      custom_stalls?: string[];
      system_prompt?: string;
    }
  | {
      type: "user.chunk";
      turn_id: string;
      chunk: string;
    }
  | {
      type: "user.stop";
      turn_id: string;
      pcm_b64?: string;
      sample_rate?: number;
    }
  | {
      type: "user.text";
      turn_id: string;
      text: string;
      persona?: PersonaId;
      voice?: string;
      speed?: number;
      custom_voice?: string;
      custom_speed?: number;
      custom_tone?: string;
      custom_stalls?: string[];
      system_prompt?: string;
    }
  | {
      type: "user.attach";
      turn_id: string;
      ref: string;
      kind: EyesKind;
      mime: string;
      b64: string;
      filename?: string;
      task?: EyesTask;
    }
  | { type: "barge"; turn_id: string };

// ---- Frames: server -> client ----
export type ServerFrame =
  | { type: "agent.stall"; turn_id: string; phrase_id: string; text?: string; audio_url?: string }
  | {
      type: "agent.sentence";
      turn_id: string;
      index?: number;
      seq?: number;
      text: string;
      audio_url: string;
      // Chunked TTS (SPEC 4.2.1). Optional so a server on the previous
      // contract still type-checks: `chunked` absent means whole-sentence.
      stream_url?: string | null;
      chunked?: boolean;
      word_times?: WordTime[];
      estimated?: boolean;
    }
  | {
      // One synthesis chunk, sent BEFORE the agent.sentence it belongs to.
      // `audio_b64` is a complete WAV file, playable on arrival; `url` serves
      // the same bytes. Exactly one chunk carries `final: true`.
      type: "agent.chunk";
      turn_id: string;
      seq: number;
      chunk_no: number;
      audio_b64: string;
      url?: string;
      final: boolean;
    }
  | { type: "agent.done"; turn_id: string; path?: string; sentences?: number }
  | { type: "agent.error"; turn_id: string; reason: string; detail?: string; ref?: string }
  | { type: "state.idle"; turn_id: string }
  | { type: "state.listening"; turn_id: string }
  | { type: "state.thinking"; turn_id: string; screen?: ScreenGrounding }
  | { type: "state.speaking"; turn_id: string }
  | { type: "transcript.user"; turn_id: string; text: string }
  | {
      type: "eyes.received";
      turn_id: string;
      ref: string;
      kind?: EyesKind;
      task?: EyesTask;
      bytes?: number;
    }
  | {
      type: "eyes.text";
      turn_id: string;
      ref: string;
      text: string;
      source?: string;
      kind?: EyesKind;
      task?: EyesTask;
      engine?: string;
      truncated?: boolean;
    };

export type ServerHandler = (frame: ServerFrame) => void;

export function newTurnId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** PCM16 mono bytes -> base64 (chunks from ScriptProcessor are Float32). */
export function float32ToBase64Pcm16(samples: Float32Array): string {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  const bytes = new Uint8Array(pcm.buffer);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

/** Map a File/Blob mime onto the three kinds the server accepts. */
export function eyesKindForMime(mime: string, hint?: "screenshot"): EyesKind | null {
  const m = (mime || "").toLowerCase();
  if (m === "application/pdf") return "pdf";
  if (!m.startsWith("image/")) return null;
  return hint === "screenshot" ? "screenshot" : "image";
}

export interface EyesAttachment {
  ref: string;
  kind: EyesKind;
  mime: string;
  b64: string;
  filename: string;
  bytes: number;
}

/** ArrayBuffer -> base64 without blowing the call stack on a big image. */
function bytesToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

/**
 * Read a picked / dropped / pasted file into a `user.attach` payload.
 * Fails closed with the same reason strings the server uses, so the UI shows
 * one vocabulary whether the client or the server rejected the file.
 */
export async function fileToAttachment(
  file: File | Blob,
  ref: string,
  hint?: "screenshot",
): Promise<EyesAttachment> {
  const filename = (file as File).name ?? "pasted";
  const kind = eyesKindForMime(file.type, hint);
  if (!kind) throw new Error("eyes_bad_kind");
  if (file.size > EYES_MAX_BYTES) throw new Error("eyes_too_large");
  const buf = await file.arrayBuffer();
  if (buf.byteLength > EYES_MAX_BYTES) throw new Error("eyes_too_large");
  return {
    ref,
    kind,
    mime: file.type,
    b64: bytesToBase64(buf),
    filename,
    bytes: buf.byteLength,
  };
}

let attachSeq = 0;
export function newAttachRef(): string {
  attachSeq += 1;
  return `att-${attachSeq}`;
}

export class PetTalkSocket {
  private ws: WebSocket | null = null;
  private handlers = new Set<ServerHandler>();
  // Close code 4401 = unauthorized (missing/wrong studio token, server/auth.py).
  // Kept separate from ServerHandler because it isn't a JSON frame — it's the
  // handshake itself failing, so there is no `onmessage` to have carried it.
  private authErrorHandlers = new Set<() => void>();

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(url: string = WS_URL): void {
    this.close();
    this.ws = new WebSocket(wsUrlWithToken(url));
    this.ws.onmessage = (ev: MessageEvent) => {
      try {
        const frame = JSON.parse(String(ev.data)) as ServerFrame;
        this.handlers.forEach((h) => h(frame));
      } catch {
        // Ignore malformed frames; server contract is JSON (§4).
      }
    };
    this.ws.onclose = (ev: CloseEvent) => {
      if (ev.code === 4401) {
        this.authErrorHandlers.forEach((h) => h());
      }
    };
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }

  onFrame(h: ServerHandler): () => void {
    this.handlers.add(h);
    return () => this.handlers.delete(h);
  }

  /** Fires when the WS handshake is closed with 4401 (unauthorized). */
  onAuthError(h: () => void): () => void {
    this.authErrorHandlers.add(h);
    return () => this.authErrorHandlers.delete(h);
  }

  send(frame: ClientFrame): void {
    if (this.connected) this.ws!.send(JSON.stringify(frame));
  }
}

export const socket = new PetTalkSocket();
