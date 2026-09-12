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
  /** The persona name is engineering detail (Finding 8) — shown only when
   * the Developer toggle is on; otherwise every turn reads as "Agent". */
  developer?: boolean;
  t: Record<string, string>;
}

export const MemoryDrawer: React.FC<MemoryDrawerProps> = ({
  isOpen,
  onClose,
  turns,
  onClearMemory,
  developer = false,
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
    <>
      <div className="pt-scrim" />
      <div className="pt-sheet pt-sheet--drawer">
        <div className="pt-sheet-head">
          <div>
            <h2 className="pt-h2">{t["memory-ledger"] || "Memory Ledger"}</h2>
            <div className="pt-lbl">
              Hippocampus Episodic Storage (<span className="pt-mono">{turns.length}</span> turns)
            </div>
          </div>
          <button type="button" className="pt-btn pt-btn--icon" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="pt-section" style={{ display: "flex", gap: "var(--pt-s2)" }}>
          <input
            type="text"
            className="pt-input"
            placeholder="Filter conversation turns..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button type="button" className="pt-btn" onClick={handleExportJsonl} title="Export JSONL">
            Export
          </button>
        </div>

        <div className="pt-section pt-scroll-y">
          {filteredTurns.length === 0 ? (
            <p className="pt-note">No episodic memories stored yet. Speak with the agent to commit turns.</p>
          ) : (
            filteredTurns.map((turn, idx) => (
              <div
                key={`${turn.turn_id || "turn"}-${turn.timestamp || idx}-${idx}`}
                className="pt-section"
              >
                <div className="pt-lbl">
                  {developer ? turn.persona.toUpperCase() : "AGENT"}
                  {" · "}
                  <span className="pt-mono">
                    {turn.timestamp ? new Date(turn.timestamp * 1000).toLocaleTimeString() : ""}
                  </span>
                </div>
                <div>
                  <strong>User: </strong>
                  {turn.user}
                </div>
                <div>
                  <strong>Agent: </strong>
                  {turn.agent}
                </div>
              </div>
            ))
          )}
        </div>

        <div className="pt-sheet-head">
          <span className="pt-note">Durable across restarts</span>
          <button
            type="button"
            className="pt-btn pt-btn--danger"
            onClick={handleClear}
            disabled={clearing || turns.length === 0}
          >
            {clearing ? "Clearing..." : t["clear-memory"] || "Clear Memory"}
          </button>
        </div>
      </div>
    </>
  );
};
