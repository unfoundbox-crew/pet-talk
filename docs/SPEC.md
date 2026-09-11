# pet-talk — Tech Design Spec (living)

## 1. What this is
Full-duplex voice loop, local-first. User interrupts anytime, agent stalls
naturally, works while speaking. pet-talk v1 (HTTP+WAV streaming) DONE.
v0.2 duplex (WS + barge + worker lanes) IN BUILD.

## 2. Architecture (see TECH-SPEC.md §2 for wire detail)
Mic → VAD → STT → Router → FAST(stall)/WORKER(sentences) → TTS → play.
Barge kills playback, flushes queue, re-routes. Eyes (zero-vision) and
heartbeat join as senses, not features.

## 3. Provider tyres (config switch, zero code change)
| Tyre | Role | State |
|---|---|---|
| stub | loop testing, no models | working |
| kokoro/af_heart (SpacePilot daemon) | default voice | measured 777–1038ms/line |
| elevenlabs (Rachel id) | premium voice | 402-gated, flag kept |
| deepgram Nova/Aura ($200 credit) | STT overflow + TTS bake-off | TO BUILD |
| whisper-local / local LLM | offline path | TO BUILD |

## 4. Humanizer (anti-robotic)
instruction-spec personas (12 fields) → humanize.py (fillers, pauses,
slang, pace, beat) → backchannel (VAD-gated) → Kokoro. Timings from
field data (Retell/Vapi): turn ≤800ms, stall ≤400ms, barge ≤100ms.

## 5. Telemetry
TurnLog JSONL: turn_id, provider set, stage ms, totals. Every swap
diffable. Bake results in bake-kokoro/ + telemetry rows.

## 6. Gates (ship iff ALL green, no fake passes)
stall ≤400ms · barge kill ≤100ms · ≥3 sentences gapless ·
provider swap = config only · persona switch = voice+stalls.

## 7. Wave plan
- Wave 1 (done): server, frontend, personas, QA harness, humanizer.
- Wave 2 (now): Deepgram tyre, /voices+CORS+telemetry wiring, eyes+heartbeat spec.
- Wave 3 (later): real STT/LLM, echo cancel, multilingual paths, mobile.
