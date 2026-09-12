# pet-talk — SPEC

- **Status**: current
- **Decision date**: 2026-09-12
- **Verified against**: commit `844a685` on `feat/overnight-hardening` (server/, qa/, cli/hotkey/ read as of this branch; `cli/hotkey/*.swift` may still be under concurrent edit by another lane)
- **Scope**: server (`server/`), providers (`server/providers/`), QA gates (`qa/`), CLI hotkey daemon (`cli/hotkey/`)
- **Supersedes**: `TECH-SPEC.md` and `docs/TECH-DESIGN.md` (both now 5-line pointer stubs to this file; their prior content is preserved in git history at commit `844a685`)

This is the one contract for pet-talk. If a claim here disagrees with the code, the code wins — file a correction here, don't add a third document.

## 1. Goal

Full-duplex voice loop: the user can interrupt any time, the agent stalls naturally while it thinks, and TTS keeps streaming behind the stall without a gap. Local-first: every provider is a config swap, never a code change.

## 2. Architecture

```
        MIC ──▶ VAD ──▶ user.start/chunk/stop ──▶ WS /ws (ws.py)
                                                     │
                                          ┌──────────┴──────────┐
                                          │   Session (ws.py)    │
                                          │   one reader loop,   │
                                          │   many turn tasks    │
                                          └──────────┬──────────┘
                                                     │ handle_turn_task (turn.py)
                    ┌────────────────────────────────┼────────────────────────────┐
                    │                                │                            │
             control.py                        stall.py                    speech.py
        deterministic <20ms                cached stall audio         LLM producer ──▶
        (stop/status/who-are-you)          (agent.stall frame)        SpeakQueue (speak_queue.py)
                    │                                │                     ──▶ TTS consumer
                    │                                │                            │
                    └────────────────┬───────────────┴────────────────────────────┘
                                     │
                              frames.py (agent.sentence / agent.done / agent.error)
                                     │
                                    WS ──▶ web/src (React+Vite cockpit)

  grounding.py: repo head + AX screen line, injected into the system prompt (persona_runtime.py)
  eyes.py: user.attach → local OCR (zrv) → eyes.received / eyes.text, injected into turn context
  provider_factory.py + settings.py: build/rebuild the STT/LLM/TTS triple, config-only, under runtime.py's swap lock
```

## 3. Module map

