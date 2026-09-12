/**
 * The cockpit — a developer inspector, not the product.
 *
 * The product is the notch. This page exists so a developer can watch a turn
 * happen: the spoken stream with the receipt under each claim, the read-ahead
 * buffer, one latency bar per turn against qa/budgets.json, and behind a
 * Developer toggle, the raw frames and every number.
 *
 * Three laws this file is built around:
 *
 *   1. **Receipts over prose.** A spoken line is a claim; the chip under it is
 *      the proof. They render together (TranscriptStream + ReceiptChip).
 *   2. **The user surface says nothing about engineering.** No frame names, no
 *      millisecond numbers, no persona name — those mount only when
 *      `developer` is on. devSurface.test.tsx greps the rendered DOM for
 *      `agent.`, `ms` and `stall` to keep that honest.
 *   3. **Read ahead, skip forward.** Buffered sentences render dim before they
 *      are spoken; Right arrow moves the play head, Left arrow moves it back,
 *      Escape stops the turn. Skipping never drops a sentence (readAhead.ts).
 *
 * Colour comes only from tokens (styles/tokens.css + tokens.pet-talk.css) via
 * the classes in styles/app.css. No hex literal lives in this file, and
 * qa/test_design_tokens.py enforces that.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentState,
  PersonaId,
  ScreenGrounding,
  ServerFrame,
  WS_URL,
  fileToAttachment,
  float32ToBase64Pcm16,
  newAttachRef,
  newTurnId,
  socket,
  studioTokenHeader,
} from "./ws";
import en from "./i18n/en.json";
import hi from "./i18n/hi.json";
import { AcousticOrb } from "./components/AcousticOrb";
import { LatencyMetrics, TelemetryHud } from "./components/TelemetryHud";
import { PersonaData, PersonaStudio } from "./components/PersonaStudio";
import { MemoryDrawer, MemoryTurn } from "./components/MemoryDrawer";
import { ActiveProviders, RuntimeSettings, SettingsModal } from "./components/SettingsModal";
import { PromptComposer } from "./components/PromptComposer";
import { EyesAttachDock, EyesBlock, EyesEntry, ScreenGroundingLine } from "./components/EyesAttach";
import { ReceiptFrame, asReceiptFrame } from "./components/ReceiptChip";
import { ChunkPlayer } from "./audio/ChunkPlayer";
import { StreamLine, TranscriptStream } from "./components/TranscriptStream";
import { LatencyBar } from "./components/LatencyBar";
import { DeveloperRail, LoggedFrame } from "./components/DeveloperRail";
import { ThemeToggle, useTheme } from "./components/ThemeToggle";
import { ArchieGlyph, ArchieGlyphState } from "./components/ArchieGlyph";
import { EMPTY_TIMING, TurnTiming } from "./latency";
import {
  EMPTY_READ_AHEAD,
  ReadAheadState,
  advance,
  bufferedAhead,
  insertSentence,
  skipBack,
  skipForward,
  startIfIdle,
} from "./readAhead";

type Strings = typeof en;
const STRINGS: Record<"en" | "hi", Strings> = { en, hi };

const DEVELOPER_STORAGE_KEY = "pet_talk_developer";
/** How many frames the inspector keeps. Older ones are gone, not summarised. */
const FRAME_LOG_CAP = 200;

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

/** A transcript entry. Agent sentences carry `seq` so the play head can find them. */
interface TranscriptEntry {
  id: string;
  who: "user" | "agent" | "thinking" | "eyes" | "notice";
  text: string;
  seq?: number;
  frame?: string;
  time: string;
  eyesRef?: string;
  receipt?: ReceiptFrame;
}

