// EyesAttach — the cockpit's Eyes affordance (WAVE3 §1, TECH-DESIGN Phase 4).
//
// Three ways in, one frame out (`user.attach`): Cmd+V of an image, drag-drop
// anywhere on the window, and an explicit file picker. Nothing here talks to
// the socket; App.tsx owns the turn_id and the send.
//
// Visual language is DESIGN.md's Gemini-Live token set (surface #191c26,
// border #282c3f, cyan #24c1e0 for perception, amber for a warning, crimson
// for a fail). No new palette, no new font.
import { useCallback, useEffect, useRef, useState } from "react";
import { EyesKind, EyesTask, ScreenGrounding } from "../ws";

const C = {
  card: "#191c26",
  elevated: "#12141c",
  border: "#282c3f",
  borderFocus: "#4f587d",
  text: "#f1f3f9",
  secondary: "#9ba3b8",
  muted: "#636c84",
  cyan: "#24c1e0",
  blue: "#4285f4",
  amber: "#ffb300",
  crimson: "#ff3d00",
  mono: "'Geist Mono', 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace",
};

/** One attachment's lifecycle, as the transcript sees it. */
export interface EyesEntry {
  ref: string;
  kind: EyesKind;
  filename: string;
  task?: EyesTask;
  bytes?: number;
  status: "sent" | "received" | "text" | "error";
  text?: string;
  truncated?: boolean;
  engine?: string;
  source?: string;
  reason?: string;
  detail?: string;
  time: string;
}

const KIND_GLYPH: Record<EyesKind, string> = {
  screenshot: "▣",
  image: "◨",
  pdf: "▤",
};

/** Keyframes cannot live in an inline style object; one tiny sheet instead. */
function EyesStyles() {
  return (
    <style>{`
@keyframes eyes-spin { to { transform: rotate(360deg); } }
@keyframes eyes-rise { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.eyes-spinner { animation: eyes-spin 900ms linear infinite; }
.eyes-rise { animation: eyes-rise 250ms cubic-bezier(0,0,0.2,1) both; }
@media (prefers-reduced-motion: reduce) {
  .eyes-spinner { animation-duration: 2400ms; }
  .eyes-rise { animation: none; }
}
`}</style>
  );
}

