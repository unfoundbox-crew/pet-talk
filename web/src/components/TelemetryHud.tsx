import React from "react";
import { AgentState } from "../ws";
import { STALL_BUDGET_MS, FIRST_AUDIO_BUDGET_MS } from "../latency";

export interface LatencyMetrics {
  vadMs: number;
  sttMs: number;
  stallMs: number;
  ttftMs: number;
  ttsMs: number;
  e2eMs: number;
  provenance: "flown" | "target" | "idle";
}

interface TelemetryHudProps {
  state: AgentState;
  metrics: LatencyMetrics;
  queueLength: number;
  connected: boolean;
}

// Developer-only readout — every number the waterfall showed stays, just
// expressed in the cockpit's own vocabulary (.pt-kv / .pt-num / .pt-lbl)
// instead of hand-rolled hex. The status dot now reads connection, not
// agent state, per the pt-dot--live/--down vocabulary (only two states).
export const TelemetryHud: React.FC<TelemetryHudProps> = (props) => {
  const { metrics, queueLength, connected } = props;

  return (
    <section aria-label="Telemetry Waterfall" className="pt-panel">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--pt-s3)",
          marginBottom: "var(--pt-s3)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "var(--pt-s2)" }}>
          <span aria-hidden className={connected ? "pt-dot pt-dot--live" : "pt-dot pt-dot--down"} />
          <span className="pt-lbl" style={{ margin: 0 }}>
            Telemetry waterfall
          </span>
          <span className={`pt-chip${metrics.provenance === "flown" ? " pt-chip--signal" : ""}`}>
            {metrics.provenance}
          </span>
        </div>
        <span className="pt-mono">
          queue depth <span className="pt-num">{queueLength}</span>
        </span>
      </div>

      <dl className="pt-kv">
        <dt className="pt-k">VAD</dt>
        <dd className="pt-v pt-num">
          {metrics.vadMs}ms <span className="pt-note">≤30ms</span>
        </dd>

        <dt className="pt-k">STT</dt>
        <dd className="pt-v pt-num">
          {metrics.sttMs}ms <span className="pt-note">≤150ms</span>
        </dd>

        <dt className="pt-k">Stall TTFB</dt>
        <dd className={`pt-v pt-num${metrics.stallMs > STALL_BUDGET_MS ? " is-breach" : ""}`}>
          {metrics.stallMs}ms <span className="pt-note">≤{STALL_BUDGET_MS}ms</span>
        </dd>

        <dt className="pt-k">LLM TTFT</dt>
        <dd className="pt-v pt-num">
          {metrics.ttftMs}ms <span className="pt-note">≤400ms</span>
        </dd>

        <dt className="pt-k">TTS chunk</dt>
        <dd className="pt-v pt-num">
          {metrics.ttsMs}ms <span className="pt-note">≤200ms</span>
        </dd>

        <dt className="pt-k">E2E reflex</dt>
        <dd className={`pt-v pt-num${metrics.e2eMs > FIRST_AUDIO_BUDGET_MS ? " is-breach" : ""}`}>
          {metrics.e2eMs}ms <span className="pt-note">≤{FIRST_AUDIO_BUDGET_MS}ms</span>
        </dd>
      </dl>
    </section>
  );
};