| File | Job |
|---|---|
| `server/app.py` | Thin composer: assembles the FastAPI app, re-exports the stable import surface (`stt`/`llm`/`tts` module globals QA monkeypatches). No logic of its own. |
| `server/runtime.py` | Process-wide provider set and the swap lock; `current()`/`snapshot()` honour a monkeypatched `server.app.{stt,llm,tts}`; `install()`/`swap()` rebuild atomically. |
| `server/settings.py` | `RuntimeSettings` dataclass, env defaults, secret redaction, `SettingsStore.apply()` for `POST /settings`. |
| `server/provider_factory.py` | Builds the STT/LLM/TTS triple from settings; a failed tyre becomes `Unavailable{STT,LLM,TTS}` that re-raises on first use, never a silent fallback. |
| `server/frames.py` | `Frame`/`frame()` wire-frame construction, `safe_send_json`, `send_error`, int/float frame-field parsing. |
| `server/speak_queue.py` | `SpeakQueue`: async FIFO between the LLM producer and TTS consumer; `flush()` (barge), `resume_from()`, high-water mark. |
| `server/speech.py` | `run_speech`: LLM producer task + TTS consumer task; `speak_sentence` synthesizes and sends one `agent.sentence`. |
| `server/stall.py` | Cached stall-phrase audio, keyed by persona+voice+speed+TTS backend identity. |
| `server/control.py` | Deterministic local commands (stop/status/identity), zero LLM round trip. |
| `server/grounding.py` | Repo-head lines + AX screen snapshot, assembled into a capped system-prompt block. |
| `server/turn.py` | One turn end to end: route → control/stall/direct → speak → `agent.done`. |
| `server/ws.py` | The `/ws` endpoint: one reader loop, `Session` per socket, per-turn cancellable tasks, barge. |
| `server/routes_http.py` | HTTP routes: `/health`, `/voices`, `/personas`, `/settings`, `/ledger`, `/transcribe`, `/audio/{id}`. |
| `server/eyes.py` | Zero-vision attach lane: OCR via local `zrv` CLI, `handle_attach`. |
| `server/voices.py` | `personas/voices.yaml` reader. |
| `server/persona.py` | Persona load/save/delete, frontmatter parsing. |
| `server/persona_runtime.py` | Resolves the active persona for a frame, builds the system prompt (persona + grounding). |
| `server/memory.py` | Hippocampus: append-only turn ledger, history-for-prompt. |
| `server/audio_store.py` | Bounded in-memory WAV store for `/audio/{id}`. |
| `server/dictation.py` | `CleanProseFormatter` (transcript cleanup) and `VoiceProseFormatter` (spoken-text sanitize). |
| `server/logs.py` | `log`, `swallowed()` — the one place a caught exception becomes a named reason. |
| `server/telemetry.py` | `TurnLog`: one JSONL row per turn with stage marks. |
| `server/providers/_shared.py` | `ProviderError`, WAV/word-time helpers, the shared sentence-splitter every LLM `stream()` uses. |
| `server/providers/llm.py` | `LLMProvider` ABC, `OpenAICompatibleLLM`, `StubLLM`, `make_llm()`. |
| `server/providers/stt.py` | `STTProvider` ABC and backends (Deepgram, Groq, OpenAI, WhisperKit, MLX, faster-whisper, SenseVoice, stub), `make_stt()`. |
| `server/providers/tts.py` | `TTSProvider` ABC and backends (Kokoro, Smallest.ai, ElevenLabs, Deepgram, stub), `make_tts()`. |
| `server/providers/vad.py` | `VAD` ABC, `EnergyGateVAD`. |
| `qa/run_all.sh` | The QA gate runner: discovers suites from one array, bounded timeouts, honest SKIP/PASS/FAIL. |
| `qa/budgets.json` | The one source of truth for latency budgets. |
| `qa/latency.py` | Live-WS latency probe against `qa/budgets.json`; prints `NOT-MEASURED` with no server. |
| `cli/hotkey/*.swift` | Native macOS hotkey/HUD/earcon daemon. A concurrent lane may still be editing this — see section 11. |

## 4. Wire protocol (WS `/ws`, JSON frames)

Every frame carries `turn_id`. `Frame.__post_init__` raises `frame_no_turn_id` if it doesn't — this is fail-closed by construction, not convention.

### 4.1 Client → server

| Frame | Fields | Handler |
|---|---|---|
| `user.start` | `turn_id?` | `_on_user_start` — resets chunk buffer, sends `state.listening` |
| `user.chunk` | `chunk` (b64 PCM16) | `_on_user_chunk` — appends to buffer |
| `user.stop` | `turn_id?`, `pcm_b64?`, `sample_rate?` | `_on_user_stop` — runs STT in the turn task, fires `handle_turn_task` |
| `user.text` | `text`, `turn_id?`, persona overrides | `_on_user_text` — skips STT, fires `handle_turn_task` directly |
| `user.attach` | `ref?`, `kind`, `mime?`, `b64`/`bytes_b64`, `filename?`, `task?` | `_on_user_attach` — delegates to `server.eyes.handle_attach` if importable, else `agent.error` reason `eyes_disabled` |
| `barge` | `turn_id?` | `_on_barge` — cancels the live turn(s), flushes the queue, returns the true dropped count |

An unrecognized frame type gets `agent.error` reason `unknown_frame` (detail: the type string, or `<missing type>`). Undecodable JSON gets reason `bad_frame`.

### 4.2 Server → client

