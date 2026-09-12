import React, { useState } from "react";
import { setStudioToken, studioToken } from "../ws";

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
  // Fired after the "Connect" field saves a new studio token — App
  // reconnects the WS and clears the auth-error banner.
  onTokenSaved?: () => void;
  /** Provider class names and the VAD cutoff's raw ms unit are engineering
   * detail (Finding 8) — shown only when the Developer toggle is on. */
  developer?: boolean;
  t: Record<string, string>;
}

// A row of side-by-side fields. Layout only — no colour, spacing from the
// ladder token, so it stays exempt from the "no ad-hoc pixel" rule.
const twoCol: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: "var(--pt-s3)",
};

const inlineRow: React.CSSProperties = {
  display: "flex",
  gap: "var(--pt-s2)",
};

// Placeholder for a password-type credential input: never the value itself.
function secretPlaceholder(field: SecretField, secretsSet: SettingsModalProps["secretsSet"]): string {
  return secretsSet[field] ? "set (hidden)" : "not set";
}

// A provider class name (e.g. "StubSTT", "FasterWhisperSTT") is engineering
// detail (Finding 8) — developer-off gets a plain status word instead.
function providerLabel(name: string, developer: boolean): string {
  if (developer) return name;
  return name === "StubSTT" || name === "StubLLM" || name === "StubTTS" ? "Off" : "Active";
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  settings,
  activeProviders,
  secretsSet,
  onApplySettings,
  onTokenSaved,
  developer = false,
  t,
}) => {
  const [tokenInput, setTokenInput] = useState("");
  const [tokenStatus, setTokenStatus] = useState("");
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

  const handleSaveToken = () => {
    setStudioToken(tokenInput.trim());
    setTokenInput("");
    setTokenStatus(tokenInput.trim() ? "✓ Saved — reconnecting" : "Cleared");
    onTokenSaved?.();
    setTimeout(() => setTokenStatus(""), 1500);
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

  const secretRow = (
    field: SecretField,
    id: string,
    label: string,
    value: string,
    onChange: (v: string) => void,
    extraHint?: string
  ) => (
    <div className="pt-field">
      <label htmlFor={id}>
        <span className={`pt-dot ${secretsSet[field] ? "pt-dot--live" : "pt-dot--down"}`} /> {label}
      </label>
      <div style={inlineRow}>
        <input
          id={id}
          className="pt-input"
          type="password"
          placeholder={extraHint ? `${secretPlaceholder(field, secretsSet)} ${extraHint}` : secretPlaceholder(field, secretsSet)}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        {secretsSet[field] && (
          <button
            type="button"
            className="pt-btn pt-btn--ghost"
            disabled={clearingField === field}
            onClick={() => handleClearSecret(field, () => onChange(""))}
          >
            clear
          </button>
        )}
      </div>
    </div>
  );

  return (
    <>
      <div className="pt-scrim" onClick={onClose} />
      <div className="pt-sheet" role="dialog" aria-modal="true" aria-label={t["settings"] || "Settings"}>
        <div className="pt-sheet-head">
          <div>
            <h2 className="pt-h2">{t["settings"] || "Settings"} &amp; Swappable Tires</h2>
            <p className="pt-note">Hot-swap models without restarting the duplex server</p>
          </div>
          <button type="button" className="pt-btn pt-btn--icon pt-btn--ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        {/* Active Providers */}
        <div className="pt-section">
          <p className="pt-lbl">Active</p>
          <dl className="pt-kv">
            <dt className="pt-k">STT</dt>
            <dd className="pt-v">
              <span className={`pt-chip ${activeProviders.stt === "StubSTT" ? "" : "pt-chip--signal"}`}>
                <span className={`pt-dot ${activeProviders.stt === "StubSTT" ? "pt-dot--down" : "pt-dot--live"}`} />
                {providerLabel(activeProviders.stt, developer)}
              </span>
            </dd>
            <dt className="pt-k">LLM</dt>
            <dd className="pt-v">
              <span className={`pt-chip ${activeProviders.llm === "StubLLM" ? "" : "pt-chip--signal"}`}>
                <span className={`pt-dot ${activeProviders.llm === "StubLLM" ? "pt-dot--down" : "pt-dot--live"}`} />
                {providerLabel(activeProviders.llm, developer)}
              </span>
            </dd>
            <dt className="pt-k">TTS</dt>
            <dd className="pt-v">
              <span className={`pt-chip ${activeProviders.tts === "StubTTS" ? "" : "pt-chip--signal"}`}>
                <span className={`pt-dot ${activeProviders.tts === "StubTTS" ? "pt-dot--down" : "pt-dot--live"}`} />
                {providerLabel(activeProviders.tts, developer)}
              </span>
            </dd>
          </dl>
        </div>

        {/* Studio Token — server/auth.py requires X-Studio-Token on every
            mutating route and on the WS handshake (?token= for browsers). */}
        <div className="pt-section">
          <div className="pt-field">
            <label htmlFor="studio-token">Studio Token</label>
            <div style={inlineRow}>
              <input
                id="studio-token"
                className="pt-input"
                type="password"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder={studioToken() ? "set (hidden) — paste to replace" : "paste the token from .qa-scratch/studio.token"}
              />
              <button type="button" className="pt-btn" onClick={handleSaveToken}>
                Connect
              </button>
            </div>
            {tokenStatus && <span className="pt-hint">{tokenStatus}</span>}
          </div>
        </div>

        {/* 1-Click Presets */}
        <div className="pt-section">
          <p className="pt-lbl">Quick Tire Presets</p>
          <div style={{ ...inlineRow, flexWrap: "wrap" }}>
            <button type="button" className="pt-btn" onClick={() => applyPreset("mocks")}>
              Local Mocks ($0)
            </button>
            <button type="button" className="pt-btn" onClick={() => applyPreset("fleet")}>
              SpacePilot Fleet + Kokoro
            </button>
            <button type="button" className="pt-btn" onClick={() => applyPreset("cloud")}>
              Cloud (OpenAI + ElevenLabs)
            </button>
          </div>
        </div>

        {/* Detailed Form */}
        <form onSubmit={handleSave}>
          {/* STT Settings */}
          <div className="pt-section">
            <div className="pt-sheet-head">
              <h3 className="pt-h3">STT (Speech to Text Matrix)</h3>
              <span className="pt-note pt-mono">Cloud · Apple Silicon · Fleet</span>
            </div>
            <div style={twoCol}>
              <div className="pt-field">
                <label htmlFor="stt-provider">Provider</label>
                <select id="stt-provider" className="pt-input" value={sttProvider} onChange={(e) => setSttProvider(e.target.value)}>
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
              </div>

              {sttProvider === "deepgram" &&
                secretRow("deepgram_api_key", "deepgram-key", "Deepgram key", deepgramKey, setDeepgramKey)}
              {sttProvider === "groq" && secretRow("groq_api_key", "groq-key", "Groq key", groqKey, setGroqKey)}
              {sttProvider === "openai" &&
                secretRow("openai_api_key", "openai-stt-key", "OpenAI key", openaiKey, setOpenaiKey)}
              {sttProvider === "sensevoice" && (
                <div className="pt-field">
                  <label htmlFor="sensevoice-url">SenseVoice URL</label>
                  <input
                    id="sensevoice-url"
                    className="pt-input"
                    type="text"
                    placeholder="http://..."
                    value={sensevoiceUrl}
                    onChange={(e) => setSensevoiceUrl(e.target.value)}
                  />
                </div>
              )}
              {sttProvider === "whisperkit" && <p className="pt-note">Apple Neural Engine CoreML</p>}
              {sttProvider === "mlx" && <p className="pt-note">Apple Silicon GPU via MLX</p>}
              {sttProvider === "stub" && <p className="pt-note">In-memory mock ($0)</p>}
            </div>
          </div>

          {/* LLM Settings */}
          <div className="pt-section">
            <h3 className="pt-h3">LLM (Language &amp; Reasoning)</h3>
            <div style={twoCol}>
              <div className="pt-field">
                <label htmlFor="llm-provider">Provider</label>
                <select id="llm-provider" className="pt-input" value={llmProvider} onChange={(e) => setLlmProvider(e.target.value)}>
                  <option value="stub">StubLLM (3 canned clauses)</option>
                  <option value="litellm">LiteLLM Fleet (SpacePilot proxy)</option>
                  <option value="openai">OpenAI Compatible (Live)</option>
                </select>
              </div>
              <div className="pt-field">
                <label htmlFor="llm-model">Model</label>
                <input
                  id="llm-model"
                  className="pt-input"
                  type="text"
                  placeholder="e.g. claude-3-7-sonnet"
                  value={llmModel}
                  onChange={(e) => setLlmModel(e.target.value)}
                  disabled={llmProvider === "stub"}
                />
              </div>
            </div>
            {llmProvider !== "stub" && (
              <div style={{ ...twoCol, marginTop: "var(--pt-s3)" }}>
                <div className="pt-field">
                  <label htmlFor="llm-base-url">Base URL</label>
                  <input
                    id="llm-base-url"
                    className="pt-input"
                    type="text"
                    placeholder="e.g. http://127.0.0.1:4000/v1"
                    value={llmBaseUrl}
                    onChange={(e) => setLlmBaseUrl(e.target.value)}
                  />
                </div>
                {secretRow("llm_api_key", "llm-api-key", "API key", llmApiKey, setLlmApiKey, "(optional if proxy)")}
              </div>
            )}
          </div>

          {/* TTS Settings */}
          <div className="pt-section">
            <h3 className="pt-h3">TTS (Voice Synthesis)</h3>
            <div style={twoCol}>
              <div className="pt-field">
                <label htmlFor="tts-provider">Provider</label>
                <select id="tts-provider" className="pt-input" value={ttsProvider} onChange={(e) => setTtsProvider(e.target.value)}>
                  <option value="stub">StubTTS (440Hz Sine WAV)</option>
                  <option value="kokoro">Kokoro SpacePilot (:8088)</option>
                  <option value="elevenlabs">ElevenLabs (Live)</option>
                  <option value="deepgram">Deepgram Aura (Live)</option>
                </select>
              </div>
              {ttsProvider === "kokoro" && (
                <div className="pt-field">
                  <label htmlFor="kokoro-url">Kokoro URL</label>
                  <input
                    id="kokoro-url"
                    className="pt-input"
                    type="text"
                    placeholder=":8088"
                    value={kokoroUrl}
                    onChange={(e) => setKokoroUrl(e.target.value)}
                  />
                </div>
              )}
            </div>
          </div>

          {/* VAD Settings */}
          <div className="pt-section">
            <div className="pt-field">
              <label htmlFor="vad-silence">
                Hands-Free Silence Cutoff{" "}
                <span className="pt-num">
                  {developer ? `${vadSilenceMs}ms` : `${(vadSilenceMs / 1000).toFixed(1)} seconds`}
                </span>
              </label>
              <input
                id="vad-silence"
                type="range"
                min="300"
                max="1500"
                step="50"
                value={vadSilenceMs}
                onChange={(e) => setVadSilenceMs(parseInt(e.target.value, 10))}
                style={{ accentColor: "var(--mv-accent)", width: "100%" }}
              />
            </div>
          </div>

          {/* Footer Actions */}
          <div className="pt-sheet-head">
            {statusMsg &&
              (statusMsg.startsWith("✓") ? (
                <span className="pt-chip pt-chip--signal">{statusMsg}</span>
              ) : (
                <div className="pt-banner">
                  <span>{statusMsg}</span>
                </div>
              ))}
            <div style={inlineRow}>
              <button type="button" className="pt-btn" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="pt-btn pt-btn--primary" disabled={saving}>
                {saving ? "Applying..." : "Apply Tires Live"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </>
  );
};
