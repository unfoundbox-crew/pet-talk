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
| `user.chunk` | `chunk` (b64 PCM16), `sample_rate?` | `_on_user_chunk` — appends to buffer, and feeds `server/stt_stream.py`'s rolling window when `PET_TALK_STT_STREAM=1` |
| `user.stop` | `turn_id?`, `pcm_b64?`, `sample_rate?` | `_on_user_stop` — runs STT in the turn task, fires `handle_turn_task` |
| `user.text` | `text`, `turn_id?`, persona overrides | `_on_user_text` — skips STT, fires `handle_turn_task` directly |
| `user.attach` | `ref?`, `kind`, `mime?`, `b64`/`bytes_b64`, `filename?`, `task?` | `_on_user_attach` — delegates to `server.eyes.handle_attach` if importable, else `agent.error` reason `eyes_disabled` |
| `user.handover` | `turn_id?`, `source?` | `_on_user_handover` — hand-over chord (Option+Shift+Tab). Marks the next turn as delegated work and opens the mic; acks with `handover.received` (§4.2) then `state.listening` |
| `barge` | `turn_id?` | `_on_barge` — cancels the live turn(s), flushes the queue, returns the true dropped count |

An unrecognized frame type gets `agent.error` reason `unknown_frame` (detail: the type string, or `<missing type>`). Undecodable JSON gets reason `bad_frame`.

### 4.2 Server → client