| Frame | Fields | Emitted by |
|---|---|---|
| `state.idle` / `state.listening` / `state.thinking` / `state.speaking` | — | `ws.py`, `turn.py` |
| `transcript.user` | `text` | after STT, or immediately for `user.text` |
| `agent.stall` | `phrase_id`, `text` | `turn.py` worker path, before the LLM answer starts |
| `agent.sentence` | `seq`, `text`, `audio_url`, `word_times`, `estimated` | `speech.py:speak_sentence` — one per spoken sentence |
| `agent.done` | `path`, `sentences`, `dropped?` | end of every turn; `path` is one of `empty`, `control`, `control_cancel`, `worker`, `direct`, `interrupted`, `error` |
| `agent.error` | `reason`, `detail?`, plus per-call fields (`seq`, `ref`, ...) | any failure, see catalogue in §4.3 |
| `eyes.received` | `ref`, `kind`, `task`, `bytes` | `eyes.py:handle_attach` as soon as the payload is accepted |
| `eyes.text` | `ref`, `source`, `kind`, `task`, `engine`, `text`, `truncated` | `eyes.py:handle_attach` after OCR resolves |

### 4.3 `agent.error` reason catalogue

Every reason a running server can actually emit today, grepped from `ProviderError(...)`, `EyesError(...)`, and direct `send_error(...)` calls. Grouped by where it originates.

**Frame/protocol (`frames.py`, `ws.py`, `turn.py`):**
`frame_no_turn_id`, `bad_frame`, `unknown_frame`, `empty_transcript`, `empty_text`, `frame_handler_failed`, `turn_task_exception`, `ws_send_disconnected`, `ws_send_after_close`, `ws_send_failed`, `ws_loop_failed`, `ws_shutdown_cancelled`, `ws_shutdown_flush_cancelled`.

