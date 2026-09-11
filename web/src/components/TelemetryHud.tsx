import React from "react";
import { AgentState } from "../ws";

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

export const TelemetryHud: React.FC<TelemetryHudProps> = ({
  state,
  metrics,
  queueLength,
  connected,
}) => {
  const getStatusColor = () => {
    if (!connected) return "#ff3d00"; // crimson
    switch (state) {
      case "listening":
        return "#00c853"; // emerald
      case "thinking":
        return "#24c1e0"; // gemini cyan
      case "speaking":
        return "#ffb300"; // amber
      default:
        return "#636c84"; // slate
    }
  };

  return (
    <section
      aria-label="Telemetry Waterfall"
      style={{
        background: "rgba(18, 20, 28, 0.85)",
        backdropFilter: "blur(12px)",
        border: "1px solid #282c3f",
        borderRadius: "12px",
        padding: "0.875rem 1.25rem",
        margin: "1rem 0",
        fontFamily: "'Geist Mono', 'JetBrains Mono', monospace",
        fontSize: "0.75rem",
        color: "#9ba3b8",
        boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "0.75rem",
          paddingBottom: "0.5rem",
          borderBottom: "1px solid #282c3f",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              backgroundColor: getStatusColor(),
              boxShadow: `0 0 8px ${getStatusColor()}`,
              display: "inline-block",
            }}
          />
          <span style={{ fontWeight: 600, color: "#f1f3f9", textTransform: "uppercase" }}>
            Telemetry Waterfall
          </span>
          <span
            style={{
              padding: "0.15rem 0.4rem",
              borderRadius: 4,
              fontSize: "0.65rem",
              background: metrics.provenance === "flown" ? "rgba(0,200,83,0.15)" : "rgba(99,108,132,0.15)",
              color: metrics.provenance === "flown" ? "#00c853" : "#9ba3b8",
              border: `1px solid ${metrics.provenance === "flown" ? "rgba(0,200,83,0.3)" : "#282c3f"}`,
            }}
          >
            {metrics.provenance}
          </span>
        </div>
        <div style={{ color: "#636c84" }}>
          Queue Depth: <span style={{ color: queueLength > 0 ? "#ffb300" : "#f1f3f9" }}>{queueLength} sentences</span>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(85px, 1fr))",
          gap: "0.75rem",
          textAlign: "center",
        }}
      >
        <div style={{ padding: "0.4rem", background: "#191c26", borderRadius: 6 }}>
          <div style={{ color: "#636c84", marginBottom: "0.2rem" }}>VAD</div>
          <div style={{ color: "#f1f3f9", fontWeight: 600 }}>{metrics.vadMs}ms</div>
          <div style={{ fontSize: "0.65rem", color: "#00c853" }}>&le;30ms</div>
        </div>

        <div style={{ padding: "0.4rem", background: "#191c26", borderRadius: 6 }}>
          <div style={{ color: "#636c84", marginBottom: "0.2rem" }}>STT</div>
          <div style={{ color: "#f1f3f9", fontWeight: 600 }}>{metrics.sttMs}ms</div>
          <div style={{ fontSize: "0.65rem", color: "#00c853" }}>&le;150ms</div>
        </div>

        <div style={{ padding: "0.4rem", background: "#191c26", borderRadius: 6 }}>
          <div style={{ color: "#636c84", marginBottom: "0.2rem" }}>STALL TTFB</div>
          <div style={{ color: "#24c1e0", fontWeight: 600 }}>{metrics.stallMs}ms</div>
          <div style={{ fontSize: "0.65rem", color: "#00c853" }}>&le;400ms</div>
        </div>

        <div style={{ padding: "0.4rem", background: "#191c26", borderRadius: 6 }}>
          <div style={{ color: "#636c84", marginBottom: "0.2rem" }}>LLM TTFT</div>
          <div style={{ color: "#f1f3f9", fontWeight: 600 }}>{metrics.ttftMs}ms</div>
          <div style={{ fontSize: "0.65rem", color: "#00c853" }}>&le;400ms</div>
        </div>

        <div style={{ padding: "0.4rem", background: "#191c26", borderRadius: 6 }}>
          <div style={{ color: "#636c84", marginBottom: "0.2rem" }}>TTS CHUNK</div>
          <div style={{ color: "#f1f3f9", fontWeight: 600 }}>{metrics.ttsMs}ms</div>
          <div style={{ fontSize: "0.65rem", color: "#00c853" }}>&le;200ms</div>
        </div>

        <div
          style={{
            padding: "0.4rem",
            background: "#191c26",
            borderRadius: 6,
            border: "1px solid rgba(36, 193, 224, 0.3)",
          }}
        >
          <div style={{ color: "#24c1e0", marginBottom: "0.2rem" }}>E2E REFLEX</div>
          <div style={{ color: "#f1f3f9", fontWeight: 700 }}>{metrics.e2eMs}ms</div>
          <div style={{ fontSize: "0.65rem", color: "#00c853" }}>&le;800ms</div>
        </div>
      </div>
    </section>
  );
};
