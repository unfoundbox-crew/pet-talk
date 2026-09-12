# pet-talk Wave 3 — Eyes, Heartbeat, Real STT/LLM

**Status (2026-09-12): section 1 (Eyes) is implemented**, in
`server/eyes.py`, wired into `server/ws.py:_on_user_attach`. The real engine
ids are `zrv` (default, shells out to the `zrv` CLI; its own `--engine`
choices are `apple-vision`, `apple-fm`, `local-vlm`, `cloud-vlm`,
`tesseract`) and `stub` (deterministic, test-only). See `docs/SPEC.md` §8 for
the current contract, including the `describe` task shipping disabled by
default (measured `apple-fm` latency ~102s per screenshot). Sections 2
(Heartbeat) and 3 (real STT/LLM wiring beyond what §5 of `docs/SPEC.md`
already documents) below are design-only, not yet built — read them as
proposals, not as a description of shipped code.

Refs: `docs/SPEC.md` (the current contract — read this first, not the
`TECH-SPEC.md §2-4` references below, which point at a superseded doc),
`server/ws.py` (`frame()`, `handle_turn_task`, `SpeakQueue`),
`server/providers/` (`STTProvider`, `LLMProvider`, `ProviderError`,
`make_tts`), `server/persona.py` (frontmatter), `server/telemetry.py`
(`TurnLog`), `personas/*.md`, `humanizer/humanize.py`.

## 1. Eyes provider (SHIPPED: `server/eyes.py`, caller: `server/ws.py`)

Goal: attach image/PDF/screenshot over existing WS `/ws`; server OCRs via `zrv ocr`
subprocess; text lands in turn context tagged by source. No new transport.

### 1.1 WS message shape (extends `app.py:frame()` — every frame keeps `turn_id`)

Client → server: `{"type":"user.attach","turn_id":"t…","ref":"att-1","kind":"image|pdf|screenshot","mime":"image/png|application/pdf","bytes_b64":"…","filename":"…"}`
Server → client acks: `{"type":"eyes.received","turn_id":"t…","ref":"att-1"}`
then `{"type":"eyes.text","turn_id":"t…","ref":"att-1","source":"eyes:att-1:screenshot","task":"transcribe|describe","text":"…","truncated":false}`
Failures reuse `agent.error` (`app.py`): reasons `eyes_too_large`, `eyes_bad_kind`, `eyes_ocr_failed`, `eyes_no_text`.

Limits: one attach per turn v1; cap 8MB decoded, PDF first 5 pages only; store bytes in tmp dir (never `_audio_store`).

### 1.2 Server resolve (`eyes.py:EyesProvider.resolve(ref) -> (task, text)`)

1. Sniff mime, enforce limits above. 2. Shell out: `zrv ocr <tmpfile> --engine <e> --task <transcribe|describe>`.
`--engine`: `local` default, `agy-flash` fallback for contact sheets (per repo rule: Flash for OCR sweeps).
`--task transcribe`: verbatim text (receipts, PDFs, contact sheets). `--task describe`: one-paragraph scene (photos, screenshots).
3. Persona picks task: new frontmatter key `eyes_default: transcribe|describe` (`server/persona.py` extension, default `transcribe`); screenshot + question mark in turn text forces `describe`. 4. Inject into `handle_turn` context as `[eyes:att-1:screenshot|describe] <text>` (max 2000 chars, `truncated:true` beyond). 5. `TurnLog.mark("eyes_ocr_ms")` per turn (`server/telemetry.py`).

### 1.3 Worked examples

A. Screenshot question: user attaches settings screenshot + asks "why greyed out?" → `describe` → context `[eyes:att-1:screenshot|describe] dialog, toggle off, …` → direct path (`route_text`→answer), spoken in ≤3 sentences.
B. PDF summarize: 4-page PDF → `transcribe` (pages 1–5 rule covers it) → stall path (`agent.stall` then worker streams ≥3 sentences, `TECH-SPEC.md` §9 gate 3).
C. Contact-sheet read: 3×3 thumbnails → `--engine agy-flash --task transcribe` → one line per cell (`r1c2: …`); garble → `eyes_no_text` → ask user to retake (see failures).

