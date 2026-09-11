# pet-talk duplex — Tech Spec (v0.2 target)

## 1. Goal

Full-duplex voice loop: user interrupts anytime, agent stalls naturally,
works while speaking. Latency floor ~600ms turn, barge-in <100ms.

## 2. Architecture

```
  MIC ──▶ VAD ──┬──▶ STT ──▶ ROUTER ──┬── FAST: stall ──▶ TTS ──▶ PLAY
                │                     └── WORKER: tools ──▶ sentences ──▶ TTS queue
                └── BARGE ──▶ kill playback, flush queue, re-route
  UI (React+Vite) ◀── WS ──▶ SERVER (FastAPI): transcript, state, controls
```

## 3. Module contracts (no hardcoding — every model swappable)

| Module | Interface | v0.2 backends |
|---|---|---|
| STTProvider | `transcribe(pcm16_bytes) -> text` | whisper-local; stub cloud |
| LLMProvider | `stream(messages) -> token_stream` + `route(text) -> stall\|answer` | local default; OpenAI-compatible URL swap |
| TTSProvider | `synth(text, voice) -> wav_bytes + word_times` | kokoro-af_heart + map |
| VAD | `voice_onset(pcm) -> bool` | energy gate v0.2, neural later |
| SpeakQueue | FIFO sentences; `flush()`, `resume_from(word_idx)` | in-process |
| Persona | `persona.md` frontmatter (voice, speed, stall inventory, tone rules) | donna, zuck, jarvis |

## 4. Wire protocol (WS `/ws`, JSON frames)

- `user.start` / `user.stop` (VAD-gated chunks), `barge` (kill + flush)
- `agent.stall` (immediate phrase id), `agent.sentence` (playable TTS url)
- `agent.done`, `state.idle|listening|thinking|speaking`
- Every frame carries `turn_id`; barge references it.

## 5. Voices + i18n

Kokoro map in `voices.yaml` (id → display name, lang). Multilingual:
STT language lock per turn, TTS voice per lang preference, UI strings via
`web/src/i18n/*.json` (en first, hi second). No hardcoded voice anywhere —
persona references voice id, server resolves.

## 6. Latency budget (measured, gated in CI smoke)

VAD 30 · STT 300 · LLM-first-token 400 · TTS-first-audio 300 ·
playback 20 · loopback 2 ≈ 1.1s naive, ~600ms with partial streaming.
Smoke test asserts p50 turn <1200ms on M1 Max or fails loudly.

## 8. Humanizer (anti-robotic layer; adopted, not invented)

Sources: OpenAI `speech` skill (instruction-spec schema), Retell
backchannel mechanism, voice-agents skill latency constants, Chatterbox
emotion control (backend option only).

### 8.1 Instruction spec (persona.md frontmatter extension)

Every persona carries these fields (stolen schema, Kokoro values):

`voice_affect, tone, pacing, emotion, pronunciation[], pauses[],
emphasis[], delivery, filler_rate (0.0-0.3), slang_level (0-2),
backchannel (on/off)`.

### 8.2 Humanizer preprocessor (`humanize.py`, stdlib only)

Pure function: `(text, spec) -> performed_text`. Ordered passes:
1. Contract (gonna/wanna/dunno per tone), 2. fillers at clause
   boundaries (p=filler_rate, never twice in row), 3. pause punctuation
   (, →150ms, ... →beat, line break → breath), 4. slang injection
   (cap slang_level), 5. energy variance (±5% speed tags per sentence),
   6. 150ms deliberate pre-answer beat marker. Deterministic under seed.

### 8.3 Backchannel (VAD-gated toggle)

While listening: VAD speech >1.5s continuous → emit one Micro
("mmhmm"/"yeah"/"right", persona-voiced, max 1 per 8s). Never steals
floor; never during user pause <500ms.

### 8.4 Latency constants (QA gates, from field data)

STT 150 · LLM 800 (budget, stream to beat) · TTS 200 · turn target
≤800ms steady, ≤1200ms worst. Silero thresholds: 250ms confirm,
500ms silence = end of turn. Stall ≤400ms, barge kill ≤100ms (unchanged).

## 9. Acceptance gates (ship iff ALL)

1. Stall plays ≤400ms after user stops on a research question.
2. Barge-in kills audio ≤100ms, resumes or redirects correctly.
3. Worker streams ≥3 sentences behind playing audio without gap.
4. Any provider swapped via config only, zero code change (tested by stub).
5. Persona switch changes voice + stalls + tone, nothing else.
