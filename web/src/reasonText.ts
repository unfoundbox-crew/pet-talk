/**
 * reasonText — plain-language mapping for wire error reasons (Finding 8).
 *
 * The user surface never shows a raw wire reason (a server/ws.py error code
 * like `stt_empty_audio`). This is the one lookup table for turning those
 * into a sentence a non-developer can read; App.tsx and EyesAttach.tsx both
 * call it so there is exactly one vocabulary, not two that can drift apart.
 *
 * An unmapped reason still gets a plain sentence, never the raw code — the
 * wire vocabulary can grow on the server without every case here keeping up.
 */

const REASON_TEXT: Record<string, string> = {
  stt_empty_audio: "I didn't catch any audio.",
  empty_transcript: "I didn't catch that.",
  unknown_persona: "That voice isn't set up.",
  stt_failed: "I couldn't hear that clearly.",
  llm_route_failed: "I couldn't reach the model just now.",
  llm_timeout: "That took too long — try again.",
  tts_failed: "I couldn't say that out loud.",
  tts_timeout: "That took too long to speak.",
  eyes_bad_kind: "That file type isn't supported.",
  eyes_too_large: "That file is too large.",
  eyes_disabled: "Attachments aren't turned on right now.",
};

const GENERIC = "Something didn't go through.";

/** Plain sentence for a wire reason. Always returns something sayable. */
export function plainReason(reason?: string): string {
  if (!reason) return GENERIC;
  return REASON_TEXT[reason] ?? GENERIC;
}