### 1.4 Failure modes

No debug path/oversize: `eyes_too_large` before subprocess (never OOM the loop). OCR garble (empty/low-confidence): `eyes_ocr_failed`, human confirms ("read it back?") — never hallucinate text. PII note: `TurnLog` stores text hash only (`telemetry.py` rule), raw OCR text never hits JSONL; tmp files deleted after resolve.

## 2. Heartbeat (NEW `server/heartbeat.py`, tick lane inside `server/app.py`)

A single asyncio tick task, not a new server. Default interval 15min (`HEARTBEAT_INTERVAL_S=900`).
Quiet hours 23:00–07:00 local (`HEARTBEAT_QUIET=23-7`): tick runs, jobs suppressed unless `house_on_fire=true`.
Per-persona mute: frontmatter `heartbeat: off|digest-only` (`server/persona.py`); `heartbeat: off` (e.g. `personas/zuck.md`) kills all three jobs.

### 2.1 Three jobs (only one fires per tick, priority: keepalive > digest > nudge)

Overnight digest: fires once after 07:00 if turns happened during quiet hours → `{"type":"agent.digest","turn_id":"hb-…","items":["3 turns last night,…"]}`.
Idle nudge: no `user.stop` for 2× interval in `state.idle` → `{"type":"agent.nudge","turn_id":"hb-…","text":"Still here — …"}` (max 1/hour).
Long-task keepalive: worker path (`handle_turn` stall branch) running >60s → `{"type":"agent.keepalive","turn_id":"<live>","text":"Still working…"}` (max 1 per turn).

### 2.2 Mute/kill rules

`barge` frame cancels pending nudge/keepalive for that turn (`SpeakQueue.flush()` already drops audio; heartbeat drops text). Any `user.start` resets idle clock. `HEARTBEAT_OFF=1` env kills the lane (tests). Never speak unprompted 23:00–07:00 unless digest backlog flagged `house_on_fire` (explicit job return, e.g. repeated `ProviderError` overnight) — then one `agent.keepalive`, not chat.

## 3. Real-STT/LLM wiring (no interface changes; config-only per `docs/SPEC.md` §3)

Both classes already exist behind ABCs (`server/providers.py`): `WhisperLocalSTT`, `OpenAICompatibleLLM`. Work is selection + gating, not new contracts.

`server/app.py` change (mirrors `make_tts()` tyre switch): `make_stt()` → `STT_PROVIDER=stub|whisper-local` (`WHISPER_MODEL=base`); `make_llm()` → `LLM_PROVIDER=stub|local-openai` (`LLM_BASE_URL`, `LLM_MODEL`). Module-level `stt = …; llm = …` lines only; `SpeakQueue`, `frame()`, `handle_turn` untouched. Lazy imports stay inside methods (existing rule: no heavy deps at import). Env missing → fail-closed `ProviderError` (`stt_whisper_not_installed`, `llm_openai_not_installed`) surfaced as `agent.error`, loop stays up.

Acceptance (extends `TECH-SPEC.md` §9 gates): WER ≤15% on 20-utterance `qa/` fixture (new `qa/stt_fixture.jsonl`); LLM first-token ≤800ms p50 local (`TECH-SPEC.md` §8.4 budget); full turn ≤1200ms p50 (existing smoke); provider swap test: stub↔real via env only, zero code diff; `TurnLog` rows diffable per `docs/SPEC.md` §5.

Open: Deepgram tyre (`docs/SPEC.md` §3 TO BUILD) reuses same `make_*` pattern later; echo-cancel/multilingual stay Wave 3-later per `docs/SPEC.md` §7.
