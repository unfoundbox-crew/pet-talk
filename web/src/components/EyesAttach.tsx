// EyesAttach — the cockpit's Eyes affordance (WAVE3 §1, TECH-DESIGN Phase 4).
//
// Three ways in, one frame out (`user.attach`): Cmd+V of an image, drag-drop
// anywhere on the window, and an explicit file picker. Nothing here talks to
// the socket; App.tsx owns the turn_id and the send.
//
// Visual language is the cockpit's own class vocabulary (web/src/styles/app.css)
// — AgentWorth's palette plus pet-talk's own layer. No hand-rolled colour.
import { useCallback, useEffect, useRef, useState } from "react";
import { EyesKind, EyesTask, ScreenGrounding } from "../ws";

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

/** Keyframes cannot live in an inline style object; one tiny sheet for the
 *  spinner (app.css's `.pt-arrive` covers every entrance already). */
function EyesStyles() {
  return (
    <style>{`
@keyframes eyes-spin { to { transform: rotate(360deg); } }
.eyes-spinner { animation: eyes-spin 900ms linear infinite; }
@media (prefers-reduced-motion: reduce) {
  .eyes-spinner { animation-duration: 2400ms; }
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
      <div className="pt-chipline">
        <button
          type="button"
          onClick={pick}
          disabled={disabled}
          title="Attach a screenshot, image, or PDF — or just paste one (Cmd+V)"
          aria-label="Attach an image or PDF for the agent to read"
          className="pt-btn"
        >
          <span aria-hidden>◉</span> Attach
        </button>
        <span className="pt-note">paste · drop · pick — OCR stays on this machine</span>
        {notice && (
          <span className="pt-banner pt-arrive" role="alert">
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
        <>
          <div aria-hidden className="pt-scrim" />
          <div aria-hidden className="pt-sheet" style={{ border: "2px dashed var(--mv-accent)" }}>
            <p className="pt-h3" style={{ margin: 0 }}>
              <span aria-hidden>◉</span> Drop to let the agent read it
            </p>
            <p className="pt-note" style={{ marginTop: "var(--pt-s2)" }}>
              PNG · JPEG · PDF — up to 8 MB
            </p>
          </div>
        </>
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

  return (
    <div className="pt-section pt-arrive" data-testid={`eyes-${entry.ref}`}>
      <div className="pt-chipline">
        <span className="pt-lbl" style={{ margin: 0 }}>
          {KIND_GLYPH[entry.kind] ?? "◨"} {entry.kind}
        </span>
        <span className="pt-mono pt-scroll-x" style={{ whiteSpace: "nowrap", maxWidth: 220 }}>
          {entry.filename || entry.kind}
        </span>
        {entry.bytes ? <span className="pt-mono">{humanBytes(entry.bytes)}</span> : null}
        {entry.task && <span className="pt-chip">{entry.task}</span>}
        {pending && (
          <span className="pt-chip pt-chip--signal">
            <span className="eyes-spinner" aria-hidden />
            {entry.status === "sent" ? "sending…" : "reading…"}
          </span>
        )}
        {entry.truncated && (
          <span className="pt-chip" title="Text was cut at the server's character cap">
            truncated
          </span>
        )}
        {entry.engine && <span className="pt-mono">{entry.engine}</span>}
        <span className="pt-mono" style={{ marginLeft: "auto" }}>
          {entry.time}
        </span>
      </div>

      {failed && (
        <div className="pt-banner" style={{ marginTop: "var(--pt-s2)" }}>
          <span>
            {entry.reason}
            {entry.detail ? ` — ${entry.detail}` : ""}
          </span>
        </div>
      )}

      {entry.status === "text" && entry.text && (
        <div style={{ marginTop: "var(--pt-s2)" }}>
          <button
            type="button"
            className="pt-btn pt-btn--ghost"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
          >
            {open ? "▾" : "▸"} {open ? "Hide" : "Show"} extracted text (
            {entry.text.length.toLocaleString()} chars)
          </button>
          {!open && (
            <p className="pt-note pt-scroll-x" style={{ whiteSpace: "nowrap", marginTop: "var(--pt-s1)" }}>
              {entry.text.split("\n")[0]}
            </p>
          )}
          {open && <pre className="pt-pre pt-scroll-x" style={{ marginTop: "var(--pt-s2)" }}>{entry.text}</pre>}
          {entry.source && (
            <p className="pt-mono" style={{ marginTop: "var(--pt-s1)" }}>
              [{entry.source}|{entry.task}] injected into turn context
            </p>
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
    <div data-testid="screen-grounding" className="pt-chipline pt-arrive">
      <span className="pt-note">looking at</span>
      <span className="pt-mono pt-scroll-x" style={{ whiteSpace: "nowrap" }}>
        {bits.join(" · ")}
      </span>
      {screen.selection && (
        <span className="pt-note pt-scroll-x" style={{ whiteSpace: "nowrap" }}>
          “{screen.selection.slice(0, 80)}”
        </span>
      )}
    </div>
  );
}