| Frame | Fields | Emitted by |
|---|---|---|
| `state.idle` / `state.thinking` / `state.speaking` | — | `ws.py`, `turn.py` |
| `state.listening` | `barged_turn?`, `dropped?` | `ws.py`. The barge ack carries both: `barged_turn` is the id of the turn that was killed (which is NOT this frame's `turn_id` — a barge mints a fresh one, and the client adopts it), `dropped` is the true count of cancelled tasks. Absent on a plain `user.start` or hand-over ack |
| `handover.received` | `source` | `ws.py:_on_user_handover` — acknowledges `user.handover`; `state.listening` follows it |
| `transcript.user` | `text`, `handover`, `partial?`, `final?` | after STT, or immediately for `user.text`; `handover` is true when a `user.handover` chord preceded this turn (consumed once). Under streaming STT the frame carries `final: true`, and `partial: true` frames may precede it when `PET_TALK_STT_PARTIALS=1` — every partial precedes the one final frame, and a client that ignores both fields sees exactly today's wire |
| `agent.stall` | `phrase_id`, `text` | `turn.py` worker path, before the LLM answer starts — or `stt_stream.emit_early_stall` at end-of-speech under streaming STT, from the latest partial, BEFORE the final transcript exists (§9.2) |
| `agent.sentence` | `seq`, `text`, `audio_url`, `word_times`, `estimated`, `stream_url`, `chunked` | `speech.py:speak_sentence` — one per spoken sentence. The two STALL sites (`turn.py`'s worker path and `stt_stream.emit_early_stall`) send the SAME field set, with `stream_url: null` and `chunked: false` — a stall is never chunked, and a client must not need a special case to find that out. `seq=0` is the stall's on every path; both answer paths start at `first_seq=1` |
| `agent.chunk` | `seq`, `chunk_no`, `audio_b64`, `url`, `final` | `speech.py:stream_chunks` — one per synthesis chunk, only on a chunk-capable tyre |
| `agent.done` | `path`, `sentences`, `dropped?`, `reason?` | end of every turn; `path` is one of `empty`, `control`, `control_cancel`, `worker`, `direct`, `interrupted`, `error`. `sentences` is on EVERY path, `interrupted` included, where it is always `0` — a barged turn's spoken count is not claimed. `path="error"` follows the `agent.error` that named the failure and repeats its `reason`, so a client always sees a turn end |
| `agent.error` | `reason`, `detail?`, plus per-call fields (`seq`, `ref`, ...) | any failure, see catalogue in §4.3 |
| `eyes.received` | `ref`, `kind`, `task`, `bytes` | `eyes.py:handle_attach` as soon as the payload is accepted |
| `eyes.text` | `ref`, `source`, `kind`, `task`, `engine`, `text`, `truncated` | `eyes.py:handle_attach` after OCR resolves |
| `agent.receipt` | `claim`, `source{kind,id,repo,path}`, `tokens`, `cost_usd`, `index_age_s`, `fresh` | `receipts.py:attach` — one per spoken claim about work done |

### 4.2.1 Chunked audio (`agent.chunk`)

A sentence's audio arrives before the sentence is finished being synthesized.
This is what puts the first audible audio inside the 200ms `tts_ms` budget: what
has to fit is one clause, not one sentence.

```
agent.sentence { seq, text, audio_url, word_times, estimated,
                 stream_url: str | null,   # /audio/<id> of chunk 0, null when not chunked
                 chunked: bool }           # true when agent.chunk frames preceded this
agent.chunk    { seq, chunk_no, audio_b64, url, final: bool }
```

Rules a client can rely on:

- **Every `agent.chunk` for a sentence precedes that sentence's
  `agent.sentence`.** `chunk_no` counts from 0 with no gaps, and exactly one
  chunk carries `final: true` — `stream_chunks` owns that flag, so a backend
  that never sets it or sets it on every chunk cannot change the count. A
  backend whose stream drained with no final flag earns a terminator frame:
  `final: true` with an **empty `audio_b64`** and the previous chunk's `url`.
  That is the only chunk frame with no audio; a client treats it as
  end-of-stream, never as a decode failure.
- **Each chunk is a complete RIFF/WAVE file**, not a PCM fragment, so it plays
  on arrival with no header to assemble. `audio_b64` is that file, base64'd;
  `url` serves the identical bytes for a client that would rather fetch than
  decode.
- **`audio_url` still carries the whole sentence, on both paths.** A client
  written against the old contract ignores `agent.chunk` and keeps working —
  backward compatible for one release. `_shared.concat_wavs` re-muxes the chunks
  by PCM to build it (gluing WAV byte strings would produce a header that lies
  about its length).
- **`word_times` on the sentence frame spans the whole sentence**; each chunk's
  own `word_times` are chunk-local, from zero. `_shared.shift_word_times` lays
  them onto the sentence timeline.
- **`chunked: false` is not a degraded frame, and never silent.** A tyre without
  `synth_chunks` — every cloud backend, which returns one finished file — takes
  the whole-sentence path, and the reason is logged by name:
  `tts_no_chunk_support`, or `tts_chunking_disabled` when
  `PET_TALK_TTS_CHUNKS=0` turned it off.

Env:

| var | default | what |
|---|---|---|
| `PET_TALK_TTS_CHUNKS` | `1` | `0` forces the whole-sentence path for every tyre |
| `PET_TALK_TTS_FIRST_CHUNK_WORDS` | `4` | words in chunk 0 — the only chunk racing a budget |
| `PET_TALK_TTS_CHUNK_WORDS` | `6` | words in every chunk after the first |
| `PET_TALK_CHUNK_MAX_BYTES` | `524288` | per-chunk cap; over it the sentence fails closed with `tts_chunk_too_large`, before the chunk is stored or sent |
| `PET_TALK_STALL_WARM` | `1` | `0` skips the startup stall pre-synth, and says so |

### 4.3 `agent.error` reason catalogue

Every reason a running server can actually emit today, grepped from `ProviderError(...)`, `EyesError(...)`, and direct `send_error(...)` calls. Grouped by where it originates.

**Frame/protocol (`frames.py`, `ws.py`, `turn.py`):**
`frame_no_turn_id`, `bad_frame`, `unknown_frame`, `empty_transcript`, `empty_text`, `frame_handler_failed`, `turn_task_exception`, `ws_send_disconnected`, `ws_send_after_close`, `ws_send_failed`, `ws_loop_failed`, `ws_shutdown_cancelled`, `ws_shutdown_flush_cancelled`.

**Providers, generic (`provider_factory.py`, `providers/_shared.py`):** `missing_api_key`, `provider_unreachable`, `provider_build_failed`, `provider_unavailable` (logged, not sent — see `/health`'s `degraded` list instead).

**LLM (`providers/llm.py`, `turn.py`, `speech.py`):** `llm_unknown_provider`, `llm_empty_messages`, `llm_stream_failed`, `llm_openai_not_installed`, `llm_route_failed` (swallowed, falls back to `route_text` — never surfaced to the client).

**STT (`providers/stt.py`, `turn.py`):** `stt_unknown_provider`, `stt_empty_audio`, `stt_empty_result`, `stt_bad_response`, `stt_request_failed`, `stt_no_key`, `stt_failed`, `stt_faster_whisper_not_installed`, `stt_whisper_not_installed`, `stt_whisperkit_failed`, `stt_mlx_failed`, `stt_mlx_not_installed`.

**TTS (`providers/tts.py`, `speech.py`, `stall.py`):** `tts_unknown_provider`, `tts_empty_text`, `tts_empty_audio`, `tts_synth_failed`, `tts_request_failed`, `tts_no_key`, `tts_no_token`, `tts_no_job_id`, `tts_job_failed`, `tts_job_timeout`, `tts_download_failed`, `stall_synth_failed`, `tts_chunk_format_mismatch`, `tts_chunk_not_wav`, `tts_chunk_too_large` (one `agent.chunk` over `PET_TALK_CHUNK_MAX_BYTES`, default 512 KiB — refused before it is stored or sent), `tts_cancelled`. Two more are telemetry only — logged by `speak_sentence`, never sent as a frame, because they describe which synthesis path ran rather than a failure: `tts_no_chunk_support`, `tts_chunking_disabled`. `stall_warm_disabled` / `stall_warm_skipped` / `stall_warm_failed` are the same kind of record for the startup pre-synth.

**Streaming STT (`stt_stream.py`, `ws.py` — all logged, not sent as frames; a lost partial is an optimization lost, never a turn lost):** `stt_stream_disabled` (the default; says nothing and is not logged), `stt_stream_unsupported_provider` (`PET_TALK_STT_STREAM=1` on a tyre whose `supports_streaming` is `False` — refused by name, never downgraded), `stt_stream_off` (named once per SOCKET, not per chunk), `stt_stream_started`, `stt_partial_failed`, `stt_partial_callback_failed`, `stt_stream_cancelled` (`reason=barged` — a barge cancels the stream session as well as the turn), `superseded_by_new_turn`, `stt_provider_swapped_mid_utterance`, `stt_stream_inflight_wait_expired`, `stt_stream_no_partial` (fell back to the whole-utterance pass, by name), `stt_stream_len_mismatch` (the caller's `pcm_b64` is a different length from the stream's buffer, so `_partial_offset` is not a valid seam — the whole utterance is decoded instead), `stt_stream_early_stall_skipped` (`reason=no_partial` / `routed_direct`), `stt_stream_early_stall_abandoned` (`reason=barged_during_stall` — a barge landed during the filler's synth, so no filler is sent), `stt_stream_empty_final`, `stt_stream_stop_abandoned` (`reason=barged_before_finalize` / `barged_during_finalize`), `stt_stream_stop_failed` (the streaming path broke BEFORE a turn was started; the whole-utterance path runs), `stt_stream_stop_failed_after_commit` (it broke AFTER — the failure re-raises rather than starting a second turn for one utterance, and surfaces as `frame_handler_failed`).

**SpeakQueue (`speak_queue.py`):** `queue_closed`, `queue_no_spoken_sentence`, `queue_bad_word_idx`, `queue_close_failed`.

**Persona (`persona.py`, `routes_http.py`, `turn.py`):** `persona_unloadable`, `persona_no_name`, `persona_save_failed`, `persona_protected`, `persona_no_stalls`, `unknown_persona`.

**Voices (`voices.py`):** `voices_not_found`, `voices_bad_yaml`, `voices_bad_voice`.

**Grounding (`grounding.py`, swallowed — logged, never sent to the client):** `grounding_repo_missing`, `grounding_subprocess_timeout`, `grounding_subprocess_missing`, `grounding_subprocess_failed`, `grounding_subprocess_nonzero`, `grounding_subprocess_kill_failed`, `grounding_subprocess_unreaped`, `grounding_subprocess_reap_failed`, `grounding_repo_task_failed`, `ax_bad_json`, `ax_not_ok`, `ax_empty_context`, `ax_task_failed`.

**Eyes (`eyes.py`):** `eyes_too_large`, `eyes_bad_kind`, `eyes_ocr_failed`, `eyes_no_text`, `eyes_disabled`, plus `eyes_import_failed` (swallowed at import time in `ws.py`, surfaces as `eyes_disabled` to the client).

**Receipts (`receipts.py`, `receipts_archie.py`):** `receipt_missing` (the claim is about work and nothing in the index proves it), `receipt_stale_index` (the scan predates HEAD, so a blame claim is refused and never spoken), `receipts_unavailable` (the `archie` binary is missing, exits non-zero, times out, or prints something that is not the JSON we asked for). Each carries `spoken`, the Noun-Rule line to say instead of the claim.

**HTTP-only reasons (`routes_http.py`, JSON body not `agent.error`):** `bad_request`, `not_found`, `audio_not_found`, `ledger_clear_failed`, `invalid_base64` (`/transcribe`), `empty_audio` (`/transcribe`).

**Env parsing (swallowed, logged only):** `bad_vad_silence_ms`, `bad_vad_silence_ms_env`, `bad_turn_delay_env`.

## 5. Provider contract

Every provider is a config swap: `STT_PROVIDER` / `LLM_PROVIDER` / `TTS_PROVIDER` at boot (env), or a live `POST /settings` at runtime. No code path branches on which provider is live.

### 5.1 Sync methods

```
STTProvider.transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> str
STTProvider.supports_streaming: bool = False   # class attribute, not a method
LLMProvider.route(text: str) -> "stall" | "answer"     # deterministic keyword routing, no LLM call
LLMProvider.stream(messages: list[dict]) -> AsyncIterator[str]   # yields whole sentences
TTSProvider.synth(text: str, voice: str, speed: float) -> tuple[bytes, list[dict]]   # (wav_bytes, word_times)
```

`supports_streaming` says whether a tyre can be called repeatedly on a growing
prefix of ONE utterance cheaply enough to be worth it. `server/stt_stream.py`
reads that flag and nothing else — it never infers capability from a class name,
and `PET_TALK_STT_STREAM=1` on a tyre that says `False` is refused by name
(`stt_stream_unsupported_provider`), never quietly downgraded. Today:

| tyre | `supports_streaming` | why |
|---|---|---|
| `stub`, `faster-whisper`, `whisper-local` | `true` | in-process, model loaded once |
| `groq`, `deepgram`, `openai`, `sensevoice` | `false` | one HTTPS round trip per window, billed per call |
| `mlx` | `false` | reloads the weights on every call (§10) |
| `whisperkit` | `false` | a process spawn plus a model load per window |

Deepgram's real streaming socket would belong behind this flag instead of
repeated POSTs; it is deliberately out of scope (2026-09-12), which fixed the
local default first.

One optional async method, on tyres that can stream. It is deliberately NOT on
the ABC: a backend that can only return a finished file stays legal rather than
carrying a fake implementation, and a fake stream is worse than an honest whole
WAV. `speak_sentence` probes for it with `getattr` and names the reason when it
is absent.

```
TTSProvider.synth_chunks(text: str, voice: str, speed: float,
                         cancel: threading.Event | None = None)
    -> AsyncIterator[tuple[bytes, list[dict], bool]]   # (standalone_wav, word_times, final)
```

Today `kokoro-local` is the only tyre that implements it (`stub-chunked` exists
so the wire path is provable hermetically). It works because `mlx_audio`'s
`model.generate` is already a generator that phonemizes per segment, so
synthesizing a four-word clause costs four words of work.

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

### 6.1 Studio token and the egress allowlist

Every mutating HTTP route (`POST`/`DELETE`) and the `/ws` handshake require the header `X-Studio-Token` (`server/auth.py`). Reads (`GET /health`, `/voices`, `/audio/{id}`, and the `GET` forms of `/settings`, `/personas`, `/ledger`) stay open — they carry no credential and a client needs them before it has read the token.

- **Three sources, checked in order**: env `STUDIO_TOKEN` → the file named by env `STUDIO_TOKEN_FILE` → a token the server generates at startup into `<repo>/.qa-scratch/studio.token` (0600, gitignored; the path is logged once, the value never). `cli/client.py` and `web/src/ws.ts` resolve the token the same way — the CLI reads the same three sources from `__file__`'s repo root, the web cockpit falls back to `VITE_STUDIO_TOKEN` then whatever was saved into `localStorage` from the Settings modal's "Connect" field.
- **Header, or `?token=` for the WS handshake**: browsers cannot set headers on `new WebSocket()`, so `/ws?token=<token>` is accepted as well as the header. A missing/wrong token closes the WS handshake before `accept()` (surfaces to a client as the handshake being rejected, close code `4401` if it reaches an open frame) and any mutating HTTP route answers `401`.
- **Egress allowlist**: any `*_base_url` field in `POST /settings` must resolve to loopback, an RFC1918 address, a host named in env `PET_TALK_ALLOWED_HOSTS` (comma-separated), or one of the provider hostnames already hardcoded as defaults. Anything else is refused with `base_url_not_allowed` before the setting is applied — this is what stops a `POST /settings` from repointing a stored credential at an attacker-controlled host.

## 7. Grounding

`server/grounding.py` assembles a capped (`MAX_GROUNDING_CHARS = 460`) block injected into the system prompt every turn (`persona_runtime.build_system_prompt`). Three sources, gathered concurrently with `asyncio.gather`:

1. **Repo-relative git head lines.** Always this checkout's own root (`repo_root()`, derived from `__file__` — no absolute path baked in). Extra repos come from `PET_TALK_GROUNDING_REPOS`, a colon-separated list of paths, empty by default. Each is a 1s-timeout `git log -n1 --oneline` subprocess.
2. **AX screen snapshot** (`ax_snapshot()`), gated by `PET_TALK_AX=1`. When set, it shells out to `pet-talk-hotkey ax` (0.3s timeout) for one JSON line describing the frontmost app/window/selection. **This directly needs the macOS Accessibility permission** — the hotkey binary must be granted Accessibility access in System Settings for `ax` to return anything but a fast `ax_not_ok`. That contradicts any "zero TCC/Accessibility permissions" claim made elsewhere for the base hotkey daemon: the *hotkey itself* (Carbon `RegisterEventHotKey`) needs no Accessibility grant, but `PET_TALK_AX=1` grounding does. Say so plainly rather than paper over it — it's an opt-in trade, not a bug.
3. **Static lines** — currently one: `"Voice loop: pet-talk duplex, local-first tyres."`

All three are gathered even on failure (`return_exceptions=True`); a failed lookup is logged via `swallowed()` and contributes nothing rather than blocking the turn.

## 8. Eyes lane

`server/eyes.py`. A client attaches an image/screenshot/PDF over the existing `/ws` socket (`user.attach`); the server OCRs it locally and injects `[eyes:<ref>:<kind>|<task>] <text>` into the next turn's context. No pixels or vision tokens leave the machine.

**Context injection (fixed this pass, 2026-09-12).** `EyesProvider.context_tag` had existed since the lane landed but nothing called it — OCR text was read and then dropped before the prompt was built. Now: `handle_attach` queues the tagged line on the provider (`EyesProvider.queue_context`, capped independently by `PET_TALK_EYES_CONTEXT_MAX_CHARS`, default 1200 — separate from `EYES_CHAR_CAP`, which only bounds the outgoing `eyes.text` wire frame) as soon as OCR resolves. `server/turn.py::handle_turn` drains that queue once per turn (`EyesProvider.take_context`) and folds it into the system prompt as an `[EYES CONTEXT]` block (`server/persona_runtime.py::build_system_prompt`'s `eyes_context` param) — so an attachment's text reaches exactly the next turn and never a later one; `TurnLog.mark("eyes_ocr_ms")` records the checkpoint on any turn that carried pending eyes context. Tested hermetically in `qa/test_eyes.py` (`TestContextQueue`, `TestEyesContextReachesTheTurnPrompt`) with the stub OCR engine and a capturing stub LLM; a `PET_TALK_REAL_ENGINE=1`-gated variant does the same against the real `zrv` CLI on the PDF fixture.

**Engines that actually exist** (`make_engine`, `EyesConfig.engine`, default `zrv`):
- `zrv` — shells out to the real `zrv` CLI. `zrv`'s own `--engine` ids (verified against `zrv --help` 2026-09-12): `apple-vision`, `apple-fm`, `local-vlm`, `cloud-vlm`, `tesseract`. `apple-vision` is zrv's local default on Apple silicon and is **OCR-only** — it refuses `--task describe`.
- `stub` — deterministic canned text, used by `qa/test_eyes.py`; never touches the filesystem or a subprocess.

An unknown engine name fails closed (`make_engine` raises `EyesError(eyes_disabled, unknown_eyes_engine:<name>)`) — there is no silent fallback to `stub` or `zrv`.

**Describe-mode measurement (2026-09-12):** the only local engine that can actually run `--task describe` is `apple-fm` (`apple-vision` refuses it in ~0.2s with a clean error). `apple-fm` was measured at **101.9s for one 2280×600 PNG** — 12x the 8s `EYES_TIMEOUT_S` default and ~68x an earlier 1.5s target. Because of this, **`describe` ships disabled by default**: `EyesConfig.describe_engine_pin` is unset, so a `describe` task hits `apple-vision`'s fast, honest refusal instead of a 100+ second hang. Set `EYES_DESCRIBE_ENGINE=apple-fm` and raise `EYES_TIMEOUT_S` well past 100s if you want describe mode anyway. `EYES_DEFAULT_TASK` and persona frontmatter `eyes_task`/`eyes_default` both default to `transcribe`.

**Describe over the LiteLLM proxy — `fleet/vision` (stub, 2026-09-12).** The intended long-term describe route is `cloud-vlm` through the house LiteLLM proxy (`server/providers/llm.py`'s `LITELLM_BASE_URL`), once the router ships a `fleet/vision` alias — not shipped yet. `EYES_DESCRIBE_ENGINE=fleet/vision` resolves today through `resolve_fleet_vision_alias()`, which reads `FLEET_VISION_MODEL`: unset (the default), it fails closed as `eyes_disabled` naming `FLEET_VISION_MODEL`, same as any other unset describe pin; set, it substitutes that value as the zrv `--engine` pin (expected `cloud-vlm` once the router exists). Tested hermetically in `qa/test_eyes.py::TestFleetVisionAlias`.

Other knobs: `EYES_MAX_BYTES` (8MB decoded cap), `EYES_CHAR_CAP` (4000 chars before `truncated:true` on the `eyes.text` frame), `PET_TALK_EYES_CONTEXT_MAX_CHARS` (1200 chars before the queued *prompt* line truncates, independent of `EYES_CHAR_CAP`), `EYES_TIMEOUT_S` (8.0s OCR wall clock), `EYES_ZRV_BIN`, `EYES_SCRATCH_DIR` (system temp dir, never inside the repo). A PDF is always `transcribe` (`task_for`), capped at `pdf_max_pages=5`.

## 8a. Receipts lane (Archie / AgentWorth)

No spoken claim about work stands without a receipt. A sentence that claims
tests passed, a commit landed, a file changed, or that a session wrote
something either emits `agent.receipt` or is refused by name. It is never
spoken bare.

`receipts.py` classifies the spoken sentence (a keyword set plus a persona
hint, no network), then asks AgentWorth what proves it. `receipts_archie.py`
shells the **`archie` CLI with `--json`** — never the MCP server, because the
voice loop has no coding agent in it to host one. Nothing but ids, paths and
token counts is read out of the JSON; transcript content never enters a frame.

```json
{
  "type": "agent.receipt",
  "turn_id": "t12-9ab3f1",
  "claim": "A Codex session wrote server/speak_queue.py, and its commit proved nothing.",
  "source": { "kind": "session|commit|scan", "id": "d01eaae", "repo": "unfoundbox-crew/pet-talk", "path": "server/speak_queue.py" },
  "tokens": 18400,
  "cost_usd": null,
  "index_age_s": 420,
  "fresh": true
}
```

`cost_usd` is always `null` today. archie 0.1.23's `repo blame`, `repo
suspect`, `session wake` and `session list` JSON all report tokens and no
dollar figure (probed 2026-09-12). A computed price would be a number nobody
measured, so the field stays null until AgentWorth publishes one. Reported
upstream, not patched from here.

**The freshness law.** `archie session wake --json` carries
`index.last_scanned_at`: when the index last took a write. If that predates
the repo's HEAD commit time, the index cannot have seen that commit's work.
`fresh` is then `false`, `behind_s` is how far behind, and every blame claim
is refused with `receipt_stale_index` and the spoken line *"My index is N
minutes behind the last commit. Rescan?"* — never an answer and never a
guess. A session recall is not refused for staleness, because recalling what
a past session did stays true when the scan is old; its receipt carries
`fresh: false` so the cockpit shows the age.

**Two voice routes**, both returning `Answer(spoken, receipt, refused)`:

| Said | Answered from | Refused when |
| --- | --- | --- |
| "who broke `<thing>`" | `repo blame` for the session and its tokens, `repo suspect` for the unproven commit | stale index, no blame row, no archie |
| "where was I with `<lane>`" | `session wake` — the checkout's carry-forward | no session for this checkout, no archie |

`receipts.answer_voice_route(text)` matches both and returns `None` for
anything else, so the deterministic router can call it before the LLM.

Config only, no code change (law 2): `ARCHIE_BIN` (default: `archie` on
`PATH`), `ARCHIE_REPO` (default: cwd), `ARCHIE_TIMEOUT_S` (default 8),
`RECEIPTS_ENABLED` (default 1; `0`/`false`/`no` switches the lane off).

**The chip.** `web/src/components/ReceiptChip.tsx` renders the receipt under
the spoken line: source chip (click copies the full session or commit id),
file path, index age, and the token total. AgentWorth's accent (violet) is
allowed on the total line and nowhere else, and Archie never appears on a
receipt — it is one of the three places their design doc forbids him. Both
rules are enforced by `qa/test_receipts.py`, not by convention.


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

### 9.1 MEASURED (2026-09-12f, streaming-STT default decision, turn-level)

**What this pass was for.** §9.2's `stall_sent` hook has since landed in
`server/turn.py` (`handle_turn_task`/`handle_turn` take `stall_sent: bool =
False`, guard is `if stall_text and not stall_sent:`). That was the one
missing piece §9.2 named for a turn-level re-measure of the early-stall path,
so this pass ran the real server (Kokoro + faster-whisper + the LiteLLM
proxy) both ways, `PET_TALK_STT_STREAM=0` and `=1`, to decide the default.

**Machine note.** The bounded 10-minute wait for 1-min load under 6 never
succeeded — load swung from ~7 up to a **peak 1-min of 435** (six-plus other
sessions building/running on this box) and was still ramping down when the
wait timed out at 10.27. Measurements below were taken as load fell through
the 6-14 range; every run's load is quoted next to its numbers, per
"never soften a budget" — this is not the quiet machine either.

**Turn-level, `APP_PORT=8089 python3 qa/latency.py`, N=5, x3 per mode, median
kept by `stall_ms` p50:**

| mode | run loads (1-min) | kept run load | `stall_ms` p50 | p95 | budget | result |
|---|---|---|---|---|---|---|
| `PET_TALK_STT_STREAM=0` | 8.80, 9.09, 8.98 (run 3 retried once after a `ws_send_disconnected` blip) | 9.09 | **223.1** | 367.0 | 400 | **PASS** |
| `PET_TALK_STT_STREAM=1` | 12.82, 12.28, 11.69 (run 3 retried once, same reason) | 12.82 | **194.3** | 919.4 | 400 | **FAIL** (p95 only) |

**The central finding: this harness cannot exercise the early-stall path at
all, in either mode.** `qa/latency.py` and `qa/live_ws_turn.py` both hand the
*entire* utterance to the server in one `user.stop` message — neither ever
sends a `user.chunk` frame. `server/ws.py`'s `_stream_stop` only serves
`user.stop` "from the turn's stream session, **if it has one**", and a stream
session is only created by `_on_user_chunk` when a `user.chunk` frame
arrives. With none sent, `_stream_stop` returns `False` every time and every
turn falls through to the identical whole-utterance path — confirmed by
`grep -c stream` on the `PET_TALK_STT_STREAM=1` server log (**0** matches)
and by `server/turns.jsonl`: every turn in both passes reads `"path":
"worker"`, never `"path": "stream"`. This is why the two modes' `stall_ms`
numbers are statistically indistinguishable (223.1ms vs 194.3ms, well within
this run-to-run noise band) — **the flag made no observable difference
because the mechanism it gates was never invoked**, not because the early
stall doesn't work (§9.2 already proved it does, component-level, with a
harness that does send `user.chunk`).

**`qa/live_ws_turn.py`** (also whole-utterance in one `user.stop`, same
limitation): time-to-stall 180.2ms streaming=0, 196.5ms streaming=1, both
PASS (budget 400) — barge ack 0.7-0.8ms both, both PASS.

**`qa/test_real_engine_e2e.py` (`PET_TALK_REAL_ENGINE=1`): 3/3 pass, both
modes.** Kokoro synthesized 278,444 bytes of valid RIFF both times;
`/transcribe` read back *"Good morning. Donna Paulson here. Executive
Secretary Mode is fully operational."* both times; a live `/ws` turn drove
Donna to a playable WAV both times (280,844 bytes streaming=0, 228,044 bytes
streaming=1 — text differed slightly turn to turn, as the LLM is not
seeded, but both were coherent, on-topic answers).

**Final-transcript comparison, same clip, streaming vs not** (N=3 each,
`qa/fixtures/weather_turn.wav`, captured via the `transcript.user` frame):
transcript text was **identical in all 6 turns** — `"What is the weather
today?"` — zero word-error difference. This is expected given the finding
above (both modes ran the identical whole-utterance decode) and is not
evidence either way about the streaming path's accuracy cost; that question
is still open per §9.2's finalize-seam note.

**Decision (§9.2 follow-up): `PET_TALK_STT_STREAM` stays `0`.** Not because
streaming failed a budget this pass — it was never actually driven — but
because this pass cannot honestly claim to have proven it, and flipping a
default on a measurement that didn't exercise the code path would be a fake
green. The real gap is now named precisely: a turn-level harness that speaks
`user.chunk` frames (pacing PCM the way `cli/client.py` does, the way
`qa/test_stt_streaming.py` already does at component level) against the live
Kokoro+faster-whisper+LiteLLM server is what's still missing before this
default can be revisited. §9.2's component-level evidence (early-stall
`stall_ms` 0.2-0.8ms p50 even at load 88) stands unchanged and is still the
only real evidence the mechanism works.

### 9.1-history: MEASURED (2026-09-12d, quiet re-measure, load ~7)

**The 12c "quiet re-measure owed" caveat, followed up — only partially resolved.**
`uptime` read 3.93 (1-min) before this pass started. By the time the turn-level
runs actually executed, two other `opencode` lanes had spun up at 200%+ CPU
each and load had risen to 6-20; a 10-minute bounded wait (per the job's own
cap) never got it back under 4 — the best point seen was 4.28, and the three
kept `qa/latency.py` runs below ran at 1-min load 6.19, 7.48, and 6.26. This is
**not the quiet machine the 12c caveat asked for**, but it is a real ~25x drop
from 12c's peak of 186, and the result is informative anyway: `stall_ms`
improved (695.6ms → 642.2ms p50) but is **still well over budget**, which means
the 12c diagnosis — "this is 100% machine load, nothing in this pass touched
STT" — was too optimistic. The dominant component, STT, is sensitive to *any*
concurrent CPU contention (Kokoro's synthesis threads included), not only to
extreme load. See the stage receipt below.

Component-level TTS/STT was not re-run in isolation this pass (no isolated
provider-call script was executed); the numbers below are turn-level
(`qa/latency.py` x3, median kept) plus the server's own per-stage telemetry
ledger (`server/turns.jsonl`).

**Turn-level** — live WS through the real FastAPI loop, `APP_PORT=8089 python3
qa/latency.py` run three times at N=5 turns; median run kept (1-min load 7.48
at the moment it ran):

| Metric | p50 (ms) | p95 (ms) | Budget (ms) | Result |
|---|---|---|---|---|
| `first_sentence_ms` / `first_audio_ms` | 642.4 | 758.9 | 800 (`turn_p50_ms`) | **PASS** |
| `cold_first_audio_ms` — first turn of a fresh process | — | 550.5 | 1200 | **PASS** |
| `barge_ms` | 1.0 | 1.2 | 100 | **PASS** |
| `stall_ms` | **642.2** | 758.7 | 400 | **FAIL** |

**The `stall_ms` receipt, from the turn's own telemetry** (`server/turns.jsonl`,
turn `t-latency-2` of the kept run): `stt 590.0ms`, `stall 639.3ms`. The stall
lands **49.3ms after STT finishes** — the warm stall cache is still working
exactly as designed. The whole of the breach is the STT stage. That stage was
164.1ms p50 in isolation on the same machine in 12c and 143.4ms on the fully
quiet 12b machine; here, inside the server, with two other lanes' processes on
CPU, it ran 495-870ms across the 15 turns in the three kept-set runs. **STT
inside this server is not resilient to ordinary background CPU contention**,
Kokoro's own synthesis threads included — this is a real property of the stack,
not only a load-186 artifact.

`cold_first_audio_ms` passed on all three post-wait runs: 192.1, 550.5, 553.5ms.
`warm_stall_cache` is still doing its job — the stall never has to pay a
cold synth.

**Other receipts from this pass:**

* **`qa/live_ws_turn.py`, one run:** barge ack latency 1.9ms (budget ≤100,
  PASS), time-to-stall 218.9ms (budget ≤400, PASS) — this harness drives the
  worker in-process against a stub LLM, so it does not see the live-server STT
  contention above; it proves the queue/cancellation path, not turn-level
  latency.
* **`qa/test_real_engine_e2e.py` (`PET_TALK_REAL_ENGINE=1`): 3/3 pass.**
  kokoro-local synthesised 278,444 bytes of valid RIFF; `/transcribe` read it
  back as *"Good morning. Donna Paulson here. Executive Secretary Mode is fully
  operational."*; a live `/ws` turn drove Donna through the proxy to 228,044
  bytes of playable audio.

**No budget was softened.** `stall_ms` still reads FAIL against its written
value, with the dominant-component diagnosis above rather than a rewritten
number.

**How the server was started for this measurement:**

```bash
TTS_PROVIDER=kokoro-local STT_PROVIDER=faster-whisper LLM_PROVIDER=litellm \
  PET_TALK_TTS_CHUNKS=1 PET_TALK_STALL_WARM=1 PET_TALK_SILENT=1 \
  doppler run --project unfoundbox --config dev_personal -- \
  ~/miniconda3/envs/local-ml-py311/bin/python -m uvicorn server.app:app \
    --host 127.0.0.1 --port 8089
```

`LITELLM_BASE_URL`/`LITELLM_MASTER_KEY` come from the Doppler scope, not the
tree. `TTS_PROVIDER`/`STT_PROVIDER`/`LLM_PROVIDER` name the tyres; the loopback
literal in `providers/_shared.py` is only a last-resort fallback.

**Stub-provider numbers, for the harness not the product.** `qa/latency.py` with
all three tyres stubbed: `stall_ms` 18.1ms p50, `first_sentence_ms` 18.1ms,
`barge_ms` 0.5ms. Hermetic slow-TTS barge test: barge kill at 0.8ms,
`dropped=4`, queue high-water 3. These exercise the queue, the frame plumbing
and the cancellation path — not any backend's real latency. Treat them as "the
harness is honest," never as user-facing latency.

### History: 2026-09-12c (real providers, real speech, live WS loop, chunked TTS)

**Read the machine note before any number below.** Three other P0 lanes were
building and testing on this same MacBook throughout this pass. Load average hit
186 at the worst point and sat at 16-20 during the runs recorded here, against a
load of roughly 2 for the 2026-09-12b pass. Every turn-level number here is a
**ceiling, not this stack's capability**, and the two regressions against 12b
(`stt_ms`, `stall_ms`) are machine load rather than code — the stage receipt
below proves where the time went. A clean re-measure on a quiet machine is owed.
(**Followed up 2026-09-12d, above**: partially confirmed. `stall_ms` improved
but did not clear budget even at load ~7, so STT's contention-sensitivity is not
purely a load-186 pathology.)

**What this pass changed.** `tts_ms` was failing structurally: every backend
returned one complete WAV per sentence, so nothing could arrive before the whole
sentence was synthesized, and at Kokoro's RTF any sentence past ~9 words was
over budget by construction. Chunked synthesis (§4.2.1) fixes the shape rather
than the provider: the sentence is split at clause boundaries, each clause is
synthesized and sent as it is produced, and what has to fit inside 200ms is one
clause. And the cold first turn no longer pays a stall synth — `warm_stall_cache`
pre-synthesizes the persona's stalls at startup.

**Component-level** — direct provider calls. The TTS sentence is 13 words (the
earlier brief called it 12; it is 13 by plain `split()`).

| Component | p50 (ms) | p95 (ms) | Budget (ms) | Result |
|---|---|---|---|---|
| `tts_ms` — first playable audio of a sentence (chunk 0, kokoro-local, N=9) | **127.3** | 246.1 | 200 | **PASS** at p50, p95 clips |
| the same sentence whole (`PET_TALK_TTS_CHUNKS=0`, N=9) | 303.1 | 548.1 | 200 | **FAIL** |
| `stt_ms` (faster-whisper tiny.en, local, N=5) | 164.1 | 285.6 | 150 | **FAIL** — was 143.4 on the quiet machine |
| `llm_ms` first content delta | — | — | 800 | **NOT MEASURED** this pass; unchanged from 12b (512.4 / 805.4) |

**The first chunk cannot usefully be made smaller.** Measured, same run:

| words in chunk 0 | first-chunk p50 (ms) | p95 (ms) | whole-sentence total (ms) |
|---|---|---|---|
| 6 | 249.8 | 675.0 | 583.7 |
| **4 (the default)** | **127.3** | **246.1** | **409.5** |
| 3 | 136.7 | 213.0 | 573.8 |
| 2 | 204.1 | 389.8 | 950.3 |

There is a floor of roughly 110ms of fixed per-call overhead, which is why the
default chunk-0 size is 4 words, not 2.

**Turn-level** — live WS, three runs at N=5, median kept:

| Metric | p50 (ms) | p95 (ms) | Budget (ms) | Result |
|---|---|---|---|---|
| `first_audio_ms` | 696.0 | 775.8 | 800 | **PASS** |
| `turn_worst_ms` (cold) | — | 433.9 | 1200 | **PASS** — was 1455.3 |
| `barge_ms` | 1.1 | 1.2 | 100 | **PASS** |
| `stall_ms` | 695.6 | 775.6 | 400 | **FAIL** — machine load, see receipt |

Stage receipt, turn `t-latency-4`: `stt 700.3ms`, `stall 748.2ms` (47.9ms after
STT). `qa/live_ws_turn.py` three single-shot runs: 387.8 PASS, 304.0 PASS, 703.7
FAIL. `qa/test_real_engine_e2e.py` 3/3 pass: 278,444 bytes RIFF from Kokoro,
210,044 bytes of playable audio from a live `/ws` Donna turn. Backward
compatibility: `GET /audio/t-live-qa-1-s1` → 200, 272,444 bytes, re-muxed
whole-sentence WAV intact for clients that ignore `agent.chunk`.

**The 2026-09-12b pass**, taken on a quiet machine before chunked synthesis, is
kept in `qa/budgets.json` under `measured.history` with its own history beneath
it. Its headline numbers: `tts_ms` 236.6 p50 **FAIL**, `stall_ms` 245.5 **PASS**,
`first_sentence_ms` 245.5 **PASS**, `stt_ms` 143.4 **PASS**, cold first turn
1455.3 **FAIL**.

### 9.2 MEASURED: streaming STT vs the whole-utterance pass (2026-09-12e, load 42-88)

**What this measures.** 9.1 named the `stall_ms` breach precisely: of 642.2ms
p50, `stt` was 590.0ms — one `faster-whisper` pass over the WHOLE utterance,
started only once `user.stop` arrived, with the stall landing 49.3ms behind it.
`server/stt_stream.py` changes the shape: `user.chunk` frames feed a rolling
window that decodes the prefix in a worker thread every
`PET_TALK_STT_STREAM_MS` (default 700) of new audio, so at end-of-speech a
partial transcript already exists, `agent.stall` goes out from it, and only the
TAIL is decoded to finalize.

**Harness and its limits, stated up front.** `qa/test_stt_streaming.py`
(`PET_TALK_REAL_ENGINE=1`), component-level: the real `faster-whisper tiny.en`
tyre driven through the real `_on_user_chunk` / `_stream_stop` handlers with
20ms chunks paced in REAL TIME (what `cli/client.py` sends), stub TTS and stub
LLM. This is **not** a turn-level re-measure of 9.1 — it does not include
Kokoro or the proxy, so its "streaming OFF" column is NOT comparable to 9.1's
642.2ms. What it compares is the one stage this lane changed, on one clock, in
the same process, back to back. Clip: `qa/fixtures/weather_turn.wav` repeated to
**2.88s** (the 1.44s fixture alone is shorter than two windows). N=5 per mode
plus a discarded warm-up turn, five runs kept below.

**The machine was far from quiet: 1-min load 42 to 88**, against ~7 for the 9.1
pass — six other lanes were building in this worktree. That makes the absolute
numbers a ceiling, and it makes the ON/OFF comparison *more* informative, not
less, because contention is exactly what 9.1 found this stage cannot survive.

| mode | `stall_ms` p50 | `stall_ms` p95 | budget | result |
|---|---|---|---|---|
| streaming ON, **early stall** | **0.2 - 0.8** | **8.8 - 39.7** | 400 | **PASS**, every run |
| streaming ON, tail-only (no early stall) | 537.9 - 808.8 | 1104.9 - 1313.4 | 400 | **FAIL** |
| streaming OFF (today's shape) | 209.2 - 542.1 | 426.9 - 1563.6 | 400 | **FAIL** |

Five runs, at 1-min load 42.3 / 52.4 / 53.4 / 74.3 / 88.2. Raw per-turn samples
are printed by the test and not summarised away.

**What the numbers say, including what they do not say.**

* **The early stall is the whole fix, and it is structural.** With
  `agent.stall` emitted from the partial before finalization, `stall_ms` stops
  being an STT number at all — 0.2-0.8ms p50 is a warm cache lookup and a frame
  send. It PASSED at load 88, which no amount of tuning the old shape has ever
  done at load 7.
* **Tail-only streaming is NOT a win — it is a regression at load.** Left with
  `turn.py` owning the stall, the stall still waits for the transcript, and the
  transcript now waits on `PET_TALK_STT_STREAM_WAIT_MS` (1200ms) for an
  in-flight prefix decode: 537.9-808.8ms p50, worse than streaming off. At
  load 50+ a prefix decode takes longer than the 700ms window, so one is almost
  always in flight at `user.stop`. Dropping the wait instead puts two
  `faster-whisper` decodes on the CPU at once and produced a 2534ms outlier when
  measured that way. **So streaming without the early stall must stay off**, and
  `PET_TALK_STT_STREAM` defaults to `0` for exactly that reason.
* **The final transcript is not improved.** 411.9-486.8ms p50 with streaming on
  against 362.5-541.8ms off, with fatter tails (up to 2136ms) from the in-flight
  wait. Streaming buys the STALL off the critical path; it does not make the
  ANSWER start sooner. Any claim that it does would be a fake green.
* **`stall_ms` in 9.1 is not yet re-measured turn-level.** ~~That needs a live
  server pass with Kokoro and the proxy, on a machine at load ~7, and it needs
  the one-line hook below.~~ **Follow-up 2026-09-12f (§9.1):** the hook landed
  and a live-server pass ran both modes at load 9-13, but turn-level
  `stall_ms` is **still NOT MEASURED** for the streaming path — one more gap
  turned up: `qa/latency.py` and `qa/live_ws_turn.py` both hand the whole
  utterance to one `user.stop` message and never send `user.chunk`, so
  `_stream_stop` never gets a stream session to serve and every turn falls
  through to the whole-utterance path regardless of the flag. A harness that
  paces `user.chunk` frames against the live server (the way this file's own
  `qa/test_stt_streaming.py` already does component-level) is what's still
  needed.
* **`STT_PROVIDER=whisperkit` is still NOT MEASURED.** `whisperkit-cli` is not
  on PATH and no CoreML model is on disk, so nothing has ever run on the Neural
  Engine here; the model plus CLI is far over the 200MB this lane was allowed to
  download. Its latency remains unknown — the belief that the ANE must be
  fastest is still a belief. It is also flagged `supports_streaming = False`
  because it spawns a process and reloads its model per call.

**The one line this needed from `server/turn.py` has landed** (2026-09-12f):
`handle_turn_task` and `handle_turn` take `stall_sent: bool = False`, threaded
through `_turn_pipeline`, and the worker path's guard is
`if stall_text and not stall_sent:`. `stt_stream.turn_accepts_stall_sent()`
reads the live signature and now returns `True`, so the early stall is emitted
and only one `agent.stall` reaches the person. The measured numbers above were
taken with that shape pinned by the test harness and are unchanged by the hook
landing. Streaming STT still ships OFF by default — see §10 for why that is a
measurement, not caution.

**How to reproduce:**

```bash
PET_TALK_SILENT=1 PET_TALK_REAL_ENGINE=1 STT_PROVIDER=faster-whisper \
  TTS_PROVIDER=stub LLM_PROVIDER=stub \
  ~/miniconda3/envs/local-ml-py311/bin/python \
  qa/test_stt_streaming.py TestRealEngineStreamingLatency
```

### 9.3 Acceptance gates and their proof

| Gate | Proven by |
|---|---|
| 1. Stall plays ≤400ms after user stops, on a routed question | `qa/latency.py` (`stall_ms` vs budget), `qa/test_turn_lifecycle.py`, `qa/test_stt_streaming.py` (§9.2 — PASSES only on the streaming early-stall path; the `stall_sent` hook it needs has landed, but that path still ships OFF by default and turn-level `stall_ms` for it is NOT MEASURED. The whole-utterance path, which is what ships, still FAILS) |
| 2. Barge-in kills audio ≤100ms and reports the true dropped count | `qa/latency.py` (`barge_ms`), `qa/test_turn_lifecycle.py`, `qa/test_socket_resilience.py` |
| 3. Worker streams ≥3 sentences behind playing audio without a gap | `qa/test_turn_lifecycle.py` — **partially exercised**: `StubLLM` emits exactly 3 sentences by default, and the queue high-water test uses a 4-sentence variant; there is no fixture that streams a long, unbounded answer to confirm the queue keeps buffering past 4-5 sentences under sustained LLM-faster-than-TTS pressure |
| 4. Provider swap via config only, zero code change | `qa/test_providers.py`, `qa/test_settings_api.py` |
| 5. Persona switch changes voice + stalls + tone, nothing else | `qa/test_persona.py`, `qa/test_persona_api.py` |

## 10. Known gaps

- **Streaming STT ships OFF.** `PET_TALK_STT_STREAM=1` plus the early stall measured `stall_ms` p50 0.2-0.8ms at load 88 (§9.2), and the `stall_sent` hook that path needs has since landed in `server/turn.py` — so the early stall now runs when the flag is on. It still ships off because streaming WITHOUT the early stall measured *worse* than streaming off (537.9-808.8ms p50) and turn-level `stall_ms` for the streaming path is NOT MEASURED (§9.2 follow-up): the default is `0` by measurement, not by caution. The finalize seam (prefix decode + tail decode) is also a real accuracy cost that is not quantified: nothing measures word error rate across the seam, and `PET_TALK_STT_FINAL_FULL=1` is the escape hatch rather than a measured comparison.

- **The speak queue is per-socket**, not per-turn: `Session.queue` is one `SpeakQueue` reused across turns via `reopen()`. Two turns on the same socket can never interleave (the newer one barges the older via `supersede()`), but this means there is exactly one live turn per socket at a time by design, not by accident.
- **Gate 3 (≥3 gapless sentences) is only partially exercised** — see §9.3. `StubLLM` hardcodes 3 sentences; nothing in `qa/` currently proves the queue keeps buffering ahead past a 4th or 5th sentence under sustained pressure.
- **PDF OCR now has a fixture and coverage (fixed this pass, 2026-09-12).** `qa/fixtures/eyes_sample.pdf` (a hand-built, stdlib-only, single-page real-text PDF) plus `qa/test_eyes.py::TestPdfOcr` — a hermetic path (`StubEngine`, always runs) and a `PET_TALK_REAL_ENGINE=1`-gated path that shells the real `zrv` CLI. Measured: `zrv ocr qa/fixtures/eyes_sample.pdf --task transcribe --json` returns `engine: apple-vision`, exact text match, in ~370ms — `zrv` reads the PDF's real text layer directly rather than rasterizing and OCRing, so there was no OCR noise to account for. `pdf_max_pages=5` (multi-page truncation) is still unexercised — this fixture is one page.
- **`EYES_DESCRIBE_ENGINE` now fails fast by name when unset (fixed this pass, 2026-09-12).** Previously an unset pin meant `describe` fell through to `zrv`'s own default engine (`apple-vision`, which refuses `--task describe` with its own unrelated error) instead of a clean `eyes_disabled`. `EyesProvider.resolve()` now raises `eyes_disabled` with detail `describe_engine_unset:EYES_DESCRIBE_ENGINE` before touching any engine when `task == "describe"` and `describe_engine_pin` is unset — tested hermetically in `qa/test_eyes.py::TestDescribeEngineGate` (unset fails closed naming the var; set routes through; transcribe is unaffected; an explicit `task: describe` on a PDF still hits the gate since a frame's explicit task wins over the PDF default per `task_for`'s own precedence).
- **Kokoro word timings are estimated, not measured.** `KokoroSpacePilotTTS` does not appear to report real per-word timestamps from the daemon; `word_times` for Kokoro-synthesized audio comes from `estimate_word_times()` (uniform-per-word or char-length-weighted), always carrying `estimated: true`. Any UI that highlights the currently-spoken word against Kokoro audio is highlighting a guess, not a measurement.
- **`cli/hotkey/build.sh` does not exist yet** in this checkout, though `Makefile`'s `build-hotkey` target and this repo's build comments both reference it as owned by another lane. `make build-hotkey` will fail until that script lands.
- **Fixed 2026-09-12b: the LLM could not stream text, and turn latency was unmeasurable.** Both are closed — see §9.1. The diagnosis in the previous pass ("`groq/compound-mini` cannot stream text") was wrong: reasoning models were never given enough token ceiling to reach `content`. `stall_ms`, `first_sentence_ms` and `barge_ms` are now measured live and all three PASS.
- **Fixed 2026-09-12c: `tts_ms` passes at p50 (127.3ms) via chunked synthesis, and clips the budget at p95 (246.1ms).** The wire-contract change the previous entry called for is made — `agent.chunk` plus `stream_url`/`chunked` on `agent.sentence`, §4.2.1. What remains is a tail, not a structural gap: the p95 is the same machine-load variance that shows up everywhere in the 12c pass. **Only `kokoro-local` streams.** Every cloud tyre still returns one finished file, so on deepgram, smallest or elevenlabs `tts_ms` is still the whole-sentence number and reads `chunked=false` with `tts_no_chunk_support` in the log. That is the honest state, not a fallback to hide.
- **Fixed 2026-09-12c: the stall cache is warm before the first turn.** `stall.warm_stall_cache` pre-synthesizes the default persona's stalls from an `on_event("startup")` task, and `turn_worst_ms` now passes on every cold run (215.7 / 495.9 / 433.9ms against 1200, from 1455.3). Two things worth knowing: the warm covers **only the default persona** — a first turn on a freshly switched persona still pays one synth per phrase — and it completes about 13s after boot, nearly all of it Kokoro's one-time model load, so a turn that arrives in the first few seconds of a fresh process still queues behind that load. `PET_TALK_STALL_WARM=0` disables it and logs the reason.
- **`word_times` across chunks are estimated twice over.** Each chunk's timings are estimated from that chunk's own duration and then shifted onto the sentence timeline (`_shared.shift_word_times`). The per-word split inside a chunk is as much a guess as it ever was, and the chunk boundaries are now also boundaries in the guess. A word-highlighting UI is no worse off than before, but it is not better either — real Kokoro timings are lane 11's job.
- **`agent.chunk` carries base64 on the WS, which is ~33% overhead per chunk.** Acceptable for clause-sized WAVs on loopback and deliberately chosen over a second HTTP round trip, but it is not the shape for a remote client. Each chunk's `url` is served too, and Opus/WebRTC streaming is lane 11's.
- **`pcm_duration_ms` believed a lying header until 2026-09-12c.** Deepgram's speak endpoint writes a streaming placeholder (`0x7fff0000`) into the WAV `data` chunk size, so a 5-second clip read back as ~12 hours and every word time derived from it was nonsense. The helper now cross-checks the header's frame count against the bytes actually present. Any other number computed from a Deepgram clip before this date is suspect.
- **`ElevenLabsTTS.synth` returned mp3 labelled as WAV until 2026-09-12c.** `/audio/{id}` served it as `audio/wav` and `pcm_duration_ms` could not parse it, so `word_times` came back empty. It now requests `pcm_24000` and wraps the PCM itself. The fix is **unverified against the live API** — this key returns HTTP 402 (no credit), so the request shape is right by the documented contract and not by measurement.
- **`GET /voices` does not list the active provider's voices.** It serves `personas/voices.yaml`. `providers/tts.py:provider_voices(provider)` is the function that enumerates a real tyre (reading Kokoro's 54 voices off the weights on disk), and it is not wired to a route yet — see docs/VOICES.md for the one-line hook.
- **`qa/live_ws_turn.py`'s stall gate is N=1 and flakes at about 1 run in 5** (measured: 183.4/199.2/213.7/319.5/401.3ms against a 400ms budget). It should take a median of a few turns the way `qa/latency.py` does; until then a single red run is not proof of a regression.
- **`kokoro-local` needs an interpreter that has `mlx-audio` and `misaki[en]`.** On this machine that is `~/miniconda3/envs/local-ml-py311`, not the base env — a server booted with the base `python3` gets a named `tts_mlx_audio_not_installed` from the first synth, not a silent fallback. Apple Silicon only.
- **The default STT is the least accurate setting in its family.** `tiny.en` with `beam_size=1` is what buys 143ms; it heard "Donna Paulsen" as "Donna Paulson" in the real-engine receipt. `WHISPER_MODEL=base.en` trades 190ms more for better words, without a code change.
- **`mlx-whisper` is not installed**, so `STT_PROVIDER=mlx` is untested here; it also reloads the model on every call, which would need the same load-once treatment `KokoroLocalTTS` got before it could be measured fairly. `whisperkit-cli` is not on PATH either.

## 11. A note on `cli/hotkey/*.swift`

These five files (`main.swift`, `hud_window.swift`, `earcons.swift`, `paste_injector.swift`, `config.swift`) may be under concurrent edit by another lane in this worktree. This spec describes their existence and role (native macOS Carbon hotkey listener, notch HUD, earcon engine, paste injector, config loader) per the module map in §3, but does not assert their current internal behavior in detail — verify against the live files before trusting a specific claim about them.
