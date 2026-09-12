import React, { useCallback, useEffect, useRef, useState } from "react";
import { float32ToBase64Pcm16, studioTokenHeader } from "../ws";
import { handleComposerKeyDown } from "./composerKeys";

interface PromptComposerProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  connected?: boolean;
  serverUrl?: string;
  /** Fired on a 401 from POST /transcribe — App shows the shared banner. */
  onAuthError?: () => void;
  t: Record<string, string>;
}

// Left-aligned action rows. Layout only — spacing from the ladder token, so
// this stays exempt from the "no ad-hoc pixel" rule.
const actionRow: React.CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: "var(--pt-s2)",
};

export const PromptComposer: React.FC<PromptComposerProps> = ({
  onSend,
  disabled = false,
  connected = true,
  serverUrl,
  onAuthError,
  t,
}) => {
  const [text, setText] = useState("");
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [cleanProse, setCleanProse] = useState(true);
  const [recordSeconds, setRecordSeconds] = useState(0);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const timerRef = useRef<number | null>(null);
  const startTimeRef = useRef<number>(0);

  // Audio capture refs
  const micStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const recordedChunksRef = useRef<Float32Array[]>([]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      if (micStreamRef.current) {
        micStreamRef.current.getTracks().forEach((trk) => trk.stop());
      }
      if (audioCtxRef.current) {
        void audioCtxRef.current.close().catch(() => {});
      }
    };
  }, []);

  const handleTextChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    setErrorMessage(null);
    // Auto-adjust textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 160)}px`;
    }
  };

  /** The one send path: both the button and Enter land here. */
  const sendNow = (trimmed: string) => {
    if (!trimmed || disabled || !connected || isRecording || isTranscribing) return;
    onSend(trimmed);
    setText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  // Enter sends, Shift+Enter is a newline, Cmd/Ctrl+Enter sends. The decision
  // is in composerKeys.ts so the suite can assert it without a DOM.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    handleComposerKeyDown(e, text, sendNow);
  };

  const handleSend = () => sendNow(text.trim());

  // --- Dictation: Start Mic Recording ---
  const startDictation = useCallback(async () => {
    setErrorMessage(null);
    try {
      recordedChunksRef.current = [];
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      micStreamRef.current = stream;

      const Ctx: typeof AudioContext =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new Ctx({ sampleRate: 16000 });
      audioCtxRef.current = ctx;

      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);

      proc.onaudioprocess = (e) => {
        const inputData = e.inputBuffer.getChannelData(0);
        recordedChunksRef.current.push(new Float32Array(inputData));
      };

      src.connect(proc);
      const zeroGain = ctx.createGain();
      zeroGain.gain.value = 0;
      proc.connect(zeroGain);
      zeroGain.connect(ctx.destination);

      setIsRecording(true);
      setRecordSeconds(0);
      startTimeRef.current = Date.now();
      timerRef.current = window.setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTimeRef.current) / 1000);
        setRecordSeconds(elapsed);
      }, 1000);
    } catch (err: unknown) {
      console.error("[pet-talk] Dictate mic access error:", err);
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Microphone access error: ${msg}`);
      setIsRecording(false);
    }
  }, []);

  // --- Dictation: Stop Recording & Call POST /transcribe ---
  const stopDictation = useCallback(async () => {
    if (!isRecording) return;
    setIsRecording(false);
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }

    // Stop tracks and close audio context cleanly
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((trk) => trk.stop());
      micStreamRef.current = null;
    }
    if (audioCtxRef.current) {
      void audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }

    const totalLen = recordedChunksRef.current.reduce((acc, c) => acc + c.length, 0);
    if (totalLen === 0) {
      return;
    }

    const merged = new Float32Array(totalLen);
    let offset = 0;
    for (const chunk of recordedChunksRef.current) {
      merged.set(chunk, offset);
      offset += chunk.length;
    }
    recordedChunksRef.current = [];

    const pcmB64 = float32ToBase64Pcm16(merged);
    setIsTranscribing(true);

    try {
      const targetBase = serverUrl || window.location.origin;
      const apiEndpoint = `${targetBase.replace(/\/ws\/?$/, "")}/transcribe`;

      const resp = await fetch(apiEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...studioTokenHeader(),
        },
        body: JSON.stringify({
          pcm_b64: pcmB64,
          sample_rate: 16000,
          clean_prose: cleanProse,
        }),
      });

      if (resp.status === 401) {
        setErrorMessage("studio token missing or wrong");
        onAuthError?.();
        return;
      }

      const data = await resp.json();
      if (resp.ok && data.ok && data.text) {
        const transcribedText = data.text.trim();
        if (transcribedText) {
          // Insert smoothly at current cursor position
          const textarea = textareaRef.current;
          if (textarea) {
            const start = textarea.selectionStart ?? text.length;
            const end = textarea.selectionEnd ?? text.length;
            const prefix = text.slice(0, start);
            const suffix = text.slice(end);
            const spaceBefore = prefix.length > 0 && !prefix.endsWith(" ") ? " " : "";
            const spaceAfter = suffix.length > 0 && !suffix.startsWith(" ") ? " " : "";
            const newText = `${prefix}${spaceBefore}${transcribedText}${spaceAfter}${suffix}`;
            setText(newText);
            // Restore cursor position
            setTimeout(() => {
              textarea.focus();
              const newPos = start + spaceBefore.length + transcribedText.length;
              textarea.setSelectionRange(newPos, newPos);
              textarea.style.height = "auto";
              textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
            }, 0);
          } else {
            setText((prev) => (prev ? `${prev} ${transcribedText}` : transcribedText));
          }
        }
      } else {
        setErrorMessage(data.error || "Transcription failed");
      }
    } catch (err: unknown) {
      console.error("[pet-talk] Transcribe API error:", err);
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(`Transcribe error: ${msg}`);
    } finally {
      setIsTranscribing(false);
    }
  }, [isRecording, text, serverUrl, cleanProse]);

  const formatTimer = (sec: number) => {
    const m = Math.floor(sec / 60)
      .toString()
      .padStart(2, "0");
    const s = (sec % 60).toString().padStart(2, "0");
    return `${m}:${s}`;
  };

  const hasText = text.trim().length > 0;

  return (
    <div className="pt-section">
      {errorMessage && (
        <div className="pt-banner">
          <span>{errorMessage}</span>
          <button
            type="button"
            className="pt-btn pt-btn--icon pt-btn--ghost"
            onClick={() => setErrorMessage(null)}
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}

      <div className="pt-panel">
        {/* Recording active state banner */}
        {isRecording && (
          <div className="pt-chipline">
            <span className="pt-chip pt-chip--danger">{formatTimer(recordSeconds)}</span>
            <span className="pt-note">{t["recording"] || "Recording…"}</span>
            <button
              type="button"
              className="pt-btn pt-btn--primary"
              data-testid="dictate-done-button"
              onClick={stopDictation}
            >
              {t["done"] || "Done"}
            </button>
          </div>
        )}

        {/* Transcribing indicator */}
        {isTranscribing && (
          <div className="pt-chipline">
            <span className="pt-chip pt-chip--signal">{t["transcribing"] || "Transcribing…"}</span>
          </div>
        )}

        {/* Text input area */}
        <textarea
          ref={textareaRef}
          className="pt-input"
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
          placeholder={t["prompt_placeholder"] || "Ask anything, or click Dictate…"}
          disabled={disabled || !connected || isTranscribing}
          rows={1}
        />

        {/* Action button bar */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: "var(--pt-s3)",
            paddingTop: "var(--pt-s2)",
            borderTop: "var(--pt-hair) solid var(--mv-border-soft)",
          }}
        >
          {/* Left: Dictate Mic Button + Clean Prose pill */}
          <div style={actionRow}>
            {!isRecording ? (
              <>
                <button
                  type="button"
                  className="pt-btn pt-btn--ghost"
                  data-testid="dictate-mic-button"
                  onClick={startDictation}
                  disabled={disabled || !connected || isTranscribing}
                  title={t["dictate"] || "Dictate"}
                >
                  {/* Clean Mic SVG */}
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
                    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                    <line x1="12" y1="19" x2="12" y2="23" />
                    <line x1="8" y1="23" x2="16" y2="23" />
                  </svg>
                  <span>{t["dictate"] || "Dictate"}</span>
                </button>

                <button
                  type="button"
                  className={`pt-chip ${cleanProse ? "pt-chip--signal" : ""}`}
                  data-testid="clean-prose-toggle"
                  aria-pressed={cleanProse}
                  onClick={() => setCleanProse((prev) => !prev)}
                  title={cleanProse ? "Clean Prose enabled" : "Clean Prose disabled"}
                >
                  <span>{t["clean_prose"] || "Clean Prose"}</span>
                </button>
              </>
            ) : (
              <span className="pt-note">Speak clearly into microphone…</span>
            )}
          </div>

          {/* Right: Hint + Send Button */}
          <div style={actionRow}>
            <span className="pt-note pt-mono">Enter ↵</span>

            <button
              type="button"
              className="pt-btn pt-btn--icon"
              data-testid="send-prompt-button"
              onClick={handleSend}
              disabled={!hasText || disabled || !connected || isRecording || isTranscribing}
              title={t["send_prompt"] || "Send"}
            >
              {/* Sleek Send Up Arrow SVG */}
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="12" y1="19" x2="12" y2="5" />
                <polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
