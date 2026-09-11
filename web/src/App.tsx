import { useCallback, useEffect, useRef, useState } from "react";
import {
  AgentState,
  PersonaId,
  ServerFrame,
  WS_URL,
  float32ToBase64Pcm16,
  newTurnId,
  socket,
} from "./ws";
import en from "./i18n/en.json";
import hi from "./i18n/hi.json";
import { AcousticOrb } from "./components/AcousticOrb";
import { LatencyMetrics, TelemetryHud } from "./components/TelemetryHud";
import { PersonaData, PersonaStudio } from "./components/PersonaStudio";
import { MemoryDrawer, MemoryTurn } from "./components/MemoryDrawer";
import { ActiveProviders, RuntimeSettings, SettingsModal } from "./components/SettingsModal";
import { PromptComposer } from "./components/PromptComposer";

type Strings = typeof en;
const STRINGS: Record<"en" | "hi", Strings> = { en, hi };

const STATIC_VOICES = [
  { id: "af_heart", display_name: "Heart (EN)", lang: "en" },
  { id: "am_adam", display_name: "Adam (EN)", lang: "en" },
  { id: "shannon", display_name: "Shannon", lang: "en" },
  { id: "archie", display_name: "Archie", lang: "en" },
];

const DEFAULT_PERSONAS: PersonaData[] = [
  {
    name: "donna",
    voice: "af_heart",
    speed: 1.05,
    stalls: ["On it — one sec.", "Checking that right now."],
    tone: "Razor-competent executive Chief of Staff. Concise, dry wit, never servile.",
  },
  {
    name: "jarvis",
    voice: "am_adam",
    speed: 0.95,
    stalls: ["One moment, sir.", "Consulting relevant systems."],
    tone: "Dry British butler precision. States latencies and facts plainly.",
  },
  {
    name: "zuck",
    voice: "am_adam",
    speed: 1.0,
    stalls: ["Great question — building the answer now.", "Connecting ideas."],
    tone: "Earnest metaverse founder energy. Passionate about connection and community.",
  },
];

interface TranscriptLine {
  id: string;
  who: "user" | "agent";
  text: string;
  audio_url?: string;
  time: string;
}

interface QueuedSentence {
  index: number;
  text: string;
  audio_url: string;
}

function httpBaseFromWs(wsUrl: string): string {
  return wsUrl.replace(/^ws/, "http").replace(/\/ws\/?$/, "");
}

function resolveAudioUrl(url?: string): string {
  if (!url) return "";
  if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("blob:")) {
    return url;
  }
  const base = httpBaseFromWs(WS_URL);
  return `${base}${url.startsWith("/") ? "" : "/"}${url}`;
}

