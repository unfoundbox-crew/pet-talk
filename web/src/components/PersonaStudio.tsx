import React, { useState } from "react";

export interface PersonaData {
  name: string;
  voice: string;
  speed: number;
  stalls: string[];
  tone: string;
}

interface VoiceOption {
  id: string;
  display_name: string;
  lang: string;
}

interface PersonaStudioProps {
  personas: PersonaData[];
  currentPersona: string;
  onSelectPersona: (name: string) => void;
  voices: VoiceOption[];
  currentVoice: string;
  onSelectVoice: (voiceId: string) => void;
  speed: number;
  onSpeedChange: (speed: number) => void;
  systemPrompt: string;
  onSystemPromptChange: (prompt: string) => void;
  onSavePersona: (persona: PersonaData) => Promise<void>;
  onDeletePersona: (name: string) => Promise<void>;
  onPreviewVoice: (voiceId: string) => void;
  t: Record<string, string>;
}

export const PersonaStudio: React.FC<PersonaStudioProps> = ({
  personas,
  currentPersona,
  onSelectPersona,
  voices,
  currentVoice,
  onSelectVoice,
  speed,
  onSpeedChange,
  systemPrompt,
  onSystemPromptChange,
  onSavePersona,
  onDeletePersona,
  onPreviewVoice,
  t,
}) => {
  const [isCreatingNew, setIsCreatingNew] = useState(false);
  const [newPersonaName, setNewPersonaName] = useState("");
  const [saving, setSaving] = useState(false);

  const activePersonaObj = personas.find((p) => p.name === currentPersona);
  const isBuiltin = ["donna", "jarvis", "zuck", "default"].includes(currentPersona.toLowerCase());

  const handleCreateSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newPersonaName.trim()) return;
    setSaving(true);
    try {
      await onSavePersona({
        name: newPersonaName.trim().toLowerCase(),
        voice: currentVoice,
        speed: speed,
        stalls: ["On it — one sec.", "Checking that right now."],
        tone: systemPrompt || "Direct, concise, and razor-competent.",
      });
      setIsCreatingNew(false);
      setNewPersonaName("");
    } finally {
      setSaving(false);
    }
  };

  const handleUpdateCurrent = async () => {
    setSaving(true);
    try {
      await onSavePersona({
        name: currentPersona,
        voice: currentVoice,
        speed: speed,
        stalls: activePersonaObj?.stalls || ["One moment."],
        tone: systemPrompt,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      style={{
        background: "#12141c",
        border: "1px solid #282c3f",
        borderRadius: "16px",
        padding: "1.25rem",
        margin: "1rem 0",
        color: "#f1f3f9",
        boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "1rem",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1.1rem", fontWeight: 600 }}>
          {t["custom-persona"] || "Persona Studio"}
        </h3>
        <button
          type="button"
          onClick={() => setIsCreatingNew(!isCreatingNew)}
          style={{
            background: isCreatingNew ? "#282c3f" : "#24c1e0",
            color: isCreatingNew ? "#f1f3f9" : "#090a0f",
            border: "none",
            borderRadius: "6px",
            padding: "0.35rem 0.75rem",
            fontSize: "0.8rem",
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          {isCreatingNew ? "Cancel" : `+ ${t["new-persona"] || "New Persona"}`}
        </button>
      </div>

      {isCreatingNew ? (
        <form onSubmit={handleCreateSubmit} style={{ marginBottom: "1rem" }}>
          <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.75rem" }}>
            <input
              type="text"
              placeholder="Persona identifier (e.g. athena)"
              value={newPersonaName}
              onChange={(e) => setNewPersonaName(e.target.value)}
              style={{
                flex: 1,
                padding: "0.5rem 0.75rem",
                borderRadius: "6px",
                border: "1px solid #282c3f",
                background: "#090a0f",
                color: "#f1f3f9",
                fontSize: "0.85rem",
              }}
              required
            />
            <button
              type="submit"
              disabled={saving}
              style={{
                background: "#00c853",
                color: "#090a0f",
                border: "none",
                borderRadius: "6px",
                padding: "0.5rem 1rem",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              {saving ? "Saving..." : t["save"] || "Save"}
            </button>
          </div>
        </form>
      ) : null}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem", marginBottom: "1rem" }}>
        {/* Persona Select */}
        <div>
          <label style={{ display: "block", fontSize: "0.75rem", color: "#9ba3b8", marginBottom: "0.35rem" }}>
            {t["persona"] || "Persona"}
          </label>
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <select
              value={currentPersona}
              onChange={(e) => onSelectPersona(e.target.value)}
              style={{
                flex: 1,
                padding: "0.5rem 0.75rem",
                borderRadius: "8px",
                border: "1px solid #282c3f",
                background: "#191c26",
                color: "#f1f3f9",
                fontSize: "0.85rem",
              }}
            >
              {personas.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name.charAt(0).toUpperCase() + p.name.slice(1)}
                </option>
              ))}
            </select>
            {!isBuiltin && (
              <button
                type="button"
                onClick={() => onDeletePersona(currentPersona)}
                title="Delete custom persona"
                style={{
                  background: "rgba(255, 61, 0, 0.2)",
                  color: "#ff3d00",
                  border: "1px solid #ff3d00",
                  borderRadius: "8px",
                  padding: "0.35rem 0.65rem",
                  fontSize: "0.75rem",
                  cursor: "pointer",
                }}
              >
                {t["delete"] || "Del"}
              </button>
            )}
          </div>
        </div>

        {/* Voice Select + Preview */}
        <div>
          <label style={{ display: "block", fontSize: "0.75rem", color: "#9ba3b8", marginBottom: "0.35rem" }}>
            {t["voice"] || "Voice"}
          </label>
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <select
              value={currentVoice}
              onChange={(e) => onSelectVoice(e.target.value)}
              style={{
                flex: 1,
                padding: "0.5rem 0.75rem",
                borderRadius: "8px",
                border: "1px solid #282c3f",
                background: "#191c26",
                color: "#f1f3f9",
                fontSize: "0.85rem",
              }}
            >
              {voices.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.display_name} ({v.lang})
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => onPreviewVoice(currentVoice)}
              title="Preview Voice Sample"
              style={{
                background: "#282c3f",
                color: "#24c1e0",
                border: "none",
                borderRadius: "8px",
                padding: "0.35rem 0.65rem",
                fontSize: "0.75rem",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              ▶
            </button>
          </div>
        </div>
      </div>

      {/* Speed Slider */}
      <div style={{ marginBottom: "1rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.75rem", color: "#9ba3b8", marginBottom: "0.35rem" }}>
          <span>{t["speed"] || "Speed"}</span>
          <span style={{ color: "#24c1e0", fontFamily: "monospace", fontWeight: 600 }}>{speed.toFixed(2)}x</span>
        </div>
        <input
          type="range"
          min="0.5"
          max="2.0"
          step="0.05"
          value={speed}
          onChange={(e) => onSpeedChange(parseFloat(e.target.value))}
          style={{ width: "100%", accentColor: "#24c1e0", cursor: "pointer" }}
        />
      </div>

      {/* System Prompt & Tone */}
      <div style={{ marginBottom: "0.75rem" }}>
        <label style={{ display: "block", fontSize: "0.75rem", color: "#9ba3b8", marginBottom: "0.35rem" }}>
          {t["system-prompt"] || "System Prompt & Tone"}
        </label>
        <textarea
          rows={4}
          value={systemPrompt}
          onChange={(e) => onSystemPromptChange(e.target.value)}
          placeholder="Define voice personality, cognitive constraints, and reflex instructions..."
          style={{
            width: "100%",
            boxSizing: "border-box",
            padding: "0.6rem 0.75rem",
            borderRadius: "8px",
            border: "1px solid #282c3f",
            background: "#090a0f",
            color: "#f1f3f9",
            fontSize: "0.8rem",
            fontFamily: "inherit",
            resize: "vertical",
          }}
        />
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="button"
          onClick={handleUpdateCurrent}
          disabled={saving}
          style={{
            background: "#4285f4",
            color: "#ffffff",
            border: "none",
            borderRadius: "8px",
            padding: "0.45rem 1rem",
            fontSize: "0.8rem",
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          {saving ? "Saving..." : t["save"] || "Save Persona"}
        </button>
      </div>
    </div>
  );
};
