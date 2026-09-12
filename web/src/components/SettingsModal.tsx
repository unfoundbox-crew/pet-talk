import React, { useState } from "react";

// Field names the server redacts (matches server.settings.SECRET_HINTS:
// any field whose name contains "key" or "token" — plus "secret"/"password").
// ``GET /settings`` sends "***" for these when set, never the real value.
const SECRET_FIELDS = [
  "deepgram_api_key",
  "groq_api_key",
  "openai_api_key",
  "smallest_api_key",
  "opencode_api_key",
  "gemini_api_key",
  "anthropic_api_key",
  "llm_api_key",
] as const;
type SecretField = (typeof SECRET_FIELDS)[number];

export interface RuntimeSettings {
  stt_provider: string;
  deepgram_api_key?: string;
  groq_api_key?: string;
  openai_api_key?: string;
  sensevoice_base_url?: string;
  llm_provider: string;
  llm_base_url: string;
  llm_model: string;
  tts_provider: string;
  kokoro_base_url: string;
  vad_silence_ms: number;
}

export interface ActiveProviders {
  stt: string;
  llm: string;
  tts: string;
}

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  settings: RuntimeSettings;
  activeProviders: ActiveProviders;
  // secrets_set[field] is true when the server holds a live credential for
  // that field. GET /settings never sends the value itself.
  secretsSet: Partial<Record<SecretField, boolean>>;
  onApplySettings: (
    newSettings: Partial<RuntimeSettings> & {
      deepgram_api_key?: string;
      groq_api_key?: string;
      openai_api_key?: string;
      sensevoice_base_url?: string;
      llm_api_key?: string;
    }
  ) => Promise<void>;
  t: Record<string, string>;
}