export default function App() {
  const [lang, setLang] = useState<"en" | "hi">("en");
  const t = STRINGS[lang];

  // Core Connection & State
  const [connected, setConnected] = useState(false);
  const [state, setState] = useState<AgentState>("idle");
  const [activeSentence, setActiveSentence] = useState<string>("");

  // Persona & Voice Customization
  const [personas, setPersonas] = useState<PersonaData[]>(DEFAULT_PERSONAS);
  const [currentPersona, setCurrentPersona] = useState<PersonaId>("donna");
  const [voice, setVoice] = useState(STATIC_VOICES[0].id);
  const [voices, setVoices] = useState(STATIC_VOICES);
  const [speed, setSpeed] = useState(1.05);
  const [systemPrompt, setSystemPrompt] = useState("");

  // Mode & Audio Controls
  const [inputMode, setInputMode] = useState<"ptt" | "handsfree">("ptt");
  const [muted, setMuted] = useState(false);
  const [talking, setTalking] = useState(false);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);

  // Modals & Panels
  const [showStudio, setShowStudio] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [showTelemetry, setShowTelemetry] = useState(true);
  const [showSettings, setShowSettings] = useState(false);

  // Runtime Settings & Swappable Tires
  const [settings, setSettings] = useState<RuntimeSettings>({
    stt_provider: "stub",
    llm_provider: "stub",
    llm_base_url: "http://100.99.50.84:8000/v1",
    llm_model: "claude-3-7-sonnet",
    tts_provider: "stub",
    kokoro_base_url: "http://127.0.0.1:8088",
    vad_silence_ms: 600,
  });
  const [activeProviders, setActiveProviders] = useState<ActiveProviders>({
    stt: "StubSTT",
    llm: "StubLLM",
    tts: "StubTTS",
  });

  // Telemetry Waterfall
  const [metrics, setMetrics] = useState<LatencyMetrics>({
    vadMs: 28,
    sttMs: 140,
    stallMs: 0.6,
    ttftMs: 380,
    ttsMs: 180,
    e2eMs: 560,
    provenance: "target",
  });
  const [queueLength, setQueueLength] = useState(0);

  // Memory & Transcripts
  const [memoryTurns, setMemoryTurns] = useState<MemoryTurn[]>([]);
  const [lines, setLines] = useState<TranscriptLine[]>([]);

  // Refs for audio and streaming
  const turnRef = useRef<string>(newTurnId());
  const turnStartTimeRef = useRef<number>(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<QueuedSentence[]>([]);
  const playingRef = useRef(false);
  const prefetchRef = useRef<HTMLAudioElement | null>(null);
  const micRef = useRef<{
    stream: MediaStream;
    ctx: AudioContext;
    proc: ScriptProcessorNode;
    src: MediaStreamAudioSourceNode;
    analyser: AnalyserNode;
  } | null>(null);

  // VAD state for hands-free mode
  const speechDetectedRef = useRef(false);
  const silenceTimerRef = useRef<number | null>(null);
  const recordedAudioRef = useRef<Float32Array[]>([]);

  // Stable refs for callbacks
  const personaRef = useRef(currentPersona);
  personaRef.current = currentPersona;
  const voiceRef = useRef(voice);
  voiceRef.current = voice;
  const speedRef = useRef(speed);
  speedRef.current = speed;
  const promptRef = useRef(systemPrompt);
  promptRef.current = systemPrompt;
  const mutedRef = useRef(muted);
  mutedRef.current = muted;

  const pushLine = useCallback((who: "user" | "agent", text: string, audio_url?: string) => {
    setLines((prev) => [
      ...prev,
      {
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        who,
        text,
        audio_url,
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
      },
    ]);
  }, []);

  // --- Playback queue: play sentence N while prefetching N+1 ---
  const pumpQueue = useCallback(() => {
    const el = audioRef.current;
    if (!el || playingRef.current) return;
    const next = queueRef.current.shift();
    setQueueLength(queueRef.current.length);
    if (!next) {
      setActiveSentence("");
      return;
    }
    playingRef.current = true;
    setActiveSentence(next.text);
    el.src = next.audio_url;
    el.playbackRate = speedRef.current;
    void el.play().catch(() => {
      playingRef.current = false;
      setActiveSentence("");
    });
    // Prefetch N+1 while N plays
    const upcoming = queueRef.current[0];
    if (upcoming) {
      const pre = new Audio();
      pre.preload = "auto";
      pre.src = upcoming.audio_url;
      prefetchRef.current = pre;
    }
  }, []);

  const onAudioEnded = useCallback(() => {
    playingRef.current = false;
    pumpQueue();
  }, [pumpQueue]);

  const killPlayback = useCallback(() => {
    queueRef.current = [];
    setQueueLength(0);
    playingRef.current = false;
    setActiveSentence("");
    prefetchRef.current?.removeAttribute("src");
    prefetchRef.current = null;
    const el = audioRef.current;
    if (el) {
      el.pause();
      el.removeAttribute("src");
      el.load();
    }
  }, []);

  const handleBarge = useCallback(() => {
    socket.send({ type: "barge", turn_id: turnRef.current });
    killPlayback();
    setState("listening");
    turnRef.current = newTurnId();
  }, [killPlayback]);

  const handleSendPrompt = useCallback(
    (promptText: string) => {
      if (!promptText.trim()) return;
      const turnId = newTurnId();
      turnRef.current = turnId;
      turnStartTimeRef.current = performance.now();
      setState("thinking");

      socket.send({
        type: "user.text",
        turn_id: turnId,
        text: promptText.trim(),
        persona: currentPersona,
        voice: voice,
        speed: speed,
        system_prompt: systemPrompt,
      });
    },
    [currentPersona, voice, speed, systemPrompt]
  );

  // --- Mic Engine: getUserMedia + ScriptProcessor + Analyser ---
  const startTalking = useCallback(async () => {
    try {
      recordedAudioRef.current = [];
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      const Ctx: typeof AudioContext =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new Ctx({ sampleRate: 16000 });
      const src = ctx.createMediaStreamSource(stream);

      // WebAudio Analyser for living orb visualization
      const ana = ctx.createAnalyser();
      ana.fftSize = 64;
      src.connect(ana);
      setAnalyser(ana);

      // Send user.start ONCE at the start of talking
      socket.send({
        type: "user.start",
        turn_id: turnRef.current,
        persona: personaRef.current,
        voice: voiceRef.current,
        speed: speedRef.current,
        system_prompt: promptRef.current,
      });

      // 512-sample chunks for smooth visualization & stream buffering
      const proc = ctx.createScriptProcessor(512, 1, 1);
      proc.onaudioprocess = (ev: AudioProcessingEvent) => {
        const input = ev.inputBuffer.getChannelData(0);
        const samples = new Float32Array(input.length);
        samples.set(input);
        recordedAudioRef.current.push(samples);

        // Calculate RMS for continuous VAD mode
        let sum = 0;
        for (let i = 0; i < samples.length; i++) {
          sum += samples[i] * samples[i];
        }
        const rms = Math.sqrt(sum / samples.length);

        if (inputMode === "handsfree") {
          if (rms > 0.02) {
            if (!speechDetectedRef.current) {
              speechDetectedRef.current = true;
              setTalking(true);
            }
            if (silenceTimerRef.current) {
              clearTimeout(silenceTimerRef.current);
              silenceTimerRef.current = null;
            }
          } else if (speechDetectedRef.current && !silenceTimerRef.current) {
            silenceTimerRef.current = setTimeout(() => {
              speechDetectedRef.current = false;
              stopTalking();
            }, 600);
          }
        }

        socket.send({
          type: "user.chunk",
          turn_id: turnRef.current,
          chunk: float32ToBase64Pcm16(samples),
        });
      };

      src.connect(proc);
      // Mute monitor to prevent feedback loops; zero-gain keeps ScriptProcessor active
      const zeroGain = ctx.createGain();
      zeroGain.gain.value = 0;
      proc.connect(zeroGain);
      zeroGain.connect(ctx.destination);

      micRef.current = { stream, ctx, proc, src, analyser: ana };
      setTalking(true);
      setState("listening");
      turnStartTimeRef.current = performance.now();
    } catch (err) {
      console.error("Failed to start mic:", err);
      setTalking(false);
    }
  }, [inputMode]);

  const stopTalking = useCallback(() => {
    if (!micRef.current) return;
    setTalking(false);
    turnStartTimeRef.current = performance.now();

    // Flatten recorded chunks into one complete PCM16 payload
    const totalLen = recordedAudioRef.current.reduce((acc, c) => acc + c.length, 0);
    let pcmB64 = "";
    if (totalLen > 0) {
      const merged = new Float32Array(totalLen);
      let offset = 0;
      for (const c of recordedAudioRef.current) {
        merged.set(c, offset);
        offset += c.length;
      }
      pcmB64 = float32ToBase64Pcm16(merged);
    }
    recordedAudioRef.current = [];

    // Stop tracks and close audio context cleanly
    try {
      micRef.current.stream.getTracks().forEach((t) => t.stop());
      void micRef.current.ctx.close();
    } catch {}
    micRef.current = null;

    socket.send({
      type: "user.stop",
      turn_id: turnRef.current,
      pcm_b64: pcmB64,
      sample_rate: 16000,
    });
  }, []);

  // --- WebSocket Setup & Event Demux ---
  useEffect(() => {
    socket.connect(WS_URL);
    setConnected(true);

    const off = socket.onFrame((frame: ServerFrame) => {
      const now = performance.now();
      const elapsed = turnStartTimeRef.current > 0 ? Math.round(now - turnStartTimeRef.current) : 0;

      switch (frame.type) {
        case "state.idle":
          setState("idle");
          break;
        case "state.listening":
          setState("listening");
          break;
        case "state.thinking":
          setState("thinking");
          break;
        case "state.speaking":
          setState("speaking");
          break;
        case "transcript.user":
          pushLine("user", frame.text);
          setMetrics((m) => ({
            ...m,
            sttMs: elapsed > 0 ? elapsed : m.sttMs,
            provenance: "flown",
          }));
          break;
        case "agent.stall":
          setMetrics((m) => ({
            ...m,
            stallMs: elapsed > 0 ? elapsed : 0.6,
            provenance: "flown",
          }));
          if (frame.audio_url && !mutedRef.current && audioRef.current) {
            queueRef.current.unshift({
              index: -1,
              text: frame.text || "One sec...",
              audio_url: resolveAudioUrl(frame.audio_url),
            });
            setQueueLength(queueRef.current.length);
            pumpQueue();
          }
          break;
        case "agent.sentence":
          pushLine("agent", frame.text, resolveAudioUrl(frame.audio_url));
          setMetrics((m) => ({
            ...m,
            ttftMs: m.ttftMs === 380 && elapsed > 0 ? elapsed : m.ttftMs,
            ttsMs: 180,
            provenance: "flown",
          }));
          if (!mutedRef.current) {
            queueRef.current.push({
              index: frame.index ?? frame.seq ?? 0,
              text: frame.text,
              audio_url: resolveAudioUrl(frame.audio_url),
            });
            queueRef.current.sort((a, b) => a.index - b.index);
            setQueueLength(queueRef.current.length);
            pumpQueue();
          }
          break;
        case "agent.done":
          setMetrics((m) => ({
            ...m,
            e2eMs: elapsed > 0 ? elapsed : m.e2eMs,
            provenance: "flown",
          }));
          turnRef.current = newTurnId();
          // Refresh memory drawer turns automatically
          fetch(`${httpBaseFromWs(WS_URL)}/ledger`)
            .then((r) => r.json())
            .then((d) => {
              if (d && Array.isArray(d.turns)) setMemoryTurns(d.turns);
            })
            .catch(() => {});
          break;
        case "agent.error":
          console.warn("[pet-talk] Agent error frame:", frame.reason);
          break;
      }
    });

    return () => {
      off();
      socket.close();
    };
  }, [pushLine, pumpQueue]);

  // --- Fetch Voices, Personas, and Memory Ledger on load ---
  useEffect(() => {
    const base = httpBaseFromWs(WS_URL);

    // 1. Fetch Voices
    fetch(`${base}/voices`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: unknown) => {
        if (!Array.isArray(data) || data.length === 0) return;
        const list = data.map((v) => ({
          id: v.id as string,
          display_name: v.display_name ?? (v.id as string),
          lang: v.lang ?? "en",
        }));
        setVoices(list);
      })
      .catch(() => {});

    // 2. Fetch Personas
    fetch(`${base}/personas`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: unknown) => {
        if (!Array.isArray(data) || data.length === 0) return;
        setPersonas(data);
        const current = data.find((p) => p.name === currentPersona);
        if (current) {
          setSpeed(current.speed);
          setSystemPrompt(current.tone);
          setVoice(current.voice);
        }
      })
      .catch(() => {});

    // 3. Fetch Memory Ledger
    fetch(`${base}/ledger`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: unknown) => {
        if (data && Array.isArray((data as { turns: MemoryTurn[] }).turns)) {
          setMemoryTurns((data as { turns: MemoryTurn[] }).turns);
        }
      })
      .catch(() => {});

    // 4. Fetch Runtime Settings & Active Providers
    fetch(`${base}/settings`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        if (d && d.settings) setSettings(d.settings);
        if (d && d.active) setActiveProviders(d.active);
      })
      .catch(() => {});
  }, [currentPersona]);

  // Handle Runtime Tire Switching
  const handleApplySettings = async (
    newSettings: Partial<RuntimeSettings> & { deepgram_api_key?: string; llm_api_key?: string },
  ) => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(newSettings),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.settings) setSettings(data.settings);
      if (data.active) setActiveProviders(data.active);
    }
  };

  // Handle Persona Selection
  const handleSelectPersona = (name: string) => {
    setCurrentPersona(name);
    const p = personas.find((item) => item.name === name);
    if (p) {
      setVoice(p.voice);
      setSpeed(p.speed);
      setSystemPrompt(p.tone);
    }
  };

  // Handle Save / Create Persona
  const handleSavePersona = async (personaData: PersonaData) => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/personas`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(personaData),
    });
    if (res.ok) {
      const refreshed = await fetch(`${base}/personas`).then((r) => r.json());
      setPersonas(refreshed);
      handleSelectPersona(personaData.name);
    }
  };

  // Handle Delete Persona
  const handleDeletePersona = async (name: string) => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/personas/${name}`, { method: "DELETE" });
    if (res.ok) {
      const refreshed = await fetch(`${base}/personas`).then((r) => r.json());
      setPersonas(refreshed);
      handleSelectPersona("donna");
    }
  };

  // Clear Memory
  const handleClearMemory = async () => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/ledger`, { method: "DELETE" });
    if (res.ok) {
      setMemoryTurns([]);
    }
  };

  // Voice Preview
  const handlePreviewVoice = (voiceId: string) => {
    const base = httpBaseFromWs(WS_URL);
    const audio = new Audio(`${base}/audio/preview-${encodeURIComponent(voiceId)}`);
    audio.play().catch(() => {});
  };

  // Keyboard Shortcuts (Space hold to talk, tap to barge, M to mute)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.code === "Space" && !e.repeat) {
        e.preventDefault();
        if (state === "speaking") {
          handleBarge();
        } else if (!talking) {
          startTalking();
        }
      } else if (e.key.toLowerCase() === "m") {
        setMuted((prev) => !prev);
      } else if (e.key === "Escape") {
        setShowStudio(false);
        setShowMemory(false);
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.code === "Space" && inputMode === "ptt") {
        e.preventDefault();
        if (talking) stopTalking();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [state, talking, inputMode, handleBarge, startTalking, stopTalking]);

  return (
    <div
      style={{
        backgroundColor: "#090a0f",
        minHeight: "100vh",
        color: "#f1f3f9",
        fontFamily: "'Google Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        boxSizing: "border-box",
        padding: "1.5rem 1rem",
      }}
    >
      {/* Top App Bar */}
      <header
        style={{
          width: "100%",
          maxWidth: 680,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "1.5rem",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "0.65rem" }}>
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: "50%",
              background: "linear-gradient(135deg, #4285f4, #a142f4)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 700,
              fontSize: "0.9rem",
              color: "#ffffff",
              boxShadow: "0 2px 8px rgba(66, 133, 244, 0.4)",
            }}
          >
            G
          </div>
          <span style={{ fontSize: "1.1rem", fontWeight: 600, letterSpacing: "-0.01em" }}>
            pet-talk <span style={{ fontSize: "0.75rem", color: "#24c1e0", fontWeight: 500 }}>duplex</span>
          </span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          {/* Language Toggle */}
          <button
            type="button"
            onClick={() => setLang((l) => (l === "en" ? "hi" : "en"))}
            style={{
              background: "#191c26",
              border: "1px solid #282c3f",
              color: "#f1f3f9",
              borderRadius: "999px",
              padding: "0.25rem 0.65rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {lang.toUpperCase()}
          </button>

          {/* Persona Studio Button */}
          <button
            type="button"
            onClick={() => setShowStudio((s) => !s)}
            style={{
              background: showStudio ? "#24c1e0" : "#191c26",
              color: showStudio ? "#090a0f" : "#f1f3f9",
              border: "1px solid #282c3f",
              borderRadius: "999px",
              padding: "0.25rem 0.75rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Studio
          </button>

          {/* Memory Ledger Button */}
          <button
            type="button"
            onClick={() => setShowMemory(true)}
            style={{
              background: "#191c26",
              border: "1px solid #282c3f",
              color: "#f1f3f9",
              borderRadius: "999px",
              padding: "0.25rem 0.75rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Memory ({memoryTurns.length})
          </button>

          {/* Settings / Tires Button */}
          <button
            type="button"
            onClick={() => setShowSettings(true)}
            style={{
              background: showSettings ? "#24c1e0" : "#191c26",
              color: showSettings ? "#090a0f" : "#f1f3f9",
              border: "1px solid #282c3f",
              borderRadius: "999px",
              padding: "0.25rem 0.75rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            ⚙️ {t["settings"] || "Settings"} ({activeProviders.llm === "StubLLM" ? "Mocks" : "Live"})
          </button>

          {/* Connection Status Pill */}
          <button
            type="button"
            onClick={() => {
              if (connected) {
                socket.close();
                setConnected(false);
              } else {
                socket.connect();
                setConnected(true);
              }
            }}
            data-testid="state-pill"
            style={{
              background: connected ? "rgba(0, 200, 83, 0.15)" : "rgba(255, 61, 0, 0.15)",
              color: connected ? "#00c853" : "#ff3d00",
              border: `1px solid ${connected ? "#00c853" : "#ff3d00"}`,
              borderRadius: "999px",
              padding: "0.25rem 0.75rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {connected ? state.toUpperCase() : "DISCONNECTED"}
          </button>
        </div>
      </header>

      {/* Main Stage */}
      <main style={{ width: "100%", maxWidth: 680, display: "flex", flexDirection: "column", alignItems: "center" }}>
        {/* Persona Studio Drawer / Panel */}
        {showStudio && (
          <div style={{ width: "100%" }}>
            <PersonaStudio
              personas={personas}
              currentPersona={currentPersona}
              onSelectPersona={handleSelectPersona}
              voices={voices}
              currentVoice={voice}
              onSelectVoice={setVoice}
              speed={speed}
              onSpeedChange={setSpeed}
              systemPrompt={systemPrompt}
              onSystemPromptChange={setSystemPrompt}
              onSavePersona={handleSavePersona}
              onDeletePersona={handleDeletePersona}
              onPreviewVoice={handlePreviewVoice}
              t={t}
            />
          </div>
        )}

        {/* Central Acoustic Orb */}
        <AcousticOrb state={state} analyser={analyser} talking={talking} size={240} />

        {/* State Caption & Spoken Subtitle */}
        <div style={{ minHeight: "4.5rem", textAlign: "center", margin: "0.5rem 0 1rem" }}>
          <div
            style={{
              fontSize: "0.85rem",
              fontWeight: 600,
              color: state === "listening" ? "#00c853" : state === "thinking" ? "#24c1e0" : state === "speaking" ? "#ffb300" : "#636c84",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              marginBottom: "0.35rem",
            }}
          >
            {state === "listening" ? t["listening"] : state === "thinking" ? t["thinking"] : state === "speaking" ? t["speaking"] : "IDLE"}
          </div>
          <div
            style={{
              fontSize: "1.05rem",
              color: "#f1f3f9",
              maxWidth: 540,
              lineHeight: 1.4,
              fontStyle: activeSentence ? "normal" : "italic",
            }}
          >
            {activeSentence || (state === "listening" ? "Say something or ask a question..." : "Hold space or push to talk")}
          </div>
        </div>

        {/* Main Interaction Controls */}
        <div style={{ display: "flex", gap: "1rem", alignItems: "center", marginBottom: "1.5rem" }}>
          {/* Push to Talk Primary Action */}
          <button
            type="button"
            data-testid="ptt-button"
            onMouseDown={startTalking}
            onMouseUp={stopTalking}
            onTouchStart={startTalking}
            onTouchEnd={stopTalking}
            style={{
              background: talking ? "#00c853" : "linear-gradient(135deg, #4285f4, #24c1e0)",
              color: talking ? "#090a0f" : "#ffffff",
              border: "none",
              borderRadius: "999px",
              padding: "0.85rem 2rem",
              fontSize: "1rem",
              fontWeight: 600,
              cursor: "pointer",
              boxShadow: talking ? "0 0 24px rgba(0,200,83,0.5)" : "0 4px 16px rgba(66,133,244,0.4)",
              transition: "transform 0.1s, box-shadow 0.1s",
              userSelect: "none",
            }}
          >
            {talking ? "Listening…" : t["push-to-talk"]}
          </button>

          {/* Barge In Button (Always available or highlights during speech) */}
          <button
            type="button"
            data-testid="barge-button"
            onClick={handleBarge}
            style={{
              background: state === "speaking" ? "#ff3d00" : "#191c26",
              color: state === "speaking" ? "#ffffff" : "#ff3d00",
              border: "1px solid #ff3d00",
              borderRadius: "999px",
              padding: "0.85rem 1.5rem",
              fontSize: "0.9rem",
              fontWeight: 600,
              cursor: "pointer",
              boxShadow: state === "speaking" ? "0 0 16px rgba(255,61,0,0.5)" : "none",
            }}
          >
            {t["barge"]}
          </button>
        </div>

        {/* Mode & Mute Switcher Bar */}
        <div style={{ display: "flex", gap: "0.75rem", marginBottom: "1rem" }}>
          <button
            type="button"
            onClick={() => setInputMode((m) => (m === "ptt" ? "handsfree" : "ptt"))}
            style={{
              background: inputMode === "handsfree" ? "rgba(36, 193, 224, 0.2)" : "#191c26",
              border: `1px solid ${inputMode === "handsfree" ? "#24c1e0" : "#282c3f"}`,
              color: inputMode === "handsfree" ? "#24c1e0" : "#9ba3b8",
              borderRadius: "8px",
              padding: "0.4rem 0.85rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {inputMode === "handsfree" ? t["hands-free"] || "Hands-Free (VAD)" : t["push-to-talk-mode"] || "Push-to-Talk"}
          </button>

          <button
            type="button"
            onClick={() => setMuted((m) => !m)}
            style={{
              background: muted ? "rgba(255, 61, 0, 0.2)" : "#191c26",
              border: `1px solid ${muted ? "#ff3d00" : "#282c3f"}`,
              color: muted ? "#ff3d00" : "#9ba3b8",
              borderRadius: "8px",
              padding: "0.4rem 0.85rem",
              fontSize: "0.75rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {muted ? `🔇 ${t["muted"]}` : "🔊 Unmuted"}
          </button>

          <button
            type="button"
            onClick={() => setShowTelemetry((s) => !s)}
            style={{
              background: "#191c26",
              border: "1px solid #282c3f",
              color: "#9ba3b8",
              borderRadius: "8px",
              padding: "0.4rem 0.85rem",
              fontSize: "0.75rem",
              cursor: "pointer",
            }}
          >
            {t["telemetry"] || "Telemetry"}
          </button>
        </div>

        {/* Telemetry Waterfall HUD */}
        {showTelemetry && (
          <div style={{ width: "100%" }}>
            <TelemetryHud
              state={state}
              metrics={metrics}
              queueLength={queueLength}
              connected={connected}
            />
          </div>
        )}

        {/* Dialogue Transcript Stream */}
        <section
          aria-label="Transcript"
          style={{
            width: "100%",
            background: "#12141c",
            border: "1px solid #282c3f",
            borderRadius: "16px",
            padding: "1.25rem",
            marginTop: "1rem",
            boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "1rem",
              paddingBottom: "0.5rem",
              borderBottom: "1px solid #282c3f",
            }}
          >
            <span style={{ fontSize: "0.85rem", fontWeight: 600, color: "#f1f3f9" }}>
              Live Dialogue Stream
            </span>
            <span style={{ fontSize: "0.75rem", color: "#636c84" }}>
              Persona: {currentPersona.toUpperCase()} ({voice})
            </span>
          </div>

          <div
            data-testid="transcript"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.75rem",
              maxHeight: 280,
              overflowY: "auto",
            }}
          >
            {lines.length === 0 ? (
              <div style={{ color: "#636c84", fontSize: "0.85rem", textAlign: "center", padding: "1.5rem" }}>
                Hold spacebar to talk. Transcripts and sentence audio will stream live here.
              </div>
            ) : (
              lines.map((line) => (
                <div
                  key={line.id}
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignSelf: line.who === "user" ? "flex-end" : "flex-start",
                    maxWidth: "80%",
                    background: line.who === "user" ? "rgba(66, 133, 244, 0.15)" : "#191c26",
                    border: `1px solid ${line.who === "user" ? "rgba(66, 133, 244, 0.3)" : "#282c3f"}`,
                    borderRadius: "12px",
                    padding: "0.6rem 0.85rem",
                    fontSize: "0.85rem",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: "0.7rem",
                      color: line.who === "user" ? "#4285f4" : "#ffb300",
                      marginBottom: "0.25rem",
                      gap: "1rem",
                    }}
                  >
                    <span style={{ fontWeight: 600 }}>{line.who === "user" ? "You" : currentPersona.toUpperCase()}</span>
                    <span style={{ color: "#636c84" }}>{line.time}</span>
                  </div>
                  <div style={{ color: "#f1f3f9", lineHeight: 1.4 }}>{line.text}</div>
                  {line.audio_url && (
                    <div style={{ marginTop: "0.35rem" }}>
                      <button
                        type="button"
                        onClick={() => {
                          const a = new Audio(line.audio_url);
                          a.playbackRate = speedRef.current;
                          a.play();
                        }}
                        style={{
                          background: "none",
                          border: "none",
                          color: "#24c1e0",
                          fontSize: "0.7rem",
                          cursor: "pointer",
                          padding: 0,
                        }}
                      >
                        ▶ Replay
                      </button>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </section>

        {/* Google Labs Styled Prompt Composer & Dictation Dock */}
        <PromptComposer
          onSend={handleSendPrompt}
          disabled={!connected}
          connected={connected}
          serverUrl={httpBaseFromWs(WS_URL)}
          t={t}
        />
      </main>

      {/* Persistent Hippocampus Memory Drawer */}
      <MemoryDrawer
        isOpen={showMemory}
        onClose={() => setShowMemory(false)}
        turns={memoryTurns}
        onClearMemory={handleClearMemory}
        t={t}
      />

      {/* Settings / Swappable Tires Modal */}
      <SettingsModal
        isOpen={showSettings}
        onClose={() => setShowSettings(false)}
        settings={settings}
        activeProviders={activeProviders}
        onApplySettings={handleApplySettings}
        t={t}
      />

      {/* Hidden audio element for gapless streaming playback */}
      <audio ref={audioRef} onEnded={onAudioEnded} preload="auto" />
    </div>
  );
}