function humanBytes(n?: number): string {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// --------------------------------------------------------------- the dock ---

export interface EyesAttachDockProps {
  /** Called with the picked/dropped/pasted file. `hint` marks a clipboard
   *  bitmap, which the server treats as kind `screenshot`. */
  onAttach: (file: File | Blob, hint?: "screenshot") => void;
  disabled?: boolean;
  /** Client-side rejection text, e.g. `eyes_too_large`. */
  notice?: string;
}

export function EyesAttachDock({ onAttach, disabled, notice }: EyesAttachDockProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);

  // Cmd+V of an image anywhere on the page.
  useEffect(() => {
    if (disabled) return;
    const onPaste = (ev: ClipboardEvent) => {
      const items = ev.clipboardData?.items;
      if (!items) return;
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        if (it.kind !== "file") continue;
        const file = it.getAsFile();
        if (!file) continue;
        if (file.type.startsWith("image/") || file.type === "application/pdf") {
          ev.preventDefault();
          onAttach(file, file.type.startsWith("image/") ? "screenshot" : undefined);
          return;
        }
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [onAttach, disabled]);

  // Drag-drop anywhere on the window.
  useEffect(() => {
    if (disabled) return;
    const over = (ev: DragEvent) => ev.preventDefault();
    const enter = (ev: DragEvent) => {
      ev.preventDefault();
      depth.current += 1;
      setDragging(true);
    };
    const leave = (ev: DragEvent) => {
      ev.preventDefault();
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    };
    const drop = (ev: DragEvent) => {
      ev.preventDefault();
      depth.current = 0;
      setDragging(false);
      const files = ev.dataTransfer?.files;
      if (files && files.length > 0) onAttach(files[0]);
    };
    window.addEventListener("dragover", over);
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [onAttach, disabled]);

  const pick = useCallback(() => inputRef.current?.click(), []);

  return (
    <>
      <EyesStyles />
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <button
          type="button"
          onClick={pick}
          disabled={disabled}
          title="Attach a screenshot, image, or PDF — or just paste one (Cmd+V)"
          aria-label="Attach an image or PDF for the agent to read"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.4rem",
            background: C.card,
            border: `1px solid ${dragging ? C.cyan : C.border}`,
            color: disabled ? C.muted : C.text,
            borderRadius: "999px",
            padding: "0.3rem 0.75rem",
            fontSize: "0.75rem",
            fontWeight: 600,
            cursor: disabled ? "not-allowed" : "pointer",
            transition: "border-color 150ms cubic-bezier(0.2,0,0,1)",
          }}
        >
          <span aria-hidden style={{ color: C.cyan, fontSize: "0.9rem", lineHeight: 1 }}>
            ◉
          </span>
          Attach
        </button>
        <span style={{ fontSize: "0.68rem", color: C.muted }}>
          paste · drop · pick — OCR stays on this machine
        </span>
        {notice && (
          <span
            className="eyes-rise"
            role="alert"
            style={{
              fontSize: "0.68rem",
              fontFamily: C.mono,
              color: C.crimson,
              border: `1px solid rgba(255,61,0,0.35)`,
              background: "rgba(255,61,0,0.1)",
              borderRadius: "6px",
              padding: "0.1rem 0.4rem",
            }}
          >
            {notice}
          </span>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept="image/*,application/pdf"
        style={{ display: "none" }}
        onChange={(ev) => {
          const f = ev.target.files?.[0];
          if (f) onAttach(f);
          ev.target.value = "";
        }}
      />

      {dragging && (
        <div
          aria-hidden
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            background: "rgba(9,10,15,0.72)",
            border: `2px dashed ${C.cyan}`,
            borderRadius: "16px",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            pointerEvents: "none",
          }}
        >
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: "2rem", color: C.cyan }}>◉</div>
            <div style={{ fontSize: "0.9rem", fontWeight: 600, color: C.text, marginTop: "0.5rem" }}>
              Drop to let the agent read it
            </div>
            <div style={{ fontSize: "0.72rem", color: C.secondary, marginTop: "0.25rem" }}>
              PNG · JPEG · PDF — up to 8 MB
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------- transcript blocks ---

/** Spinner chip for `eyes.received`; collapsible transcript for `eyes.text`. */
export function EyesBlock({ entry }: { entry: EyesEntry }) {
  const [open, setOpen] = useState(false);
  const pending = entry.status === "sent" || entry.status === "received";
  const failed = entry.status === "error";
  const accent = failed ? C.crimson : pending ? C.cyan : C.blue;

  return (
    <div
      className="eyes-rise"
      data-testid={`eyes-${entry.ref}`}
      style={{
        alignSelf: "flex-start",
        maxWidth: "88%",
        background: C.elevated,
        border: `1px solid ${failed ? "rgba(255,61,0,0.35)" : C.border}`,
        borderLeft: `2px solid ${accent}`,
        borderRadius: "12px",
        padding: "0.55rem 0.8rem",
        fontSize: "0.82rem",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
        <span aria-hidden style={{ color: accent }}>
          {KIND_GLYPH[entry.kind] ?? "◨"}
        </span>
        <span style={{ fontWeight: 600, fontSize: "0.7rem", color: accent, letterSpacing: "0.02em" }}>
          EYES
        </span>
        <span
          style={{
            fontFamily: C.mono,
            fontSize: "0.7rem",
            color: C.secondary,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            maxWidth: 220,
          }}
        >
          {entry.filename || entry.kind}
        </span>
        {entry.bytes ? (
          <span style={{ fontFamily: C.mono, fontSize: "0.66rem", color: C.muted }}>
            {humanBytes(entry.bytes)}
          </span>
        ) : null}
        {entry.task && (
          <span
            style={{
              fontFamily: C.mono,
              fontSize: "0.64rem",
              color: C.muted,
              border: `1px solid ${C.border}`,
              borderRadius: "999px",
              padding: "0 0.35rem",
            }}
          >
            {entry.task}
          </span>
        )}
        {pending && (
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
            <span
              className="eyes-spinner"
              aria-hidden
              style={{
                width: 10,
                height: 10,
                borderRadius: "50%",
                border: `1.5px solid rgba(36,193,224,0.25)`,
                borderTopColor: C.cyan,
                display: "inline-block",
              }}
            />
            <span style={{ fontSize: "0.68rem", color: C.cyan }}>
              {entry.status === "sent" ? "sending…" : "reading…"}
            </span>
          </span>
        )}
        {entry.truncated && (
          <span
            title="Text was cut at the server's character cap"
            style={{
              fontSize: "0.64rem",
              fontWeight: 700,
              fontFamily: C.mono,
              color: C.amber,
              background: "rgba(255,179,0,0.12)",
              border: "1px solid rgba(255,179,0,0.35)",
              borderRadius: "999px",
              padding: "0 0.4rem",
            }}
          >
            truncated
          </span>
        )}
        {entry.engine && (
          <span style={{ fontFamily: C.mono, fontSize: "0.64rem", color: C.muted }}>{entry.engine}</span>
        )}
        <span style={{ marginLeft: "auto", fontSize: "0.66rem", color: C.muted }}>{entry.time}</span>
      </div>

      {failed && (
        <div style={{ marginTop: "0.35rem", fontFamily: C.mono, fontSize: "0.72rem", color: C.crimson }}>
          {entry.reason}
          {entry.detail ? ` — ${entry.detail}` : ""}
        </div>
      )}

      {entry.status === "text" && entry.text && (
        <div style={{ marginTop: "0.4rem" }}>
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            style={{
              background: "none",
              border: "none",
              color: C.cyan,
              fontSize: "0.7rem",
              cursor: "pointer",
              padding: 0,
              fontWeight: 600,
            }}
          >
            {open ? "▾" : "▸"} {open ? "Hide" : "Show"} extracted text (
            {entry.text.length.toLocaleString()} chars)
          </button>
          {!open && (
            <div
              style={{
                marginTop: "0.25rem",
                color: C.secondary,
                fontSize: "0.76rem",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {entry.text.split("\n")[0]}
            </div>
          )}
          {open && (
            <pre
              style={{
                marginTop: "0.4rem",
                marginBottom: 0,
                maxHeight: 220,
                overflow: "auto",
                background: "#0d0f16",
                border: `1px solid ${C.border}`,
                borderRadius: "8px",
                padding: "0.6rem",
                fontFamily: C.mono,
                fontSize: "0.72rem",
                lineHeight: 1.5,
                color: C.text,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {entry.text}
            </pre>
          )}
          {entry.source && (
            <div style={{ marginTop: "0.3rem", fontFamily: C.mono, fontSize: "0.64rem", color: C.muted }}>
              [{entry.source}|{entry.task}] injected into turn context
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** AX grounding line — only drawn when the server sends `state.thinking.screen`. */
export function ScreenGroundingLine({ screen }: { screen?: ScreenGrounding | null }) {
  if (!screen || (!screen.app && !screen.window && !screen.selection && !screen.path)) return null;
  const bits: string[] = [];
  if (screen.app) bits.push(screen.app);
  if (screen.window) bits.push(screen.window);
  if (screen.path) bits.push(screen.path);
  return (
    <div
      data-testid="screen-grounding"
      className="eyes-rise"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.45rem",
        width: "100%",
        fontFamily: C.mono,
        fontSize: "0.68rem",
        color: C.secondary,
        background: C.elevated,
        border: `1px solid ${C.border}`,
        borderLeft: `2px solid ${C.borderFocus}`,
        borderRadius: "8px",
        padding: "0.3rem 0.6rem",
      }}
    >
      <span aria-hidden style={{ color: C.borderFocus }}>
        ⌗
      </span>
      <span style={{ color: C.muted }}>looking at</span>
      <span style={{ color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {bits.join(" · ")}
      </span>
      {screen.selection && (
        <span style={{ color: C.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          “{screen.selection.slice(0, 80)}”
        </span>
      )}
    </div>
  );
}
