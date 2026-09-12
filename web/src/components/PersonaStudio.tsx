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
  /** The sheet needs a way out: the scrim and Escape both call this. */
  onClose?: () => void;
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
  onClose,
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
    <>
      <div className="pt-scrim" onClick={onClose} />
      <div className="pt-sheet">
        <div className="pt-sheet-head">
          <h2 className="pt-h2">{t["custom-persona"] || "Persona Studio"}</h2>
          {onClose ? (
            <button type="button" className="pt-btn pt-btn--icon" onClick={onClose} aria-label="Close">
              ×
            </button>
          ) : null}
          <button
            type="button"
            className="pt-btn"
            aria-pressed={isCreatingNew}
            onClick={() => setIsCreatingNew(!isCreatingNew)}
          >
            {isCreatingNew ? "Cancel" : `+ ${t["new-persona"] || "New Persona"}`}
          </button>
        </div>

        {isCreatingNew ? (
          <form onSubmit={handleCreateSubmit} className="pt-section">
            <div className="pt-field">
              <label>{t["persona"] || "Persona"} identifier</label>
              <div style={{ display: "flex", gap: "var(--pt-s2)" }}>
                <input
                  type="text"
                  className="pt-input"
                  placeholder="Persona identifier (e.g. athena)"
                  value={newPersonaName}
                  onChange={(e) => setNewPersonaName(e.target.value)}
                  required
                />
                <button type="submit" className="pt-btn pt-btn--primary" disabled={saving}>
                  {saving ? "Saving..." : t["save"] || "Save"}
                </button>
              </div>
            </div>
          </form>
        ) : null}

        <div className="pt-section">
          <div className="pt-lbl">{t["persona"] || "Persona"}</div>
          <div className="pt-rail-group pt-scroll-y">
            {personas.map((p) => (
              <button
                key={p.name}
                type="button"
                className="pt-rail-row"
                aria-current={p.name === currentPersona}
                onClick={() => onSelectPersona(p.name)}
              >
                {p.name.charAt(0).toUpperCase() + p.name.slice(1)}
              </button>
            ))}
          </div>
          {!isBuiltin && (
            <button
              type="button"
              className="pt-btn pt-btn--danger"
              onClick={() => onDeletePersona(currentPersona)}
              title="Delete custom persona"
            >
              {t["delete"] || "Delete"}
            </button>
          )}
        </div>

        <div className="pt-section">
          <div className="pt-field">
            <label>{t["voice"] || "Voice"}</label>
            <div style={{ display: "flex", gap: "var(--pt-s2)" }}>
              <select
                className="pt-input"
                value={currentVoice}
                onChange={(e) => onSelectVoice(e.target.value)}
              >
                {voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.display_name} ({v.lang})
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="pt-btn pt-btn--icon"
                onClick={() => onPreviewVoice(currentVoice)}
                title="Preview Voice Sample"
              >
                ▶
              </button>
            </div>
          </div>
        </div>

        <div className="pt-section">
          <div className="pt-field">
            <label>
              {t["speed"] || "Speed"} <span className="pt-num">{speed.toFixed(2)}x</span>
            </label>
            <input
              type="range"
              min="0.5"
              max="2.0"
              step="0.05"
              value={speed}
              onChange={(e) => onSpeedChange(parseFloat(e.target.value))}
            />
          </div>
        </div>

        <div className="pt-section">
          <div className="pt-field">
            <label>{t["system-prompt"] || "System Prompt & Tone"}</label>
            <textarea
              className="pt-input"
              rows={4}
              value={systemPrompt}
              onChange={(e) => onSystemPromptChange(e.target.value)}
              placeholder="Define voice personality, cognitive constraints, and reflex instructions..."
            />
          </div>
        </div>

        <div className="pt-section">
          <button
            type="button"
            className="pt-btn pt-btn--primary"
            onClick={handleUpdateCurrent}
            disabled={saving}
          >
            {saving ? "Saving..." : t["save"] || "Save Persona"}
          </button>
        </div>
      </div>
    </>
  );
};