// Placeholder for a password-type credential input: never the value itself.
function secretPlaceholder(field: SecretField, secretsSet: SettingsModalProps["secretsSet"]): string {
  return secretsSet[field] ? "set (hidden)" : "not set";
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  settings,
  activeProviders,
  secretsSet,
  onApplySettings,
  t,
}) => {
  const [sttProvider, setSttProvider] = useState(settings.stt_provider || "stub");
  // Credential inputs always start empty — the server never returns the
  // real value (it sends "***" when set), so there is nothing safe to
  // prefill. secretsSet drives the placeholder instead.
  const [deepgramKey, setDeepgramKey] = useState("");
  const [groqKey, setGroqKey] = useState("");
  const [openaiKey, setOpenaiKey] = useState("");
  const [sensevoiceUrl, setSensevoiceUrl] = useState(settings.sensevoice_base_url || "http://127.0.0.1:8086");
  const [llmProvider, setLlmProvider] = useState(settings.llm_provider || "stub");
  // Default base URL comes from the server (settings.llm_base_url); no
  // hardcoded fleet address here.
  const [llmBaseUrl, setLlmBaseUrl] = useState(settings.llm_base_url || "");
  const [llmModel, setLlmModel] = useState(settings.llm_model || "claude-3-7-sonnet");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [ttsProvider, setTtsProvider] = useState(settings.tts_provider || "stub");
  const [kokoroUrl, setKokoroUrl] = useState(settings.kokoro_base_url || "http://127.0.0.1:8088");
  const [vadSilenceMs, setVadSilenceMs] = useState(settings.vad_silence_ms || 600);
  const [saving, setSaving] = useState(false);
  const [statusMsg, setStatusMsg] = useState("");
  const [clearingField, setClearingField] = useState<SecretField | null>(null);

  if (!isOpen) return null;

  const clearButtonStyle: React.CSSProperties = {
    background: "none",
    border: "1px solid #282c3f",
    color: "#9ba3b8",
    borderRadius: 6,
    padding: "0 0.5rem",
    fontSize: "0.7rem",
    cursor: "pointer",
    marginLeft: "0.35rem",
  };

  // Posts an explicit "" for one credential field — the one way to wipe a
  // live key, since a blank typed value is treated as "leave unchanged".
  const handleClearSecret = async (field: SecretField, resetLocal: () => void) => {
    setClearingField(field);
    setStatusMsg("");
    try {
      await onApplySettings({ [field]: "" } as Partial<RuntimeSettings> & Record<string, string>);
      resetLocal();
      setStatusMsg(`✓ Cleared ${field}`);
    } catch {
      setStatusMsg(`Failed to clear ${field}`);
    } finally {
      setClearingField(null);
    }
  };

  const applyPreset = (preset: "mocks" | "fleet" | "cloud" | "apple") => {
    if (preset === "mocks") {
      setSttProvider("stub");
      setLlmProvider("stub");
      setTtsProvider("stub");
    } else if (preset === "fleet") {
      setSttProvider("sensevoice");
      setSensevoiceUrl("http://127.0.0.1:8086");
      setLlmProvider("litellm");
      setLlmModel("claude-3-7-sonnet");
      setTtsProvider("kokoro");
      setKokoroUrl("http://127.0.0.1:8088");
    } else if (preset === "cloud") {
      setSttProvider("groq");
      setLlmProvider("openai");
      setLlmBaseUrl("https://api.openai.com/v1");
      setLlmModel("gpt-4o");
      setTtsProvider("elevenlabs");
    } else if (preset === "apple") {
      setSttProvider("whisperkit");
      setLlmProvider("stub");
      setTtsProvider("kokoro");
    }
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setStatusMsg("");
    try {
      await onApplySettings({
        stt_provider: sttProvider,
        deepgram_api_key: deepgramKey || undefined,
        groq_api_key: groqKey || undefined,
        openai_api_key: openaiKey || undefined,
        sensevoice_base_url: sensevoiceUrl || undefined,
        llm_provider: llmProvider,
        llm_base_url: llmBaseUrl,
        llm_model: llmModel,
        llm_api_key: llmApiKey || undefined,
        tts_provider: ttsProvider,
        kokoro_base_url: kokoroUrl,
        vad_silence_ms: vadSilenceMs,
      });
      setStatusMsg("✓ Tires hot-swapped live");
      setTimeout(() => {
        setStatusMsg("");
        onClose();
      }, 900);
    } catch {
      setStatusMsg("Failed to update settings");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        backgroundColor: "rgba(9, 10, 15, 0.75)",
        backdropFilter: "blur(8px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 200,
        padding: "1rem",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 540,
          maxHeight: "90vh",
          overflowY: "auto",
          background: "#12141c",
          border: "1px solid #282c3f",
          borderRadius: "16px",
          padding: "1.5rem",
          color: "#f1f3f9",
          boxShadow: "0 12px 32px rgba(0,0,0,0.7)",
          fontFamily: "system-ui, -apple-system, sans-serif",
        }}
      >
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1.25rem" }}>
          <div>
            <h3 style={{ margin: 0, fontSize: "1.15rem", fontWeight: 600 }}>
              {t["settings"] || "Settings"} & Swappable Tires
            </h3>
            <span style={{ fontSize: "0.75rem", color: "#636c84" }}>
              Hot-swap models without restarting the duplex server
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            style={{ background: "none", border: "none", color: "#9ba3b8", fontSize: "1.2rem", cursor: "pointer" }}
          >
            ✕
          </button>
        </div>

        {/* Active Providers Pill */}
        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            background: "#191c26",
            border: "1px solid #282c3f",
            borderRadius: "8px",
            padding: "0.5rem 0.75rem",
            fontSize: "0.75rem",
            marginBottom: "1.25rem",
            fontFamily: "monospace",
          }}
        >
          <span style={{ color: "#636c84" }}>Active:</span>
          <span>STT: <b style={{ color: activeProviders.stt === "StubSTT" ? "#ffb300" : "#00c853" }}>{activeProviders.stt}</b></span>
          <span>•</span>
          <span>LLM: <b style={{ color: activeProviders.llm === "StubLLM" ? "#ffb300" : "#24c1e0" }}>{activeProviders.llm}</b></span>
          <span>•</span>
          <span>TTS: <b style={{ color: activeProviders.tts === "StubTTS" ? "#ffb300" : "#a142f4" }}>{activeProviders.tts}</b></span>
        </div>

        {/* 1-Click Presets */}
        <div style={{ marginBottom: "1.25rem" }}>
          <label style={{ display: "block", fontSize: "0.75rem", color: "#9ba3b8", marginBottom: "0.4rem", fontWeight: 600 }}>
            Quick Tire Presets
          </label>
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={() => applyPreset("mocks")}
              style={{
                background: "#191c26",
                border: "1px solid #282c3f",
                color: "#ffb300",
                borderRadius: "6px",
                padding: "0.35rem 0.65rem",
                fontSize: "0.75rem",
                cursor: "pointer",
                fontWeight: 500,
              }}
            >
              ⚡ Local Mocks ($0)
            </button>
            <button
              type="button"
              onClick={() => applyPreset("fleet")}
              style={{
                background: "#191c26",
                border: "1px solid #282c3f",
                color: "#24c1e0",
                borderRadius: "6px",
                padding: "0.35rem 0.65rem",
                fontSize: "0.75rem",
                cursor: "pointer",
                fontWeight: 500,
              }}
            >
              🚀 SpacePilot Fleet + Kokoro
            </button>
            <button
              type="button"
              onClick={() => applyPreset("cloud")}
              style={{
                background: "#191c26",
                border: "1px solid #282c3f",
                color: "#00c853",
                borderRadius: "6px",
                padding: "0.35rem 0.65rem",
                fontSize: "0.75rem",
                cursor: "pointer",
                fontWeight: 500,
              }}
            >
              ☁️ Cloud (OpenAI + ElevenLabs)
            </button>
          </div>
        </div>

        {/* Detailed Form */}
        <form onSubmit={handleSave}>
          {/* STT Settings */}
          <div style={{ marginBottom: "1rem", padding: "0.75rem", background: "#191c26", borderRadius: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.4rem" }}>
              <label style={{ fontSize: "0.8rem", fontWeight: 600, color: "#f1f3f9" }}>STT (Speech to Text Matrix)</label>
              <span style={{ fontSize: "0.7rem", color: "#636c84" }}>Cloud • Apple Silicon • Fleet</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
              <select
                value={sttProvider}
                onChange={(e) => setSttProvider(e.target.value)}
                style={{
                  padding: "0.4rem 0.6rem",
                  borderRadius: 6,
                  border: "1px solid #282c3f",
                  background: "#090a0f",
                  color: "#f1f3f9",
                  fontSize: "0.8rem",
                }}
              >
                <optgroup label="Cloud Flagships">
                  <option value="deepgram">Deepgram Nova-3 (Cloud)</option>
                  <option value="groq">Groq Whisper LPU (Ultra-Fast)</option>
                  <option value="openai">OpenAI Whisper (Cloud)</option>
                </optgroup>
                <optgroup label="Apple Silicon (Local)">
                  <option value="whisperkit">WhisperKit (CoreML ANE)</option>
                  <option value="mlx">MLX Whisper (Apple GPU)</option>
                </optgroup>
                <optgroup label="Sovereign Fleet">
                  <option value="sensevoice">SenseVoice Fleet (:8086)</option>
                </optgroup>
                <optgroup label="Zero-Cost Mock">
                  <option value="stub">StubSTT (Zero download)</option>
                </optgroup>
              </select>
              {sttProvider === "deepgram" && (
                <div style={{ display: "flex", alignItems: "center" }}>
                  <input
                    type="password"
                    placeholder={secretPlaceholder("deepgram_api_key", secretsSet)}
                    value={deepgramKey}
                    onChange={(e) => setDeepgramKey(e.target.value)}
                    style={{
                      flex: 1,
                      padding: "0.4rem 0.6rem",
                      borderRadius: 6,
                      border: "1px solid #282c3f",
                      background: "#090a0f",
                      color: "#f1f3f9",
                      fontSize: "0.8rem",
                    }}
                  />
                  {secretsSet.deepgram_api_key && (
                    <button
                      type="button"
                      style={clearButtonStyle}
                      disabled={clearingField === "deepgram_api_key"}
                      onClick={() => handleClearSecret("deepgram_api_key", () => setDeepgramKey(""))}
                    >
                      clear
                    </button>
                  )}
                </div>
              )}
              {sttProvider === "groq" && (
                <div style={{ display: "flex", alignItems: "center" }}>
                  <input
                    type="password"
                    placeholder={secretPlaceholder("groq_api_key", secretsSet)}
                    value={groqKey}
                    onChange={(e) => setGroqKey(e.target.value)}
                    style={{
                      flex: 1,
                      padding: "0.4rem 0.6rem",
                      borderRadius: 6,
                      border: "1px solid #282c3f",
                      background: "#090a0f",
                      color: "#f1f3f9",
                      fontSize: "0.8rem",
                    }}
                  />
                  {secretsSet.groq_api_key && (
                    <button
                      type="button"
                      style={clearButtonStyle}
                      disabled={clearingField === "groq_api_key"}
                      onClick={() => handleClearSecret("groq_api_key", () => setGroqKey(""))}
                    >
                      clear
                    </button>
                  )}
                </div>
              )}
              {sttProvider === "openai" && (
                <div style={{ display: "flex", alignItems: "center" }}>
                  <input
                    type="password"
                    placeholder={secretPlaceholder("openai_api_key", secretsSet)}
                    value={openaiKey}
                    onChange={(e) => setOpenaiKey(e.target.value)}
                    style={{
                      flex: 1,
                      padding: "0.4rem 0.6rem",
                      borderRadius: 6,
                      border: "1px solid #282c3f",
                      background: "#090a0f",
                      color: "#f1f3f9",
                      fontSize: "0.8rem",
                    }}
                  />
                  {secretsSet.openai_api_key && (
                    <button
                      type="button"
                      style={clearButtonStyle}
                      disabled={clearingField === "openai_api_key"}
                      onClick={() => handleClearSecret("openai_api_key", () => setOpenaiKey(""))}
                    >
                      clear
                    </button>
                  )}
                </div>
              )}
              {sttProvider === "sensevoice" && (
                <input
                  type="text"
                  placeholder="SenseVoice URL (http://...)"
                  value={sensevoiceUrl}
                  onChange={(e) => setSensevoiceUrl(e.target.value)}
                  style={{
                    padding: "0.4rem 0.6rem",
                    borderRadius: 6,
                    border: "1px solid #282c3f",
                    background: "#090a0f",
                    color: "#f1f3f9",
                    fontSize: "0.8rem",
                  }}
                />
              )}
              {sttProvider === "whisperkit" && (
                <div style={{ display: "flex", alignItems: "center", fontSize: "0.75rem", color: "#9ba3b8", paddingLeft: "0.25rem" }}>
                  <span>⚡ Apple Neural Engine CoreML</span>
                </div>
              )}
              {sttProvider === "mlx" && (
                <div style={{ display: "flex", alignItems: "center", fontSize: "0.75rem", color: "#9ba3b8", paddingLeft: "0.25rem" }}>
                  <span>🚀 Apple Silicon GPU via MLX</span>
                </div>
              )}
              {sttProvider === "stub" && (
                <div style={{ display: "flex", alignItems: "center", fontSize: "0.75rem", color: "#9ba3b8", paddingLeft: "0.25rem" }}>
                  <span>✓ In-memory mock ($0)</span>
                </div>
              )}
            </div>
          </div>

          {/* LLM Settings */}
          <div style={{ marginBottom: "1rem", padding: "0.75rem", background: "#191c26", borderRadius: 8 }}>
            <div style={{ marginBottom: "0.4rem" }}>
              <label style={{ fontSize: "0.8rem", fontWeight: 600, color: "#f1f3f9" }}>LLM (Language & Reasoning)</label>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem", marginBottom: "0.5rem" }}>
              <select
                value={llmProvider}
                onChange={(e) => setLlmProvider(e.target.value)}
                style={{
                  padding: "0.4rem 0.6rem",
                  borderRadius: 6,
                  border: "1px solid #282c3f",
                  background: "#090a0f",
                  color: "#f1f3f9",
                  fontSize: "0.8rem",
                }}
              >
                <option value="stub">StubLLM (3 canned clauses)</option>
                <option value="litellm">LiteLLM Fleet (SpacePilot proxy)</option>
                <option value="openai">OpenAI Compatible (Live)</option>
              </select>
              <input
                type="text"
                placeholder="Model (e.g. claude-3-7-sonnet)"
                value={llmModel}
                onChange={(e) => setLlmModel(e.target.value)}
                disabled={llmProvider === "stub"}
                style={{
                  padding: "0.4rem 0.6rem",
                  borderRadius: 6,
                  border: "1px solid #282c3f",
                  background: "#090a0f",
                  color: "#f1f3f9",
                  fontSize: "0.8rem",
                  opacity: llmProvider === "stub" ? 0.5 : 1,
                }}
              />
            </div>
            {llmProvider !== "stub" && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
                <input
                  type="text"
                  placeholder="Base URL (e.g. http://127.0.0.1:4000/v1)"
                  value={llmBaseUrl}
                  onChange={(e) => setLlmBaseUrl(e.target.value)}
                  style={{
                    padding: "0.4rem 0.6rem",
                    borderRadius: 6,
                    border: "1px solid #282c3f",
                    background: "#090a0f",
                    color: "#f1f3f9",
                    fontSize: "0.75rem",
                  }}
                />
                <div style={{ display: "flex", alignItems: "center" }}>
                  <input
                    type="password"
                    placeholder={secretPlaceholder("llm_api_key", secretsSet) + " (optional if proxy)"}
                    value={llmApiKey}
                    onChange={(e) => setLlmApiKey(e.target.value)}
                    style={{
                      flex: 1,
                      padding: "0.4rem 0.6rem",
                      borderRadius: 6,
                      border: "1px solid #282c3f",
                      background: "#090a0f",
                      color: "#f1f3f9",
                      fontSize: "0.75rem",
                    }}
                  />
                  {secretsSet.llm_api_key && (
                    <button
                      type="button"
                      style={clearButtonStyle}
                      disabled={clearingField === "llm_api_key"}
                      onClick={() => handleClearSecret("llm_api_key", () => setLlmApiKey(""))}
                    >
                      clear
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* TTS Settings */}
          <div style={{ marginBottom: "1rem", padding: "0.75rem", background: "#191c26", borderRadius: 8 }}>
            <div style={{ marginBottom: "0.4rem" }}>
              <label style={{ fontSize: "0.8rem", fontWeight: 600, color: "#f1f3f9" }}>TTS (Voice Synthesis)</label>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
              <select
                value={ttsProvider}
                onChange={(e) => setTtsProvider(e.target.value)}
                style={{
                  padding: "0.4rem 0.6rem",
                  borderRadius: 6,
                  border: "1px solid #282c3f",
                  background: "#090a0f",
                  color: "#f1f3f9",
                  fontSize: "0.8rem",
                }}
              >
                <option value="stub">StubTTS (440Hz Sine WAV)</option>
                <option value="kokoro">Kokoro SpacePilot (:8088)</option>
                <option value="elevenlabs">ElevenLabs (Live)</option>
                <option value="deepgram">Deepgram Aura (Live)</option>
              </select>
              {ttsProvider === "kokoro" && (
                <input
                  type="text"
                  placeholder="Kokoro URL (:8088)"
                  value={kokoroUrl}
                  onChange={(e) => setKokoroUrl(e.target.value)}
                  style={{
                    padding: "0.4rem 0.6rem",
                    borderRadius: 6,
                    border: "1px solid #282c3f",
                    background: "#090a0f",
                    color: "#f1f3f9",
                    fontSize: "0.8rem",
                  }}
                />
              )}
            </div>
          </div>

          {/* VAD Settings */}
          <div style={{ marginBottom: "1.25rem", padding: "0.75rem", background: "#191c26", borderRadius: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", marginBottom: "0.35rem" }}>
              <span style={{ fontWeight: 600 }}>Hands-Free Silence Cutoff</span>
              <span style={{ color: "#24c1e0", fontFamily: "monospace" }}>{vadSilenceMs}ms</span>
            </div>
            <input
              type="range"
              min="300"
              max="1500"
              step="50"
              value={vadSilenceMs}
              onChange={(e) => setVadSilenceMs(parseInt(e.target.value, 10))}
              style={{ width: "100%", accentColor: "#24c1e0", cursor: "pointer" }}
            />
          </div>

          {/* Footer Actions */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: "0.8rem", color: statusMsg.startsWith("✓") ? "#00c853" : "#ff3d00" }}>
              {statusMsg}
            </span>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <button
                type="button"
                onClick={onClose}
                style={{
                  background: "#191c26",
                  border: "1px solid #282c3f",
                  color: "#f1f3f9",
                  borderRadius: "6px",
                  padding: "0.45rem 0.85rem",
                  fontSize: "0.8rem",
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                style={{
                  background: "#4285f4",
                  color: "#ffffff",
                  border: "none",
                  borderRadius: "6px",
                  padding: "0.45rem 1.25rem",
                  fontSize: "0.8rem",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                {saving ? "Applying..." : "Apply Tires Live"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
};