**Providers, generic (`provider_factory.py`, `providers/_shared.py`):** `missing_api_key`, `provider_unreachable`, `provider_build_failed`, `provider_unavailable` (logged, not sent — see `/health`'s `degraded` list instead).

**LLM (`providers/llm.py`, `turn.py`, `speech.py`):** `llm_unknown_provider`, `llm_empty_messages`, `llm_stream_failed`, `llm_openai_not_installed`, `llm_route_failed` (swallowed, falls back to `route_text` — never surfaced to the client).

**STT (`providers/stt.py`, `turn.py`):** `stt_unknown_provider`, `stt_empty_audio`, `stt_empty_result`, `stt_bad_response`, `stt_request_failed`, `stt_no_key`, `stt_failed`, `stt_faster_whisper_not_installed`, `stt_whisper_not_installed`, `stt_whisperkit_failed`, `stt_mlx_failed`, `stt_mlx_not_installed`.

**TTS (`providers/tts.py`, `speech.py`, `stall.py`):** `tts_unknown_provider`, `tts_empty_text`, `tts_empty_audio`, `tts_synth_failed`, `tts_request_failed`, `tts_no_key`, `tts_no_token`, `tts_no_job_id`, `tts_job_failed`, `tts_job_timeout`, `tts_download_failed`, `stall_synth_failed`.

**SpeakQueue (`speak_queue.py`):** `queue_closed`, `queue_no_spoken_sentence`, `queue_bad_word_idx`, `queue_close_failed`.

**Persona (`persona.py`, `routes_http.py`, `turn.py`):** `persona_unloadable`, `persona_no_name`, `persona_save_failed`, `persona_protected`, `persona_no_stalls`, `unknown_persona`.

**Voices (`voices.py`):** `voices_not_found`, `voices_bad_yaml`, `voices_bad_voice`.

**Grounding (`grounding.py`, swallowed — logged, never sent to the client):** `grounding_repo_missing`, `grounding_subprocess_timeout`, `grounding_subprocess_missing`, `grounding_subprocess_failed`, `grounding_subprocess_nonzero`, `grounding_subprocess_kill_failed`, `grounding_subprocess_unreaped`, `grounding_subprocess_reap_failed`, `grounding_repo_task_failed`, `ax_bad_json`, `ax_not_ok`, `ax_empty_context`, `ax_task_failed`.

**Eyes (`eyes.py`):** `eyes_too_large`, `eyes_bad_kind`, `eyes_ocr_failed`, `eyes_no_text`, `eyes_disabled`, plus `eyes_import_failed` (swallowed at import time in `ws.py`, surfaces as `eyes_disabled` to the client).

**HTTP-only reasons (`routes_http.py`, JSON body not `agent.error`):** `bad_request`, `not_found`, `audio_not_found`, `ledger_clear_failed`, `invalid_base64` (`/transcribe`), `empty_audio` (`/transcribe`).

**Env parsing (swallowed, logged only):** `bad_vad_silence_ms`, `bad_vad_silence_ms_env`, `bad_turn_delay_env`.

## 5. Provider contract

Every provider is a config swap: `STT_PROVIDER` / `LLM_PROVIDER` / `TTS_PROVIDER` at boot (env), or a live `POST /settings` at runtime. No code path branches on which provider is live.

### 5.1 Sync methods

```
STTProvider.transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> str
LLMProvider.route(text: str) -> "stall" | "answer"     # deterministic keyword routing, no LLM call
LLMProvider.stream(messages: list[dict]) -> AsyncIterator[str]   # yields whole sentences
TTSProvider.synth(text: str, voice: str, speed: float) -> tuple[bytes, list[dict]]   # (wav_bytes, word_times)
```

### 5.2 `word_times` shape

```json
[{"word": "hello", "start_ms": 0, "end_ms": 350, "estimated": true}]
```

`estimated: true` means the backend gave no real per-word timing and `estimate_word_times()` (uniform split, `_shared.py`, or the char-length-weighted split in `speak_queue.py`) filled in a guess. Kokoro currently reports estimated timings for every word — see §7. A `SpokenSentence` is `estimated=True` overall iff any word in it is estimated.

### 5.3 Factory error reasons

`make_llm()` / `make_stt()` / `make_tts()` never do network I/O and never silently substitute a provider. An unknown provider name raises `{llm,stt,tts}_unknown_provider`. A provider missing its required key raises `missing_api_key` with the env var name as `detail`, checked once in `provider_factory.py:_require_key` before construction. `provider_factory.build_providers()` itself never raises — every failure becomes an `Unavailable{STT,LLM,TTS}` placeholder that re-raises the original `ProviderError` on first real use, and is listed by class+reason in `/health`'s `degraded` array.

## 6. Settings API

`GET /settings` / `POST /settings` (`routes_http.py`), guarded by `runtime.swap_lock` so a swap can't land mid-turn — every turn snapshots the provider triple once at start (`runtime.snapshot_under_lock()`).

- **Redaction**: any field whose name contains `key`, `token`, `secret`, or `password` (`settings.py:SECRET_HINTS`) comes back from `GET`/`POST` as `"***"` if set, `""` if not, alongside a companion `secrets_set: {field: bool}` map. Real values are never returned.
- **`***` semantics**: `POST /settings` treats an incoming field value of exactly `"***"` as "leave this field alone" (`SettingsStore.apply`). This is what lets a UI round-trip a masked `GET` payload without wiping a live credential — it can never accidentally set a key to the literal string `***`.
- **Swap atomicity**: `SettingsStore.apply()` merges the request into a `replace()`d copy of `RuntimeSettings`, then `provider_factory.build_providers()` builds a fresh triple, then `runtime.install()` publishes it (updates `server.app.{stt,llm,tts}` and the module-level `_providers`). All three happen under one lock acquisition (`runtime.swap`). A provider change with no explicit `llm_base_url`/`llm_model`/`llm_api_key` in the same request picks that provider's own defaults (`LLM_BASE_URL_DEFAULTS`/`LLM_MODEL_DEFAULTS`/`key_for_llm()`) instead of inheriting the previous provider's — a `groq`→`gemini` swap doesn't accidentally keep pointing at Groq's URL.

## 7. Grounding

`server/grounding.py` assembles a capped (`MAX_GROUNDING_CHARS = 460`) block injected into the system prompt every turn (`persona_runtime.build_system_prompt`). Three sources, gathered concurrently with `asyncio.gather`:

1. **Repo-relative git head lines.** Always this checkout's own root (`repo_root()`, derived from `__file__` — no absolute path baked in). Extra repos come from `PET_TALK_GROUNDING_REPOS`, a colon-separated list of paths, empty by default. Each is a 1s-timeout `git log -n1 --oneline` subprocess.
2. **AX screen snapshot** (`ax_snapshot()`), gated by `PET_TALK_AX=1`. When set, it shells out to `pet-talk-hotkey ax` (0.3s timeout) for one JSON line describing the frontmost app/window/selection. **This directly needs the macOS Accessibility permission** — the hotkey binary must be granted Accessibility access in System Settings for `ax` to return anything but a fast `ax_not_ok`. That contradicts any "zero TCC/Accessibility permissions" claim made elsewhere for the base hotkey daemon: the *hotkey itself* (Carbon `RegisterEventHotKey`) needs no Accessibility grant, but `PET_TALK_AX=1` grounding does. Say so plainly rather than paper over it — it's an opt-in trade, not a bug.
3. **Static lines** — currently one: `"Voice loop: pet-talk duplex, local-first tyres."`

All three are gathered even on failure (`return_exceptions=True`); a failed lookup is logged via `swallowed()` and contributes nothing rather than blocking the turn.

## 8. Eyes lane

`server/eyes.py`. A client attaches an image/screenshot/PDF over the existing `/ws` socket (`user.attach`); the server OCRs it locally and injects `[eyes:<ref>:<kind>|<task>] <text>` into the next turn's context. No pixels or vision tokens leave the machine.

**Engines that actually exist** (`make_engine`, `EyesConfig.engine`, default `zrv`):
- `zrv` — shells out to the real `zrv` CLI. `zrv`'s own `--engine` ids (verified against `zrv --help` 2026-09-12): `apple-vision`, `apple-fm`, `local-vlm`, `cloud-vlm`, `tesseract`. `apple-vision` is zrv's local default on Apple silicon and is **OCR-only** — it refuses `--task describe`.
- `stub` — deterministic canned text, used by `qa/test_eyes.py`; never touches the filesystem or a subprocess.

An unknown engine name fails closed (`make_engine` raises `EyesError(eyes_disabled, unknown_eyes_engine:<name>)`) — there is no silent fallback to `stub` or `zrv`.

**Describe-mode measurement (2026-09-12):** the only local engine that can actually run `--task describe` is `apple-fm` (`apple-vision` refuses it in ~0.2s with a clean error). `apple-fm` was measured at **101.9s for one 2280×600 PNG** — 12x the 8s `EYES_TIMEOUT_S` default and ~68x an earlier 1.5s target. Because of this, **`describe` ships disabled by default**: `EyesConfig.describe_engine_pin` is unset, so a `describe` task hits `apple-vision`'s fast, honest refusal instead of a 100+ second hang. Set `EYES_DESCRIBE_ENGINE=apple-fm` and raise `EYES_TIMEOUT_S` well past 100s if you want describe mode anyway. `EYES_DEFAULT_TASK` and persona frontmatter `eyes_task`/`eyes_default` both default to `transcribe`.

Other knobs: `EYES_MAX_BYTES` (8MB decoded cap), `EYES_CHAR_CAP` (4000 chars before `truncated:true`), `EYES_TIMEOUT_S` (8.0s OCR wall clock), `EYES_ZRV_BIN`, `EYES_SCRATCH_DIR` (system temp dir, never inside the repo). A PDF is always `transcribe` (`task_for`), capped at `pdf_max_pages=5`.

## 9. Latency budgets and gates

Source of truth: `qa/budgets.json`.

| Budget | ms |
|---|---|
| `stall_ms` | 400 |
| `barge_ms` | 100 |
| `turn_p50_ms` | 800 |
| `turn_worst_ms` | 1200 |
| `stt_ms` | 150 |
| `llm_ms` | 800 |
| `tts_ms` | 200 |

### 9.1 MEASURED (2026-09-12, stub providers only — not a real-engine measurement)

`qa/latency.py` against a running server with `STT_PROVIDER=stub LLM_PROVIDER=stub TTS_PROVIDER=stub`:

| Metric | p50 (ms) | p95 (ms) |
|---|---|---|
| `stall_ms` | 18.1 | 20.2 |
| `first_sentence_ms` | 18.1 | 20.3 |
| `barge_ms` | 0.5 | — |

Hermetic slow-TTS barge test (`qa/test_turn_lifecycle.py`): barge kill at **0.8ms**, `dropped=4`, queue high-water **3**.

**Real-engine numbers: NOT MEASURED.** These numbers exercise the queue, the frame plumbing, and the cancellation path — not a real STT/LLM/TTS backend's actual latency. Treat every ms above as "the harness works," not "the product is fast." Do not quote these as user-facing latency.

### 9.2 Acceptance gates and their proof

| Gate | Proven by |
|---|---|
| 1. Stall plays ≤400ms after user stops, on a routed question | `qa/latency.py` (`stall_ms` vs budget), `qa/test_turn_lifecycle.py` |
| 2. Barge-in kills audio ≤100ms and reports the true dropped count | `qa/latency.py` (`barge_ms`), `qa/test_turn_lifecycle.py`, `qa/test_socket_resilience.py` |
| 3. Worker streams ≥3 sentences behind playing audio without a gap | `qa/test_turn_lifecycle.py` — **partially exercised**: `StubLLM` emits exactly 3 sentences by default, and the queue high-water test uses a 4-sentence variant; there is no fixture that streams a long, unbounded answer to confirm the queue keeps buffering past 4-5 sentences under sustained LLM-faster-than-TTS pressure |
| 4. Provider swap via config only, zero code change | `qa/test_providers.py`, `qa/test_settings_api.py` |
| 5. Persona switch changes voice + stalls + tone, nothing else | `qa/test_persona.py`, `qa/test_persona_api.py` |

## 10. Known gaps

- **The speak queue is per-socket**, not per-turn: `Session.queue` is one `SpeakQueue` reused across turns via `reopen()`. Two turns on the same socket can never interleave (the newer one barges the older via `supersede()`), but this means there is exactly one live turn per socket at a time by design, not by accident.
- **Gate 3 (≥3 gapless sentences) is only partially exercised** — see §9.2. `StubLLM` hardcodes 3 sentences; nothing in `qa/` currently proves the queue keeps buffering ahead past a 4th or 5th sentence under sustained pressure.
- **PDF OCR is untested.** `eyes.py`'s PDF path (`pdf_max_pages=5`, `task_for()` forcing `transcribe`) has no `qa/test_eyes.py` coverage with an actual PDF fixture — only image/stub-engine paths are tested.
- **Kokoro word timings are estimated, not measured.** `KokoroSpacePilotTTS` does not appear to report real per-word timestamps from the daemon; `word_times` for Kokoro-synthesized audio comes from `estimate_word_times()` (uniform-per-word or char-length-weighted), always carrying `estimated: true`. Any UI that highlights the currently-spoken word against Kokoro audio is highlighting a guess, not a measurement.
- **`cli/hotkey/build.sh` does not exist yet** in this checkout, though `Makefile`'s `build-hotkey` target and this repo's build comments both reference it as owned by another lane. `make build-hotkey` will fail until that script lands.

## 11. A note on `cli/hotkey/*.swift`

These five files (`main.swift`, `hud_window.swift`, `earcons.swift`, `paste_injector.swift`, `config.swift`) may be under concurrent edit by another lane in this worktree. This spec describes their existence and role (native macOS Carbon hotkey listener, notch HUD, earcon engine, paste injector, config loader) per the module map in §3, but does not assert their current internal behavior in detail — verify against the live files before trusting a specific claim about them.
