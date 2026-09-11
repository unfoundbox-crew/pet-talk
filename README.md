# pet-talk — full-duplex voice loop, local-first

Talk to your pets. User interrupts anytime, agent stalls naturally (`"let me
look into that"`), works while speaking. Kokoro voice, swappable tyres.

## Run (2 minutes)

```bash
./serve.sh                       # SpacePilot daemon :8088 (Kokoro)
python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8089   # duplex
cd web && npm install && npm run dev   # UI on :5173
```

Open http://127.0.0.1:5173 → push-to-talk → stall plays → answers stream →
BARGE mid-speech. Personas: donna / zuck / jarvis.

## Layout

| Path | What |
|---|---|
| `server/` | FastAPI duplex (WS `/ws`, providers, telemetry, `/voices`) |
| `web/` | React+Vite UI (push-to-talk, transcript, barge, pickers) |
| `personas/` | donna, zuck, jarvis (12-field instruction spec) + voices.yaml |
| `humanizer/` | Fillers, pauses, slang, pace (deterministic, seeded) |
| `qa/` | `./qa/run_all.sh` — protocol, persona, latency, live WS gates |
| `docs/` | SPEC.md (agents start here), WAVE3.md, charts of record |
| `say.sh` | Legacy streaming CLI (still works) |
| `bake-kokoro/`, `bake-deepgram_0.wav` | Voice bake-off evidence |

## Provider tyres (env switch, zero code change)

`TTS_PROVIDER=stub|kokoro|elevenlabs`, `STT_PROVIDER=stub|deepgram`
(+ whisper-local stub). ElevenLabs needs quota; Deepgram key via Doppler.

## Docs for agents: AGENTS.md. Design: docs/SPEC.md.
