import React, { useState } from "react";

export interface MemoryTurn {
  turn_id: string;
  timestamp: number;
  persona: string;
  user: string;
  agent: string;
}

interface MemoryDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  turns: MemoryTurn[];
  onClearMemory: () => Promise<void>;
  t: Record<string, string>;
}

export const MemoryDrawer: React.FC<MemoryDrawerProps> = ({
  isOpen,
  onClose,
  turns,
  onClearMemory,
  t,
}) => {
  const [query, setQuery] = useState("");
  const [clearing, setClearing] = useState(false);

  if (!isOpen) return null;

  const filteredTurns = turns.filter((item) => {
    if (!query) return true;
    const q = query.toLowerCase();
    return (
      item.user.toLowerCase().includes(q) ||
      item.agent.toLowerCase().includes(q) ||
      item.persona.toLowerCase().includes(q)
    );
  });

  const handleClear = async () => {
    if (!window.confirm("Are you sure you want to clear episodic memory ledger?")) return;
    setClearing(true);
    try {
      await onClearMemory();
    } finally {
      setClearing(false);
    }
  };

  const handleExportJsonl = () => {
    const blob = new Blob([turns.map((t) => JSON.stringify(t)).join("\n")], {
      type: "application/jsonl",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `hippocampus-memory-${Date.now()}.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        width: "min(420px, 90vw)",
        background: "#12141c",
        borderLeft: "1px solid #282c3f",
        boxShadow: "-8px 0 32px rgba(0,0,0,0.7)",
        zIndex: 100,
        display: "flex",
        flexDirection: "column",
        color: "#f1f3f9",
        fontFamily: "system-ui, -apple-system, sans-serif",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "1.25rem",
          borderBottom: "1px solid #282c3f",
        }}
      >
        <div>
          <h3 style={{ margin: 0, fontSize: "1.05rem", fontWeight: 600 }}>
            {t["memory-ledger"] || "Memory Ledger"}
          </h3>
          <span style={{ fontSize: "0.75rem", color: "#636c84" }}>
            Hippocampus Episodic Storage ({turns.length} turns)
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          style={{
            background: "none",
            border: "none",
            color: "#9ba3b8",
            fontSize: "1.2rem",
            cursor: "pointer",
            padding: "0.25rem",
          }}
        >
          ✕
        </button>
      </div>

      {/* Filter & Actions */}
      <div
        style={{
          padding: "0.875rem 1.25rem",
          borderBottom: "1px solid #282c3f",
          display: "flex",
          gap: "0.5rem",
        }}
      >
        <input
          type="text"
          placeholder="Filter conversation turns..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{
            flex: 1,
            padding: "0.45rem 0.75rem",
            borderRadius: "6px",
            border: "1px solid #282c3f",
            background: "#090a0f",
            color: "#f1f3f9",
            fontSize: "0.8rem",
          }}
        />
        <button
          type="button"
          onClick={handleExportJsonl}
          title="Export JSONL"
          style={{
            background: "#191c26",
            border: "1px solid #282c3f",
            color: "#24c1e0",
            borderRadius: "6px",
            padding: "0.45rem 0.65rem",
            fontSize: "0.75rem",
            cursor: "pointer",
          }}
        >
          Export
        </button>
      </div>

      {/* Turns List */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "1.25rem",
          display: "flex",
          flexDirection: "column",
          gap: "0.875rem",
        }}
      >
        {filteredTurns.length === 0 ? (
          <div style={{ color: "#636c84", fontSize: "0.85rem", textAlign: "center", marginTop: "2rem" }}>
            No episodic memories stored yet. Speak with the agent to commit turns.
          </div>
        ) : (
          filteredTurns.map((turn, idx) => (
            <div
              key={`${turn.turn_id || "turn"}-${turn.timestamp || idx}-${idx}`}
              style={{
                background: "#191c26",
                border: "1px solid #282c3f",
                borderRadius: "8px",
                padding: "0.75rem",
                fontSize: "0.8rem",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  color: "#636c84",
                  fontSize: "0.7rem",
                  marginBottom: "0.35rem",
                }}
              >
                <span style={{ color: "#24c1e0", fontWeight: 600 }}>{turn.persona.toUpperCase()}</span>
                <span>{turn.timestamp ? new Date(turn.timestamp * 1000).toLocaleTimeString() : ""}</span>
              </div>
              <div style={{ marginBottom: "0.4rem", color: "#f1f3f9" }}>
                <span style={{ color: "#00c853", fontWeight: 600 }}>User: </span>
                {turn.user}
              </div>
              <div style={{ color: "#9ba3b8" }}>
                <span style={{ color: "#ffb300", fontWeight: 600 }}>Agent: </span>
                {turn.agent}
              </div>
            </div>
          ))
        )}
      </div>

      {/* Footer */}
      <div
        style={{
          padding: "1rem 1.25rem",
          borderTop: "1px solid #282c3f",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span style={{ fontSize: "0.75rem", color: "#636c84" }}>Durable across restarts</span>
        <button
          type="button"
          onClick={handleClear}
          disabled={clearing || turns.length === 0}
          style={{
            background: "rgba(255, 61, 0, 0.15)",
            border: "1px solid rgba(255, 61, 0, 0.4)",
            color: "#ff3d00",
            borderRadius: "6px",
            padding: "0.4rem 0.75rem",
            fontSize: "0.75rem",
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          {clearing ? "Clearing..." : t["clear-memory"] || "Clear Memory"}
        </button>
      </div>
    </div>
  );
};
