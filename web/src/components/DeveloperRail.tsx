/**
 * DeveloperRail — everything the user surface refuses to show.
 *
 * Frame names, millisecond numbers, budgets, the persona actually loaded, the
 * raw JSON of any frame, and a receipt's full source. It renders only when the
 * Developer toggle is on (persisted in localStorage, see App.tsx), which is
 * what makes the user surface's silence checkable: web/src/devSurface.test.tsx
 * greps the user render for `agent.`, `ms`, `stall` and the persona name.
 */

import { useState } from "react";
import type { ReceiptFrame } from "./ReceiptChip";
import { STALL_BUDGET_MS, FIRST_AUDIO_BUDGET_MS, type TurnTiming } from "../latency";
import { segmentsFor } from "../latency";

export interface LoggedFrame {
  id: string;
  /** Wall-clock of arrival, for reading the log against a terminal. */
  clock: string;
  direction: "in" | "out";
  /** The frame's `type` — `agent.sentence`, `user.stop`, and so on. */
  name: string;
  payload: unknown;
}

function preview(payload: unknown): string {
  try {
    const s = JSON.stringify(payload);
    if (!s) return "";
    // Audio payloads are megabytes of base64; the log shows their size, not
    // their bytes.
    return s.length > 120 ? `${s.slice(0, 120)}…` : s;
  } catch {
    return "";
  }
}

function pretty(payload: unknown): string {
  try {
    return JSON.stringify(
      payload,
      (key, value) =>
        key === "audio_b64" && typeof value === "string"
          ? `<${value.length} b64 chars>`
          : value,
      2,
    );
  } catch {
    return String(payload);
  }
}

export function DeveloperRail({
  frames,
  timing,
  personaName,
  providers,
  queueLength,
  bufferedCount,
  lastReceipt,
}: {
  frames: LoggedFrame[];
  timing: TurnTiming;
  personaName: string;
  providers: { stt: string; llm: string; tts: string };
  queueLength: number;
  bufferedCount: number;
  lastReceipt: ReceiptFrame | null;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const chosen = frames.find((f) => f.id === selected) ?? frames[frames.length - 1];
  const segments = segmentsFor(timing);

  return (
    <aside className="pt-rail pt-rail--right" data-testid="developer-rail">
      <section className="pt-section">
        <p className="pt-lbl">budgets this turn</p>
        <div className="pt-kv">
          {segments.length === 0 ? (
            <>
              <span className="pt-k">stall</span>
              <span className="pt-v">not measured</span>
              <span className="pt-k">first audio</span>
              <span className="pt-v">not measured</span>
            </>
          ) : (
            segments.map((seg) => (
              <div key={seg.key} style={{ display: "contents" }}>
                <span className="pt-k">{seg.label}</span>
                <span className={seg.breach ? "pt-v pt-num is-breach" : "pt-v pt-num"}>
                  {`${Math.round(seg.ms)} / ${seg.budgetMs} ms`}
                </span>
              </div>
            ))
          )}
          <span className="pt-k">written budgets</span>
          <span className="pt-v">{`${STALL_BUDGET_MS} / ${FIRST_AUDIO_BUDGET_MS} ms`}</span>
        </div>
        <p className="pt-note">qa/budgets.json is the only source for these.</p>
      </section>

      <section className="pt-section">
        <p className="pt-lbl">runtime</p>
        <div className="pt-kv">
          <span className="pt-k">persona</span>
          <span className="pt-v">{personaName}</span>
          <span className="pt-k">stt</span>
          <span className="pt-v">{providers.stt}</span>
          <span className="pt-k">llm</span>
          <span className="pt-v">{providers.llm}</span>
          <span className="pt-k">tts</span>
          <span className="pt-v">{providers.tts}</span>
          <span className="pt-k">play queue</span>
          <span className="pt-v">{queueLength}</span>
          <span className="pt-k">buffered ahead</span>
          <span className="pt-v">{bufferedCount}</span>
        </div>
      </section>

      <section className="pt-section">
        <p className="pt-lbl">raw frames</p>
        <div className="pt-log pt-scroll-y" style={{ maxHeight: "220px" }}>
          {frames.length === 0 ? <p className="pt-note">no frames yet.</p> : null}
          {frames
            .slice()
            .reverse()
            .map((f) => (
              <button
                type="button"
                key={f.id}
                className="pt-log-row"
                aria-pressed={chosen?.id === f.id}
                onClick={() => setSelected(f.id)}
              >
                <span className="pt-log-bar" />
                <span>{f.clock}</span>
                <span>{f.direction === "in" ? "←" : "→"}</span>
                <span className="pt-log-name">{f.name}</span>
                <span className="pt-log-payload">{preview(f.payload)}</span>
              </button>
            ))}
        </div>
      </section>

      {chosen ? (
        <section className="pt-section">
          <p className="pt-lbl">{chosen.name}</p>
          <pre className="pt-pre pt-scroll-x">{pretty(chosen.payload)}</pre>
        </section>
      ) : null}

      <section className="pt-section">
        <p className="pt-lbl">receipt source</p>
        {lastReceipt ? (
          <pre className="pt-pre pt-scroll-x">{pretty(lastReceipt)}</pre>
        ) : (
          <p className="pt-note">no receipt on this turn.</p>
        )}
      </section>
    </aside>
  );
}
