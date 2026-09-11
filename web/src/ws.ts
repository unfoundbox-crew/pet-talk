// Typed WS client for the pet-talk frame protocol (TECH-SPEC §4).
// Backend WS lives at VITE_WS_URL, default ws://127.0.0.1:8089.
// (SpacePilot daemon is :8088 — do NOT collide.)

export const WS_URL =
  import.meta.env.VITE_WS_URL ?? "ws://127.0.0.1:8089/ws";

export type AgentState = "idle" | "listening" | "thinking" | "speaking";

export type PersonaId = "donna" | "zuck" | "jarvis";

// ---- Frames: client -> server ----
export type ClientFrame =
  | { type: "user.start"; turn_id: string; chunk: string; persona: PersonaId; voice: string; speed: number }
  | { type: "user.stop"; turn_id: string }
  | { type: "barge"; turn_id: string };

// ---- Frames: server -> client ----
export type ServerFrame =
  | { type: "agent.stall"; turn_id: string; phrase_id: string; audio_url?: string }
  | { type: "agent.sentence"; turn_id: string; index: number; text: string; audio_url: string }
  | { type: "agent.done"; turn_id: string }
  | { type: "state.idle"; turn_id: string }
  | { type: "state.listening"; turn_id: string }
  | { type: "state.thinking"; turn_id: string }
  | { type: "state.speaking"; turn_id: string }
  | { type: "transcript.user"; turn_id: string; text: string };

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

export class PetTalkSocket {
  private ws: WebSocket | null = null;
  private handlers = new Set<ServerHandler>();

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(url: string = WS_URL): void {
    this.close();
    this.ws = new WebSocket(url);
    this.ws.onmessage = (ev: MessageEvent) => {
      try {
        const frame = JSON.parse(String(ev.data)) as ServerFrame;
        this.handlers.forEach((h) => h(frame));
      } catch {
        // Ignore malformed frames; server contract is JSON (§4).
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

  send(frame: ClientFrame): void {
    if (this.connected) this.ws!.send(JSON.stringify(frame));
  }
}

export const socket = new PetTalkSocket();
