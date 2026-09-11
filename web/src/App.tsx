import { useCallback, useEffect, useRef, useState } from "react";
import {
  AgentState,
  PersonaId,
  ServerFrame,
  float32ToBase64Pcm16,
  newTurnId,
  socket,
} from "./ws";
import en from "./i18n/en.json";
import hi from "./i18n/hi.json";

type Strings = typeof en;
const STRINGS: Record<"en" | "hi", Strings> = { en, hi };

// TODO(backend): GET /voices -> [{ id, display_name, lang }] (see voices.yaml).
// Static fallback until the duplex server exposes it.
const STATIC_VOICES = [
  { id: "af_heart", display_name: "Heart (EN)", lang: "en" },
  { id: "shannon", display_name: "Shannon", lang: "en" },
  { id: "archie", display_name: "Archie", lang: "en" },
];

interface TranscriptLine {
  id: string;
  who: "user" | "agent";
  text: string;
}

interface QueuedSentence {
  index: number;
  text: string;
  audio_url: string;
}

function httpBaseFromWs(wsUrl: string): string {
  return wsUrl.replace(/^ws/, "http").replace(/\/ws\/?$/, "");
}

export default function App() {
  const [lang, setLang] = useState<"en" | "hi">("en");
  const t = STRINGS[lang];
  const [connected, setConnected] = useState(false);
  const [state, setState] = useState<AgentState>("idle");
  const [persona, setPersona] = useState<PersonaId>("donna");
  const [voice, setVoice] = useState(STATIC_VOICES[0].id);
  const [voices, setVoices] = useState(STATIC_VOICES);
  const [speed, setSpeed] = useState(1.0);
  const [muted, setMuted] = useState(false);
  const [talking, setTalking] = useState(false);
  const [lines, setLines] = useState<TranscriptLine[]>([]);

  const turnRef = useRef<string>(newTurnId());
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<QueuedSentence[]>([]);
  const playingRef = useRef(false);
  const prefetchRef = useRef<HTMLAudioElement | null>(null);
  const micRef = useRef<{
    stream: MediaStream;
    ctx: AudioContext;
    proc: ScriptProcessorNode;
    src: MediaStreamAudioSourceNode;
  } | null>(null);
  // Refs mirrored for use inside stable callbacks.
  const personaRef = useRef(persona);
  personaRef.current = persona;
  const voiceRef = useRef(voice);
  voiceRef.current = voice;
  const speedRef = useRef(speed);
  speedRef.current = speed;
  const mutedRef = useRef(muted);
  mutedRef.current = muted;

  const pushLine = useCallback((who: "user" | "agent", text: string) => {
    setLines((prev) => [
      ...prev,
      { id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, who, text },
    ]);
  }, []);

  // --- Playback queue: play sentence N while prefetching N+1 (cf. say.sh) ---
  const pumpQueue = useCallback(() => {
    const el = audioRef.current;
    if (!el || playingRef.current) return;
    const next = queueRef.current.shift();
    if (!next) return;
    playingRef.current = true;
    el.src = next.audio_url;
    el.playbackRate = speedRef.current;
    void el.play().catch(() => {
      playingRef.current = false;
    });
    // Prefetch N+1 while N plays.
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
    playingRef.current = false;
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
    turnRef.current = newTurnId();
  }, [killPlayback]);

  // --- Mic: getUserMedia + ScriptProcessor 128-sample chunks -> user.start ---
  const startTalking = useCallback(async () => {
    try {
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
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      const ctx = new Ctx();
      const src = ctx.createMediaStreamSource(stream);
      // 128-sample chunks per spec; mono input / mono output.
      const proc = ctx.createScriptProcessor(128, 1, 1);
      proc.onaudioprocess = (ev: AudioProcessingEvent) => {
        const samples = ev.inputBuffer.getChannelData(0).slice(0, 128);
        socket.send({
          type: "user.start",
          turn_id: turnRef.current,
          chunk: float32ToBase64Pcm16(samples),
          persona: personaRef.current,
          voice: voiceRef.current,
          speed: speedRef.current,
        });
      };
      src.connect(proc);
      // Mute monitor to prevent speaker feedback loop; zero-gain keeps proc running
      const zeroGain = ctx.createGain();
      zeroGain.gain.value = 0;
      proc.connect(zeroGain);
      zeroGain.connect(ctx.destination);
      micRef.current = { stream, ctx, proc, src };
      setTalking(true);
    } catch {
      // Mic denied/unavailable: stay idle, no crash.
      setTalking(false);
    }
  }, []);

  const stopTalking = useCallback(() => {
    const mic = micRef.current;
    micRef.current = null;
    if (mic) {
      mic.proc.disconnect();
      mic.src.disconnect();
      void mic.ctx.close();
      mic.stream.getTracks().forEach((tr) => tr.stop());
    }
    socket.send({ type: "user.stop", turn_id: turnRef.current });
    setTalking(false);
  }, []);

  // --- Server frames ---
  useEffect(() => {
    const off = socket.onFrame((frame: ServerFrame) => {
      switch (frame.type) {
        case "state.idle":
        case "state.listening":
        case "state.thinking":
        case "state.speaking":
          setState(frame.type.slice("state.".length) as AgentState);
          break;
        case "transcript.user":
          pushLine("user", frame.text);
          break;
        case "agent.stall":
          // Stall plays immediately, ahead of the sentence queue.
          if (frame.audio_url && !mutedRef.current && audioRef.current) {
            queueRef.current.unshift({
              index: -1,
              text: "",
              audio_url: frame.audio_url,
            });
            pumpQueue();
          }
          break;
        case "agent.sentence":
          pushLine("agent", frame.text);
          if (!mutedRef.current) {
            queueRef.current.push({
              index: frame.index,
              text: frame.text,
              audio_url: frame.audio_url,
            });
            // Keep queue ordered by sentence index (worker may stream fast).
            queueRef.current.sort((a, b) => a.index - b.index);
            pumpQueue();
          }
          break;
        case "agent.done":
          turnRef.current = newTurnId();
          break;
      }
    });
    return off;
  }, [pushLine, pumpQueue]);

  // --- Voices: try backend, keep static fallback ---
  useEffect(() => {
    let cancelled = false;
    const base = httpBaseFromWs(
      import.meta.env.VITE_WS_URL ?? "ws://127.0.0.1:8089",
    );
    fetch(`${base}/voices`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("no /voices"))))
      .then((data: unknown) => {
        if (cancelled || !Array.isArray(data) || data.length === 0) return;
        const list = (data as Array<Partial<(typeof STATIC_VOICES)[number]>>)
          .filter((v) => typeof v.id === "string")
          .map((v) => ({
            id: v.id as string,
            display_name: v.display_name ?? (v.id as string),
            lang: v.lang ?? "en",
          }));
        if (list.length > 0) {
          setVoices(list);
          setVoice((cur) =>
            list.some((v) => v.id === cur) ? cur : list[0].id,
          );
        }
      })
      .catch(() => {
        // Static fallback stands.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const toggleConnect = useCallback(() => {
    if (socket.connected) {
      socket.close();
      setConnected(false);
    } else {
      socket.connect();
      setConnected(true);
    }
  }, []);

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 640, margin: "2rem auto" }}>
      <h1>pet-talk</h1>

      <p>
        <span
          data-testid="state-pill"
          style={{
            padding: "0.25rem 0.75rem",
            borderRadius: 999,
            background:
              state === "idle"
                ? "#eee"
                : state === "listening"
                  ? "#cde"
                  : state === "thinking"
                    ? "#fda"
                    : "#cfc",
          }}
        >
          {state}
        </span>{" "}
        <button onClick={toggleConnect}>
          {t.connect}: {connected ? "✓" : "✗"}
        </button>{" "}
        <button onClick={() => setLang((l) => (l === "en" ? "hi" : "en"))}>
          {lang === "en" ? "हिंदी" : "English"}
        </button>
      </p>

      <p>
        <button
          onMouseDown={startTalking}
          onMouseUp={stopTalking}
          onMouseLeave={talking ? stopTalking : undefined}
          onTouchStart={startTalking}
          onTouchEnd={stopTalking}
          style={{ fontSize: "1.2rem", padding: "0.75rem 1.5rem" }}
        >
          🎙 {t["push-to-talk"]}
        </button>{" "}
        <button
          onClick={handleBarge}
          style={{ fontSize: "1.2rem", padding: "0.75rem 1.5rem" }}
        >
          ⏹ {t.barge}
        </button>{" "}
        <button onClick={() => setMuted((m) => !m)}>
          {muted ? `🔇 ${t.muted}` : "🔊"}
        </button>
      </p>
      {talking && <p>{t.listening}</p>}
      {state === "thinking" && <p>{t.thinking}</p>}
      {state === "speaking" && <p>{t.speaking}</p>}

      <p>
        <label>
          {t.persona}:{" "}
          <select
            value={persona}
            onChange={(e) => setPersona(e.target.value as PersonaId)}
          >
            <option value="donna">donna</option>
            <option value="zuck">zuck</option>
            <option value="jarvis">jarvis</option>
          </select>
        </label>{" "}
        <label>
          {t.voice}:{" "}
          <select value={voice} onChange={(e) => setVoice(e.target.value)}>
            {voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.display_name}
              </option>
            ))}
          </select>
        </label>{" "}
        <label>
          {t.speed}:{" "}
          <input
            type="range"
            min={0.5}
            max={2}
            step={0.1}
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value))}
          />{" "}
          {speed.toFixed(1)}x
        </label>
      </p>

      <audio ref={audioRef} onEnded={onAudioEnded} />

      <section>
        <h2>transcript</h2>
        <ul data-testid="transcript">
          {lines.map((l) => (
            <li key={l.id}>
              <strong>{l.who}:</strong> {l.text}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