function clockNow(): string {
  return new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
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

function readDeveloperFlag(): boolean {
  try {
    return localStorage.getItem(DEVELOPER_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/** Archie's light is the state: idle steady, listening bright, speaking beat.
 *  Lane 5a owns the glyph; the cockpit only tells it which state it is in. */
function archieState(state: AgentState, offline: boolean): ArchieGlyphState {
  if (offline) return "error";
  if (state === "speaking") return "speaking";
  if (state === "listening") return "listening";
  return "idle";
}

const STATE_WORD: Record<AgentState, string> = {
  idle: "ready",
  listening: "listening",
  thinking: "working",
  speaking: "speaking",
};

export default function App() {
  const [lang, setLang] = useState<"en" | "hi">("en");
  const t = STRINGS[lang];

  const [theme, setTheme] = useTheme();
  const [developer, setDeveloper] = useState<boolean>(readDeveloperFlag);
  useEffect(() => {
    try {
      localStorage.setItem(DEVELOPER_STORAGE_KEY, developer ? "1" : "0");
    } catch {
      // Not persisted; the toggle still holds for this page-load.
    }
  }, [developer]);

  // Core connection & state
  const [connected, setConnected] = useState(false);
  const [authError, setAuthError] = useState(false);
  const [state, setState] = useState<AgentState>("idle");

  // Persona & voice
  const [personas, setPersonas] = useState<PersonaData[]>(DEFAULT_PERSONAS);
  const [currentPersona, setCurrentPersona] = useState<PersonaId>("donna");
  const [voice, setVoice] = useState(STATIC_VOICES[0].id);
  const [voices, setVoices] = useState(STATIC_VOICES);
  const [speed, setSpeed] = useState(1.05);
  const [systemPrompt, setSystemPrompt] = useState("");

  // Mode & audio
  const [inputMode, setInputMode] = useState<"ptt" | "handsfree">("ptt");
  const [muted, setMuted] = useState(false);
  const [talking, setTalking] = useState(false);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);

  // Sheets
  const [showStudio, setShowStudio] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  // Runtime settings & swappable tyres
  const [settings, setSettings] = useState<RuntimeSettings>({
    stt_provider: "stub",
    llm_provider: "stub",
    llm_base_url: "",
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
  const [secretsSet, setSecretsSet] = useState<Record<string, boolean>>({});

  // Measurement. `timing` is this turn's; `metrics` feeds the older HUD.
  const [timing, setTiming] = useState<TurnTiming>(EMPTY_TIMING);
  const [timingTurn, setTimingTurn] = useState<string>("");
  const [metrics, setMetrics] = useState<LatencyMetrics>({
    vadMs: 0,
    sttMs: 0,
    stallMs: 0,
    ttftMs: 0,
    ttsMs: 0,
    e2eMs: 0,
    provenance: "idle",
  });
  const [frames, setFrames] = useState<LoggedFrame[]>([]);

  // Transcript, buffer, memory, eyes
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [buffer, setBuffer] = useState<ReadAheadState>(EMPTY_READ_AHEAD);
  const [memoryTurns, setMemoryTurns] = useState<MemoryTurn[]>([]);
  const [eyesMap, setEyesMap] = useState<Record<string, EyesEntry>>({});
  const [eyesNotice, setEyesNotice] = useState("");
  const [screenGround, setScreenGround] = useState<ScreenGrounding | null>(null);
  const [lastReceipt, setLastReceipt] = useState<ReceiptFrame | null>(null);

  // Refs
  const turnRef = useRef<string>(newTurnId());
  const turnStartTimeRef = useRef<number>(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const bufferRef = useRef<ReadAheadState>(EMPTY_READ_AHEAD);
  bufferRef.current = buffer;
  const chunkPlayerRef = useRef<ChunkPlayer | null>(null);
  const getChunkPlayer = useCallback((): ChunkPlayer => {
    if (!chunkPlayerRef.current) {
      chunkPlayerRef.current = new ChunkPlayer({
        onError: (reason, detail) => console.warn(`[pet-talk] chunk player: ${reason} ${detail}`),
      });
    }
    return chunkPlayerRef.current;
  }, []);
  const micRef = useRef<{
    stream: MediaStream;
    ctx: AudioContext;
    proc: ScriptProcessorNode;
    src: MediaStreamAudioSourceNode;
    analyser: AnalyserNode;
  } | null>(null);
  const speechDetectedRef = useRef(false);
  const silenceTimerRef = useRef<number | null>(null);
  const recordedAudioRef = useRef<Float32Array[]>([]);

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

  const logFrame = useCallback(
    (direction: "in" | "out", name: string, payload: unknown) => {
      setFrames((prev) => {
        const next = [
          ...prev,
          {
            id: `${name}-${prev.length}-${Date.now()}`,
            clock: clockNow(),
            direction,
            name,
            payload,
          },
        ];
        return next.length > FRAME_LOG_CAP ? next.slice(next.length - FRAME_LOG_CAP) : next;
      });
    },
    [],
  );

  const pushEntry = useCallback((entry: Omit<TranscriptEntry, "id" | "time">) => {
    setEntries((prev) => [
      ...prev,
      { ...entry, id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, time: clockNow() },
    ]);
  }, []);

  /**
   * Attach proof to the claim it proves: the newest agent line whose text is
   * the receipt's claim. A receipt whose claim never reached the transcript
   * speaks for itself on its own line rather than being dropped — an unshown
   * receipt is the failure mode this whole surface exists to stop.
   */
  const attachReceipt = useCallback((receipt: ReceiptFrame) => {
    setLastReceipt(receipt);
    setEntries((prev) => {
      for (let i = prev.length - 1; i >= 0; i -= 1) {
        const line = prev[i];
        if (line.who === "agent" && !line.receipt && line.text === receipt.claim) {
          const next = [...prev];
          next[i] = { ...line, receipt };
          return next;
        }
      }
      return [
        ...prev,
        {
          id: `receipt-${receipt.turn_id}-${prev.length}`,
          who: "agent",
          text: receipt.claim,
          time: clockNow(),
          receipt,
        },
      ];
    });
  }, []);

  const patchEyes = useCallback((ref: string, patch: Partial<EyesEntry>) => {
    setEyesMap((prev) => (prev[ref] ? { ...prev, [ref]: { ...prev[ref], ...patch } } : prev));
  }, []);

  // --- Playback, driven by the play head ------------------------------------

  /** Point the audio element at the sentence the head is on and play it. */
  const playHead = useCallback((next: ReadAheadState) => {
    const el = audioRef.current;
    const sentence = next.sentences[next.head];
    if (!el || !sentence || mutedRef.current) return;
    if (!sentence.audioUrl) return;
    el.src = sentence.audioUrl;
    el.playbackRate = speedRef.current;
    void el.play().catch(() => undefined);
  }, []);

  const onAudioEnded = useCallback(() => {
    setBuffer((prev) => {
      const next = advance(prev);
      if (next.sentences[next.head]) playHead(next);
      return next;
    });
  }, [playHead]);

  const killPlayback = useCallback(() => {
    const el = audioRef.current;
    if (el) {
      el.pause();
      el.removeAttribute("src");
      el.load();
    }
    chunkPlayerRef.current?.stop();
  }, []);

  /**
   * Skip a sentence. The head moves; the buffer does not shrink.
   *
   * The wire protocol has no `resume_from` on `barge` (see ws.ts ClientFrame),
   * so a skip is entirely local: stop what is sounding — including chunked
   * audio, which the chunk player can only stop wholesale — and play the
   * sentence the head landed on from its whole-sentence `audio_url`. The
   * server keeps streaming; nothing is cancelled and nothing is lost.
   */
  const skip = useCallback(
    (direction: 1 | -1) => {
      setBuffer((prev) => {
        const next = direction === 1 ? skipForward(prev) : skipBack(prev);
        if (next === prev) return prev;
        chunkPlayerRef.current?.stop();
        playHead(next);
        return next;
      });
    },
    [playHead],
  );

  const handleBarge = useCallback(() => {
    socket.send({ type: "barge", turn_id: turnRef.current });
    logFrame("out", "barge", { type: "barge", turn_id: turnRef.current });
    killPlayback();
    setBuffer(EMPTY_READ_AHEAD);
    setState("listening");
    turnRef.current = newTurnId();
  }, [killPlayback, logFrame]);

  /** Read the file locally, show it in the transcript, then send user.attach. */
  const handleAttach = useCallback(
    async (file: File | Blob, hint?: "screenshot") => {
      setEyesNotice("");
      const ref = newAttachRef();
      try {
        const att = await fileToAttachment(file, ref, hint);
        setEyesMap((prev) => ({
          ...prev,
          [ref]: {
            ref,
            kind: att.kind,
            filename: att.filename,
            bytes: att.bytes,
            status: "sent",
            time: clockNow(),
          },
        }));
        pushEntry({ who: "eyes", text: "", eyesRef: ref, frame: "user.attach" });
        socket.send({
          type: "user.attach",
          turn_id: turnRef.current,
          ref,
          kind: att.kind,
          mime: att.mime,
          b64: att.b64,
          filename: att.filename,
        });
        logFrame("out", "user.attach", { ref, kind: att.kind, bytes: att.bytes });
      } catch (e) {
        // Same reason vocabulary as the server, so one UI covers both.
        setEyesNotice((e as Error)?.message || "eyes_bad_kind");
      }
    },
    [pushEntry, logFrame],
  );

  const handleSendPrompt = useCallback(
    (promptText: string) => {
      if (!promptText.trim()) return;
      const turnId = newTurnId();
      turnRef.current = turnId;
      turnStartTimeRef.current = performance.now();
      setTiming(EMPTY_TIMING);
      setBuffer(EMPTY_READ_AHEAD);
      setState("thinking");
      const frame = {
        type: "user.text" as const,
        turn_id: turnId,
        text: promptText.trim(),
        persona: personaRef.current,
        voice: voiceRef.current,
        speed: speedRef.current,
        system_prompt: promptRef.current,
      };
      socket.send(frame);
      logFrame("out", "user.text", { ...frame, text: frame.text });
    },
    [logFrame],
  );

  // --- Mic engine -----------------------------------------------------------
  const stopTalking = useCallback(() => {
    if (!micRef.current) return;
    setTalking(false);
    turnStartTimeRef.current = performance.now();

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

    try {
      micRef.current.stream.getTracks().forEach((tr) => tr.stop());
      void micRef.current.ctx.close();
    } catch {
      // Already closed.
    }
    micRef.current = null;
    setAnalyser(null);

    socket.send({
      type: "user.stop",
      turn_id: turnRef.current,
      pcm_b64: pcmB64,
      sample_rate: 16000,
    });
    logFrame("out", "user.stop", { turn_id: turnRef.current, pcm_bytes: pcmB64.length });
    setTiming(EMPTY_TIMING);
    setBuffer(EMPTY_READ_AHEAD);
  }, [logFrame]);

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

      const ana = ctx.createAnalyser();
      ana.fftSize = 64;
      src.connect(ana);
      setAnalyser(ana);

      socket.send({
        type: "user.start",
        turn_id: turnRef.current,
        persona: personaRef.current,
        voice: voiceRef.current,
        speed: speedRef.current,
        system_prompt: promptRef.current,
      });
      logFrame("out", "user.start", { turn_id: turnRef.current });

      const proc = ctx.createScriptProcessor(512, 1, 1);
      proc.onaudioprocess = (ev: AudioProcessingEvent) => {
        const input = ev.inputBuffer.getChannelData(0);
        const samples = new Float32Array(input.length);
        samples.set(input);
        recordedAudioRef.current.push(samples);

        let sum = 0;
        for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
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
            silenceTimerRef.current = window.setTimeout(() => {
              speechDetectedRef.current = false;
              silenceTimerRef.current = null;
              stopTalking();
            }, settings.vad_silence_ms || 600);
          }
        }

        socket.send({
          type: "user.chunk",
          turn_id: turnRef.current,
          chunk: float32ToBase64Pcm16(samples),
        });
      };

      src.connect(proc);
      // Zero-gain monitor keeps the ScriptProcessor alive without feedback.
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
  }, [inputMode, logFrame, settings.vad_silence_ms, stopTalking]);

  // --- Socket ---------------------------------------------------------------
  useEffect(() => {
    socket.connect(WS_URL);
    setConnected(true);

    const offAuth = socket.onAuthError(() => {
      setConnected(false);
      setAuthError(true);
    });

    const off = socket.onFrame((frame: ServerFrame) => {
      const now = performance.now();
      const elapsed = turnStartTimeRef.current > 0 ? Math.round(now - turnStartTimeRef.current) : 0;
      const kind = (frame as { type?: string }).type ?? "unknown";
      logFrame("in", kind, frame);

      const receipt = asReceiptFrame(frame);
      if (receipt) {
        attachReceipt(receipt);
        return;
      }

      /** First playable audio of the turn, whichever frame carries it. */
      const markFirstAudio = () => {
        setTiming((prev) =>
          prev.firstAudioMs === null && elapsed > 0
            ? { ...prev, firstAudioMs: elapsed }
            : prev,
        );
        setTimingTurn(frame.turn_id);
      };

      switch (frame.type) {
        case "state.idle":
          setState("idle");
          break;
        case "state.listening":
          setState("listening");
          chunkPlayerRef.current?.stop();
          break;
        case "state.thinking":
          setState("thinking");
          setScreenGround(frame.screen ?? null);
          break;
        case "state.speaking":
          setState("speaking");
          break;
        case "transcript.user":
          pushEntry({ who: "user", text: frame.text, frame: "transcript.user" });
          setMetrics((m) => ({
            ...m,
            sttMs: elapsed > 0 ? elapsed : m.sttMs,
            provenance: "flown",
          }));
          break;
        case "agent.stall":
          setTiming((prev) =>
            prev.stallMs === null && elapsed > 0 ? { ...prev, stallMs: elapsed } : prev,
          );
          setTimingTurn(frame.turn_id);
          setMetrics((m) => ({
            ...m,
            stallMs: elapsed > 0 ? elapsed : m.stallMs,
            provenance: "flown",
          }));
          if (frame.text) {
            pushEntry({ who: "thinking", text: frame.text, frame: "agent.stall" });
          }
          if (frame.audio_url && !mutedRef.current && audioRef.current) {
            // The stall is not part of the sentence buffer — it is not a claim
            // and it must never be skippable or re-orderable. It plays direct.
            const el = audioRef.current;
            el.src = resolveAudioUrl(frame.audio_url);
            el.playbackRate = speedRef.current;
            void el.play().catch(() => undefined);
            markFirstAudio();
          }
          break;
        case "agent.sentence": {
          const seq = frame.seq ?? frame.index ?? 0;
          pushEntry({ who: "agent", text: frame.text, seq, frame: "agent.sentence" });
          setBuffer((prev) => {
            let next = insertSentence(prev, {
              seq,
              text: frame.text,
              audioUrl: resolveAudioUrl(frame.audio_url),
              chunked: Boolean(frame.chunked),
            });
            const wasIdle = prev.head < 0 || prev.head >= prev.sentences.length;
            next = startIfIdle(next);
            // A chunked sentence is already sounding through ChunkPlayer, so the
            // head only tracks it; an unchunked one needs the audio element.
            if (wasIdle && !frame.chunked) playHead(next);
            return next;
          });
          if (!frame.chunked) markFirstAudio();
          setMetrics((m) => ({
            ...m,
            ttftMs: m.ttftMs === 0 && elapsed > 0 ? elapsed : m.ttftMs,
            provenance: "flown",
          }));
          break;
        }
        case "agent.chunk":
          if (!mutedRef.current) {
            void getChunkPlayer().enqueue({
              seq: frame.seq,
              chunk_no: frame.chunk_no,
              audio_b64: frame.audio_b64,
              final: frame.final,
            });
          }
          if (frame.chunk_no === 0) markFirstAudio();
          break;
        case "handover.received":
          pushEntry({
            who: "notice",
            text: `Handed over from ${frame.source}.`,
            frame: "handover.received",
          });
          break;
        case "agent.done":
          setMetrics((m) => ({
            ...m,
            e2eMs: elapsed > 0 ? elapsed : m.e2eMs,
            provenance: "flown",
          }));
          setTimingTurn(frame.turn_id);
          turnRef.current = newTurnId();
          fetch(`${httpBaseFromWs(WS_URL)}/ledger`)
            .then((r) => r.json())
            .then((d) => {
              if (d && Array.isArray(d.turns)) setMemoryTurns(d.turns);
            })
            .catch(() => undefined);
          break;
        case "eyes.received":
          patchEyes(frame.ref, {
            status: "received",
            task: frame.task,
            kind: frame.kind ?? "image",
            bytes: frame.bytes,
          });
          break;
        case "eyes.text":
          patchEyes(frame.ref, {
            status: "text",
            text: frame.text,
            truncated: Boolean(frame.truncated),
            task: frame.task,
            engine: frame.engine,
            source: frame.source,
          });
          break;
        case "agent.error":
          console.warn("[pet-talk] Agent error frame:", frame.reason);
          if (frame.ref) {
            patchEyes(frame.ref, {
              status: "error",
              reason: frame.reason,
              detail: frame.detail,
            });
          } else {
            // An error is the one moment that waits for the user, so it lands
            // in the stream in plain words instead of only in the console.
            pushEntry({
              who: "notice",
              text: `Could not finish that: ${frame.reason}.`,
              frame: "agent.error",
            });
          }
          break;
      }
    });

    return () => {
      off();
      offAuth();
      socket.close();
    };
  }, [pushEntry, patchEyes, attachReceipt, getChunkPlayer, logFrame, playHead]);

  // --- Server state on load -------------------------------------------------
  useEffect(() => {
    const base = httpBaseFromWs(WS_URL);

    fetch(`${base}/voices`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: unknown) => {
        if (!Array.isArray(data) || data.length === 0) return;
        setVoices(
          data.map((v) => ({
            id: v.id as string,
            display_name: v.display_name ?? (v.id as string),
            lang: v.lang ?? "en",
          })),
        );
      })
      .catch(() => undefined);

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
      .catch(() => undefined);

    fetch(`${base}/ledger`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data: unknown) => {
        if (data && Array.isArray((data as { turns: MemoryTurn[] }).turns)) {
          setMemoryTurns((data as { turns: MemoryTurn[] }).turns);
        }
      })
      .catch(() => undefined);

    fetch(`${base}/settings`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        if (d && d.settings) setSettings(d.settings);
        if (d && d.active) setActiveProviders(d.active);
        if (d && d.secrets_set) setSecretsSet(d.secrets_set);
      })
      .catch(() => undefined);
  }, [currentPersona]);

  const handleApplySettings = async (
    newSettings: Partial<RuntimeSettings> & { deepgram_api_key?: string; llm_api_key?: string },
  ) => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...studioTokenHeader() },
      body: JSON.stringify(newSettings),
    });
    if (res.status === 401) {
      setAuthError(true);
      return;
    }
    if (res.ok) {
      const data = await res.json();
      if (data.settings) setSettings(data.settings);
      if (data.active) setActiveProviders(data.active);
      if (data.secrets_set) setSecretsSet(data.secrets_set);
    }
  };

  const handleSelectPersona = (name: string) => {
    setCurrentPersona(name);
    const p = personas.find((item) => item.name === name);
    if (p) {
      setVoice(p.voice);
      setSpeed(p.speed);
      setSystemPrompt(p.tone);
    }
  };

  const handleSavePersona = async (personaData: PersonaData) => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/personas`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...studioTokenHeader() },
      body: JSON.stringify(personaData),
    });
    if (res.status === 401) {
      setAuthError(true);
      return;
    }
    if (res.ok) {
      const refreshed = await fetch(`${base}/personas`).then((r) => r.json());
      setPersonas(refreshed);
      handleSelectPersona(personaData.name);
    }
  };

  const handleDeletePersona = async (name: string) => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/personas/${name}`, {
      method: "DELETE",
      headers: { ...studioTokenHeader() },
    });
    if (res.status === 401) {
      setAuthError(true);
      return;
    }
    if (res.ok) {
      const refreshed = await fetch(`${base}/personas`).then((r) => r.json());
      setPersonas(refreshed);
      handleSelectPersona(refreshed[0]?.name ?? "donna");
    }
  };

  const handleClearMemory = async () => {
    const base = httpBaseFromWs(WS_URL);
    const res = await fetch(`${base}/ledger`, {
      method: "DELETE",
      headers: { ...studioTokenHeader() },
    });
    if (res.status === 401) {
      setAuthError(true);
      return;
    }
    if (res.ok) setMemoryTurns([]);
  };

  const handlePreviewVoice = (voiceId: string) => {
    const base = httpBaseFromWs(WS_URL);
    const audio = new Audio(`${base}/audio/preview-${encodeURIComponent(voiceId)}`);
    audio.play().catch(() => undefined);
  };

  // --- Keyboard -------------------------------------------------------------
  // Space holds to talk (tap barges while speaking); Right/Left skip inside the
  // read-ahead buffer; Escape stops the turn; M mutes.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.code === "Space" && !e.repeat) {
        e.preventDefault();
        if (state === "speaking") handleBarge();
        else if (!talking) void startTalking();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        skip(1);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        skip(-1);
      } else if (e.key === "Escape") {
        if (showStudio || showMemory || showSettings) {
          setShowStudio(false);
          setShowMemory(false);
          setShowSettings(false);
        } else {
          handleBarge();
        }
      } else if (e.key.toLowerCase() === "m") {
        setMuted((prev) => !prev);
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
  }, [
    state,
    talking,
    inputMode,
    handleBarge,
    startTalking,
    stopTalking,
    skip,
    showStudio,
    showMemory,
    showSettings,
  ]);

  // --- Render ---------------------------------------------------------------

  /** Transcript entries plus the phase each agent sentence is in right now. */
  const streamLines: StreamLine[] = useMemo(() => {
    const seqIndex = new Map<number, number>();
    buffer.sentences.forEach((s, i) => seqIndex.set(s.seq, i));
    return entries.map((entry) => {
      const line: StreamLine = {
        id: entry.id,
        who: entry.who,
        text: entry.text,
        receipt: entry.receipt,
        seq: entry.seq,
        frame: entry.frame,
      };
      if (entry.who === "agent" && entry.seq !== undefined) {
        const index = seqIndex.get(entry.seq);
        line.phase =
          index === undefined
            ? "spoken"
            : index < buffer.head
              ? "spoken"
              : index === buffer.head
                ? "speaking"
                : "buffered";
      }
      if (entry.who === "eyes" && entry.eyesRef && eyesMap[entry.eyesRef]) {
        line.slot = <EyesBlock entry={eyesMap[entry.eyesRef]} />;
      }
      return line;
    });
  }, [entries, buffer, eyesMap]);

  const aheadCount = bufferedAhead(buffer).length;

  return (
    <div className="pt-app">
      <header className="pt-header">
        <div className="pt-header-left">
          <ArchieGlyph
            state={archieState(state, !connected || authError)}
            colourway="C4"
            size={24}
          />
          <span className="pt-wordmark">pet-talk</span>
          <span className="pt-chip">
            <span
              className={
                connected ? (state === "idle" ? "pt-dot" : "pt-dot pt-dot--live") : "pt-dot pt-dot--down"
              }
            />
            {connected ? STATE_WORD[state] : "offline"}
          </span>
          {aheadCount > 0 ? (
            <span className="pt-chip pt-chip--signal">
              {aheadCount === 1 ? "1 line ahead" : `${aheadCount} lines ahead`}
            </span>
          ) : null}
          {developer ? <span className="pt-mono">{turnRef.current}</span> : null}
        </div>

        <div className="pt-header-right">
          <button
            type="button"
            className="pt-btn pt-btn--ghost"
            onClick={() => setLang(lang === "en" ? "hi" : "en")}
            title="Language"
          >
            {lang === "en" ? "EN" : "हि"}
          </button>
          <button type="button" className="pt-btn" onClick={() => setShowStudio(true)}>
            {t["custom-persona"]}
          </button>
          <button type="button" className="pt-btn" onClick={() => setShowMemory(true)}>
            {t["memory-ledger"]}
          </button>
          <button type="button" className="pt-btn" onClick={() => setShowSettings(true)}>
            {t["settings"]}
          </button>
          <button
            type="button"
            className="pt-btn"
            aria-pressed={developer}
            onClick={() => setDeveloper((prev) => !prev)}
            title="Show frames, budgets and raw sources"
          >
            Developer
          </button>
          <ThemeToggle theme={theme} onChange={setTheme} />
        </div>
      </header>

      <div className="pt-body" data-developer={developer ? "true" : "false"}>
        <nav className="pt-rail">
          <div className="pt-rail-group">
            <p className="pt-lbl">voice</p>
            <button
              type="button"
              className="pt-rail-row"
              aria-pressed={talking}
              onMouseDown={() => void startTalking()}
              onMouseUp={() => (inputMode === "ptt" ? stopTalking() : undefined)}
            >
              {talking ? "listening — release to send" : "hold to talk (space)"}
            </button>
            <button
              type="button"
              className="pt-rail-row"
              aria-pressed={inputMode === "handsfree"}
              onClick={() => setInputMode(inputMode === "ptt" ? "handsfree" : "ptt")}
            >
              hands-free
            </button>
            <button
              type="button"
              className="pt-rail-row"
              aria-pressed={muted}
              onClick={() => setMuted((prev) => !prev)}
            >
              muted
            </button>
            <button type="button" className="pt-rail-row" onClick={handleBarge}>
              stop (esc)
            </button>
          </div>

          <div className="pt-rail-group">
            <p className="pt-lbl">level</p>
            <div style={{ padding: `0 var(--pt-s4)` }}>
              <AcousticOrb analyser={analyser} state={state} talking={talking} size={132} />
            </div>
          </div>

          <div className="pt-rail-group">
            <p className="pt-lbl">attach</p>
            <div style={{ padding: `0 var(--pt-s4)` }}>
              <EyesAttachDock onAttach={handleAttach} disabled={!connected} notice={eyesNotice} />
            </div>
          </div>

          {developer ? (
            <div className="pt-rail-group">
              <p className="pt-lbl">personas</p>
              {personas.map((p) => (
                <button
                  type="button"
                  key={p.name}
                  className="pt-rail-row"
                  aria-current={p.name === currentPersona}
                  onClick={() => handleSelectPersona(p.name)}
                >
                  {p.name}
                </button>
              ))}
            </div>
          ) : null}
        </nav>

        <main className="pt-main">
          {authError ? (
            <div className="pt-banner" role="alert">
              <span>studio token missing or wrong — set it in Settings to reconnect.</span>
              <button type="button" className="pt-btn pt-btn--danger" onClick={() => setShowSettings(true)}>
                Open Settings
              </button>
            </div>
          ) : null}

          {screenGround ? <ScreenGroundingLine screen={screenGround} /> : null}

          <div className="pt-scroll-y">
            <TranscriptStream
              lines={streamLines}
              developer={developer}
              emptyHint="Hold space and ask something. Buffered lines appear dim before they are spoken; → skips one, Esc stops."
            />
          </div>

          <LatencyBar timing={timing} developer={developer} turnKey={timingTurn} />

          <PromptComposer
            onSend={handleSendPrompt}
            disabled={!connected}
            connected={connected}
            serverUrl={httpBaseFromWs(WS_URL)}
            onAuthError={() => setAuthError(true)}
            t={t}
          />
        </main>

        {developer ? (
          <DeveloperRail
            frames={frames}
            timing={timing}
            personaName={currentPersona}
            providers={activeProviders}
            queueLength={buffer.sentences.length}
            bufferedCount={aheadCount}
            lastReceipt={lastReceipt}
          />
        ) : null}
      </div>

      {developer ? (
        <div style={{ position: "fixed", right: "var(--pt-s5)", bottom: "var(--pt-s5)" }}>
          <TelemetryHud
            state={state}
            metrics={metrics}
            queueLength={aheadCount}
            connected={connected}
          />
        </div>
      ) : null}

      {showStudio ? (
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
          onClose={() => setShowStudio(false)}
          t={t}
        />
      ) : null}

      <MemoryDrawer
        isOpen={showMemory}
        onClose={() => setShowMemory(false)}
        turns={memoryTurns}
        onClearMemory={handleClearMemory}
        t={t}
      />

      <SettingsModal
        isOpen={showSettings}
        onClose={() => setShowSettings(false)}
        settings={settings}
        activeProviders={activeProviders}
        secretsSet={secretsSet}
        onApplySettings={handleApplySettings}
        onTokenSaved={() => {
          setAuthError(false);
          socket.connect(WS_URL);
          setConnected(true);
        }}
        t={t}
      />

      <audio ref={audioRef} onEnded={onAudioEnded} hidden />
    </div>
  );
}
