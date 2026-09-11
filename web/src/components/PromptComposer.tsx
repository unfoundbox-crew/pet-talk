import React, { useCallback, useEffect, useRef, useState } from "react";
import { float32ToBase64Pcm16 } from "../ws";

interface PromptComposerProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  connected?: boolean;
  serverUrl?: string;
  t: Record<string, string>;
}

export const PromptComposer: React.FC<PromptComposerProps> = ({
  onSend,
  disabled = false,
  connected = true,
  serverUrl,
  t,
}) => {
  const [text, setText] = useState("");
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
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

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled || !connected || isRecording || isTranscribing) return;
    onSend(trimmed);
    setText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

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
        },
        body: JSON.stringify({
          pcm_b64: pcmB64,
          sample_rate: 16000,
        }),
      });

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
  }, [isRecording, text, serverUrl]);

  const formatTimer = (sec: number) => {
    const m = Math.floor(sec / 60)
      .toString()
      .padStart(2, "0");
    const s = (sec % 60).toString().padStart(2, "0");
    return `${m}:${s}`;
  };

  const hasText = text.trim().length > 0;

  return (
    <div
      style={{
        width: "100%",
        display: "flex",
        flexDirection: "column",
        gap: "0.5rem",
        marginTop: "1.25rem",
      }}
    >
      {errorMessage && (
        <div
          style={{
            fontSize: "0.75rem",
            color: "#ff5252",
            background: "rgba(255, 82, 82, 0.1)",
            border: "1px solid rgba(255, 82, 82, 0.3)",
            borderRadius: "8px",
            padding: "0.4rem 0.8rem",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span>{errorMessage}</span>
          <button
            type="button"
            onClick={() => setErrorMessage(null)}
            style={{
              background: "none",
              border: "none",
              color: "#ff5252",
              cursor: "pointer",
              fontSize: "0.8rem",
              padding: 0,
            }}
          >
            ✕
          </button>
        </div>
      )}

      <div
        style={{
          background: "#12141c",
          border: isRecording ? "1px solid rgba(255, 61, 0, 0.5)" : "1px solid #282c3f",
          borderRadius: "16px",
          padding: "0.75rem 1rem",
          boxShadow: isRecording
            ? "0 0 20px rgba(255, 61, 0, 0.2), 0 4px 16px rgba(0,0,0,0.5)"
            : "0 4px 16px rgba(0,0,0,0.4)",
          transition: "border 0.2s, box-shadow 0.2s",
          display: "flex",
          flexDirection: "column",
          gap: "0.6rem",
        }}
      >
        {/* Recording active state banner */}
        {isRecording && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              background: "rgba(255, 61, 0, 0.1)",
              border: "1px solid rgba(255, 61, 0, 0.25)",
              borderRadius: "10px",
              padding: "0.4rem 0.75rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
              <span
                style={{
                  display: "inline-block",
                  width: "10px",
                  height: "10px",
                  borderRadius: "50%",
                  background: "#ff3d00",
                  boxShadow: "0 0 8px #ff3d00",
                  animation: "pulse 1.2s infinite",
                }}
              />
              <span
                style={{
                  fontSize: "0.85rem",
                  fontWeight: 600,
                  fontFamily: "monospace",
                  color: "#ff5252",
                  letterSpacing: "0.05em",
                }}
              >
                {formatTimer(recordSeconds)}
              </span>
              <span style={{ fontSize: "0.8rem", color: "#e0e0e0" }}>
                {t["recording"] || "Recording…"}
              </span>
            </div>

            <button
              type="button"
              data-testid="dictate-done-button"
              onClick={stopDictation}
              style={{
                background: "linear-gradient(135deg, #00c853, #00e676)",
                color: "#090a0f",
                border: "none",
                borderRadius: "999px",
                padding: "0.35rem 0.9rem",
                fontSize: "0.75rem",
                fontWeight: 700,
                cursor: "pointer",
                boxShadow: "0 2px 8px rgba(0, 200, 83, 0.4)",
                transition: "transform 0.1s",
              }}
            >
              ✓ {t["done"] || "Done"}
            </button>
          </div>
        )}

        {/* Transcribing indicator */}
        {isTranscribing && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.6rem",
              background: "rgba(36, 193, 224, 0.1)",
              border: "1px solid rgba(36, 193, 224, 0.3)",
              borderRadius: "10px",
              padding: "0.4rem 0.75rem",
              color: "#24c1e0",
              fontSize: "0.8rem",
              fontWeight: 500,
            }}
          >
            <span
              style={{
                display: "inline-block",
                width: "12px",
                height: "12px",
                border: "2px solid #24c1e0",
                borderTopColor: "transparent",
                borderRadius: "50%",
                animation: "spin 0.8s linear infinite",
              }}
            />
            <span>{t["transcribing"] || "Transcribing…"}</span>
          </div>
        )}

        {/* Text input area */}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
          placeholder={t["prompt_placeholder"] || "Ask Donna anything, or click Dictate…"}
          disabled={disabled || !connected || isTranscribing}
          rows={1}
          style={{
            width: "100%",
            background: "transparent",
            border: "none",
            outline: "none",
            resize: "none",
            color: "#f1f3f9",
            fontSize: "0.95rem",
            lineHeight: "1.45",
            fontFamily: "inherit",
            minHeight: "28px",
            maxHeight: "160px",
            boxSizing: "border-box",
          }}
        />

        {/* Action button bar */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            paddingTop: "0.25rem",
            borderTop: "1px solid #1a1e2b",
          }}
        >
          {/* Left: Dictate Mic Button */}
          <div>
            {!isRecording ? (
              <button
                type="button"
                data-testid="dictate-mic-button"
                onClick={startDictation}
                disabled={disabled || !connected || isTranscribing}
                title={t["dictate"] || "Dictate"}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.45rem",
                  background: "#191c26",
                  border: "1px solid #282c3f",
                  borderRadius: "999px",
                  padding: "0.4rem 0.85rem",
                  color: "#e2e8f0",
                  fontSize: "0.8rem",
                  fontWeight: 600,
                  cursor: disabled || !connected || isTranscribing ? "not-allowed" : "pointer",
                  transition: "background 0.15s, border-color 0.15s",
                  opacity: disabled || !connected || isTranscribing ? 0.5 : 1,
                }}
              >
                {/* Clean Mic SVG */}
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#24c1e0"
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
            ) : (
              <span style={{ fontSize: "0.75rem", color: "#9ba3b8", fontStyle: "italic" }}>
                Speak clearly into microphone…
              </span>
            )}
          </div>

          {/* Right: Hint + Send Button */}
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
            <span style={{ fontSize: "0.7rem", color: "#636c84" }}>Enter ↵</span>

            <button
              type="button"
              data-testid="send-prompt-button"
              onClick={handleSend}
              disabled={!hasText || disabled || !connected || isRecording || isTranscribing}
              title={t["send_prompt"] || "Send"}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: "34px",
                height: "34px",
                borderRadius: "50%",
                background: hasText && connected && !isRecording && !isTranscribing
                  ? "linear-gradient(135deg, #4285f4, #24c1e0)"
                  : "#191c26",
                color: hasText && connected && !isRecording && !isTranscribing ? "#ffffff" : "#636c84",
                border: "none",
                cursor: hasText && connected && !isRecording && !isTranscribing ? "pointer" : "default",
                boxShadow: hasText && connected && !isRecording && !isTranscribing
                  ? "0 2px 10px rgba(66, 133, 244, 0.4)"
                  : "none",
                transition: "background 0.2s, transform 0.1s, box-shadow 0.2s",
              }}
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

      <style>{`
        @keyframes pulse {
          0% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(1.15); }
          100% { opacity: 1; transform: scale(1); }
        }
        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
};
