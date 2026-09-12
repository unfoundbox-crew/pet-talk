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
| `user.handover` | client → server | `{turn_id, source}` | Hand-over chord (Option+Shift+Tab). Marks the next turn as delegated work and opens the mic. |
| `handover.received` | server → client | `{turn_id, source}` | Acknowledges `user.handover`; followed by `state.listening`. |
| `barge` | `turn_id?` | `_on_barge` — cancels the live turn(s), flushes the queue, returns the true dropped count |

An unrecognized frame type gets `agent.error` reason `unknown_frame` (detail: the type string, or `<missing type>`). Undecodable JSON gets reason `bad_frame`.

### 4.2 Server → client

| Frame | Fields | Emitted by |
|---|---|---|
| `state.idle` / `state.listening` / `state.thinking` / `state.speaking` | — | `ws.py`, `turn.py` |
| `transcript.user` | `text` | after STT, or immediately for `user.text` |
| `agent.stall` | `phrase_id`, `text` | `turn.py` worker path, before the LLM answer starts |
| `agent.sentence` | `seq`, `text`, `audio_url`, `word_times`, `estimated`, `stream_url`, `chunked` | `speech.py:speak_sentence` — one per spoken sentence |
| `agent.chunk` | `seq`, `chunk_no`, `audio_b64`, `url`, `final` | `speech.py:stream_chunks` — one per synthesis chunk, only on a chunk-capable tyre |
| `agent.done` | `path`, `sentences`, `dropped?` | end of every turn; `path` is one of `empty`, `control`, `control_cancel`, `worker`, `direct`, `interrupted`, `error` |
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
  chunk carries `final: true`.
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
| `PET_TALK_STALL_WARM` | `1` | `0` skips the startup stall pre-synth, and says so |

### 4.3 `agent.error` reason catalogue

Every reason a running server can actually emit today, grepped from `ProviderError(...)`, `EyesError(...)`, and direct `send_error(...)` calls. Grouped by where it originates.

**Frame/protocol (`frames.py`, `ws.py`, `turn.py`):**
`frame_no_turn_id`, `bad_frame`, `unknown_frame`, `empty_transcript`, `empty_text`, `frame_handler_failed`, `turn_task_exception`, `ws_send_disconnected`, `ws_send_after_close`, `ws_send_failed`, `ws_loop_failed`, `ws_shutdown_cancelled`, `ws_shutdown_flush_cancelled`.

**Providers, generic (`provider_factory.py`, `providers/_shared.py`):** `missing_api_key`, `provider_unreachable`, `provider_build_failed`, `provider_unavailable` (logged, not sent — see `/health`'s `degraded` list instead).

**LLM (`providers/llm.py`, `turn.py`, `speech.py`):** `llm_unknown_provider`, `llm_empty_messages`, `llm_stream_failed`, `llm_openai_not_installed`, `llm_route_failed` (swallowed, falls back to `route_text` — never surfaced to the client).

**STT (`providers/stt.py`, `turn.py`):** `stt_unknown_provider`, `stt_empty_audio`, `stt_empty_result`, `stt_bad_response`, `stt_request_failed`, `stt_no_key`, `stt_failed`, `stt_faster_whisper_not_installed`, `stt_whisper_not_installed`, `stt_whisperkit_failed`, `stt_mlx_failed`, `stt_mlx_not_installed`.

**TTS (`providers/tts.py`, `speech.py`, `stall.py`):** `tts_unknown_provider`, `tts_empty_text`, `tts_empty_audio`, `tts_synth_failed`, `tts_request_failed`, `tts_no_key`, `tts_no_token`, `tts_no_job_id`, `tts_job_failed`, `tts_job_timeout`, `tts_download_failed`, `stall_synth_failed`, `tts_chunk_format_mismatch`, `tts_chunk_not_wav`, `tts_cancelled`. Two more are telemetry only — logged by `speak_sentence`, never sent as a frame, because they describe which synthesis path ran rather than a failure: `tts_no_chunk_support`, `tts_chunking_disabled`. `stall_warm_disabled` / `stall_warm_skipped` / `stall_warm_failed` are the same kind of record for the startup pre-synth.

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
LLMProvider.route(text: str) -> "stall" | "answer"     # deterministic keyword routing, no LLM call
LLMProvider.stream(messages: list[dict]) -> AsyncIterator[str]   # yields whole sentences
TTSProvider.synth(text: str, voice: str, speed: float) -> tuple[bytes, list[dict]]   # (wav_bytes, word_times)
```

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

**Engines that actually exist** (`make_engine`, `EyesConfig.engine`, default `zrv`):
- `zrv` — shells out to the real `zrv` CLI. `zrv`'s own `--engine` ids (verified against `zrv --help` 2026-09-12): `apple-vision`, `apple-fm`, `local-vlm`, `cloud-vlm`, `tesseract`. `apple-vision` is zrv's local default on Apple silicon and is **OCR-only** — it refuses `--task describe`.
- `stub` — deterministic canned text, used by `qa/test_eyes.py`; never touches the filesystem or a subprocess.

An unknown engine name fails closed (`make_engine` raises `EyesError(eyes_disabled, unknown_eyes_engine:<name>)`) — there is no silent fallback to `stub` or `zrv`.

**Describe-mode measurement (2026-09-12):** the only local engine that can actually run `--task describe` is `apple-fm` (`apple-vision` refuses it in ~0.2s with a clean error). `apple-fm` was measured at **101.9s for one 2280×600 PNG** — 12x the 8s `EYES_TIMEOUT_S` default and ~68x an earlier 1.5s target. Because of this, **`describe` ships disabled by default**: `EyesConfig.describe_engine_pin` is unset, so a `describe` task hits `apple-vision`'s fast, honest refusal instead of a 100+ second hang. Set `EYES_DESCRIBE_ENGINE=apple-fm` and raise `EYES_TIMEOUT_S` well past 100s if you want describe mode anyway. `EYES_DEFAULT_TASK` and persona frontmatter `eyes_task`/`eyes_default` both default to `transcribe`.

Other knobs: `EYES_MAX_BYTES` (8MB decoded cap), `EYES_CHAR_CAP` (4000 chars before `truncated:true`), `EYES_TIMEOUT_S` (8.0s OCR wall clock), `EYES_ZRV_BIN`, `EYES_SCRATCH_DIR` (system temp dir, never inside the repo). A PDF is always `transcribe` (`task_for`), capped at `pdf_max_pages=5`.

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

### 9.1 MEASURED (2026-09-12, real providers, real speech, live WS loop)

**A real end-to-end turn is measured for the first time.** Yesterday's pass
recorded component numbers and NOT MEASURED for everything turn-level. Two
things blocked it, and both were bugs in our own tree rather than facts about
the providers:

1. **`qa/latency.py` sent 320 samples of silence.** Fine against stub STT,
   which answers regardless; against a real tyre it transcribes to nothing and
   the turn ends in `empty_transcript`, so every turn-level number it printed
   was the error path. Fixed: both live-turn scripts now send real speech from
   `qa/fixtures/weather_turn.wav` through one resolver (`qa/fixture_audio.py`),
   with the RIFF header stripped and the true `sample_rate` sent.
2. **"`LLM_PROVIDER=groq`'s default model cannot stream text" was the wrong
   diagnosis.** `groq/compound-mini` *can* emit `content`; it was never given
   room to. A reasoning model spends its token ceiling on `reasoning` deltas
   first, and at the ordinary 120-token ceiling it finishes with
   `finish_reason: "length"` having spoken nothing — reproduced on
   `openai/gpt-oss-20b` and `qwen/qwen3.6-27b` too. Fixed in
   `_build_payload` (ceiling floored at 400 for the reasoning family,
   `reasoning_effort: "low"` for gpt-oss), and `_sentence_stream` now raises
   `llm_no_content` instead of returning an empty generator, so a provider
   that cannot speak can never again look healthy.

Chosen defaults, all three swapped on measurement:

| tyre | default | why |
|---|---|---|
| STT | `faster-whisper` (`tiny.en`, tuned) | the only tyre inside 150ms from this machine |
| LLM | `litellm` @ `gpt-oss-120b-groq` | the house proxy route, and inside 800ms |
| TTS | `kokoro-local` (in-process Kokoro-82M) | 5x the daemon, and the closest to 200ms |

**Turn-level** — live WS through the real FastAPI loop, `APP_PORT=8089 python3
qa/latency.py` run three times at N=5 turns, median of the three runs:

| Metric | p50 (ms) | p95 (ms) | Budget (ms) | Result |
|---|---|---|---|---|
| `stall_ms` | 245.5 | 291.1 | 400 | **PASS** |
| `first_sentence_ms` | 245.5 | 291.2 | 800 (`turn_p50_ms`) | **PASS** |
| `barge_ms` | 0.9 | 1.5 | 100 | **PASS** |

**Component-level** — direct provider calls, N=5, same fixture for STT, a
12-word sentence for TTS:

| Component | p50 (ms) | p95 (ms) | Budget (ms) | Result |
|---|---|---|---|---|
| `stt_ms` (faster-whisper tiny.en, local) | 143.4 | 148.4 | 150 | **PASS** |
| `llm_ms` first content delta (proxy → gpt-oss-120b) | 512.4 | 805.4 | 800 | **PASS** at p50 |
| `tts_ms` (kokoro-local, 12-word sentence) | 236.6 | 262.3 | 200 | **FAIL** |
| `turn_worst_ms` (warm) | — | 291.2 | 1200 | **PASS** |
| `turn_worst_ms` (first turn of a fresh process) | — | 1455.3 | 1200 | **FAIL** |

Recorded in `qa/budgets.json` under `measured`, with the previous pass kept
beneath it as `measured.history`. No budget was softened; the three that do not
hold are diagnosed below and stay at their written values.

**What every candidate measured.** The defaults above are the survivors, not
the only things tried:

| STT (same 1.44s clip) | p50 (ms) | | TTS (12-word sentence) | p50 (ms) |
|---|---|---|---|---|
| faster-whisper `tiny.en` tuned | **143.4** | | **kokoro-local** (in-process) | **236.6** |
| faster-whisper `tiny.en` before tuning | 193.1 | | kokoro daemon, adaptive poll | 1379.3 |
| groq `whisper-large-v3-turbo` | 306.1 | | kokoro daemon, flat 0.5s poll | 1553.7 |
| groq `whisper-large-v3` | 331.1 | | deepgram `aura-2-thalia-en` | 1939.4 |
| faster-whisper `base.en` | 332.6 | | deepgram `aura-asteria-en` | 1640.0 |
| deepgram `nova-3` | 1432.3 | | | |
| deepgram `nova-2` | 1436.3 | | | |
| `mlx-whisper` | not installed | | | |

| LLM (first content delta) | p50 (ms) | note |
|---|---|---|
| groq `openai/gpt-oss-20b` | 499.0 | fastest overall; the new `groq` default |
| **litellm `gpt-oss-120b-groq`** | **512.4** | the new default — house proxy route |
| groq `openai/gpt-oss-120b` | 554.0 | |
| litellm `claude-sonnet-4-6` | 1180.3 | over budget |
| litellm `gemini-3.7-flash` | — | HTTP 429, the proxy's Gemini key is out of quota |
| litellm `claude-sonnet-5` / `claude-fable-5` | — | HTTP 500, Bedrock: model not enabled |
| litellm `gpt-oss-120b-cerebras` | — | HTTP 402, payment required |
| groq `qwen/qwen3.6-27b`, `nim-gpt-oss-20b` | — | reasoning deltas only, at any ceiling |

`gemini-3.7-flash` was the intended default and is rejected on evidence, not
taste.

**The three FAILs, diagnosed. None of the budgets moved.**

* **`tts_ms` 236.6ms against 200ms.** Down from 1285.9ms, and the remaining gap
  is structural rather than a tuning miss. Kokoro's real-time factor here is
  ~0.06, so a 12-word sentence (4.05s of audio) costs ~240ms to synthesise in
  full, and the `agent.sentence`/`audio_url` contract is one finished WAV per
  sentence. Every sentence past about nine words is therefore over budget by
  construction. Meeting 200ms on the 20-word ceiling (`MAX_SENTENCE_WORDS`)
  needs chunked synthesis that streams audio as it is produced — a change to
  the wire contract, not to a provider. Worth noting what the budget is *for*:
  the first thing a person hears is the persona's stall phrase, which is 5
  words (119ms p50) and is served from the RAM stall cache at zero synth cost
  on every turn after the first. That is why `first_sentence_ms` passes at
  245ms while `tts_ms` fails.
* **`turn_worst_ms` 1455.3ms on the first turn of a fresh process.** That is
  one cold stall-cache miss: the first turn synthesises its stall phrase, every
  later turn reads it from RAM. Warm worst is 291.2ms. A pre-warm of the
  persona's stall phrases at startup would remove it; `kokoro-local` already
  warms the model this way, the stall cache does not yet.
* **`llm_ms` p95 805.4ms against 800ms.** p50 is 512.4ms. The tail is the
  proxy hop plus Groq queueing; going direct to Groq (`LLM_PROVIDER=groq`,
  499ms p50 / 513.7ms p95) clears it with room, at the cost of leaving the
  house route. Recorded rather than resolved — the proxy stays the default.

**`qa/live_ws_turn.py`, five runs of the same single-shot stall gate:** 183.4,
199.2, 213.7, 319.5, 401.3ms. Four inside the 400ms budget, one 1.3ms over. The
gate is N=1, so it will flake at roughly that rate until it takes a median of a
few turns like `qa/latency.py` does. `barge_ms` was 0.9-1.3ms on every run,
`dropped` reported as a number, and the `audio_url` of the first worker
sentence returned 200 `audio/wav`.

**`qa/test_real_engine_e2e.py` (PET_TALK_REAL_ENGINE=1): 3/3 pass.** Receipts:
`kokoro-local` synthesised 278,444 bytes of valid RIFF; `/transcribe` read that
audio back as *"Good morning. Donna Paulson here. Executive Secretary Mode is
fully operational."* (the real text says "Paulsen" — greedy `tiny.en` is the
weakest setting in the family, which is the accuracy cost of the 143ms named
plainly); and a live `/ws` turn drove the Donna persona through the proxy to
148,844 bytes of playable audio.

**How the server was started for this measurement** (the interpreter matters —
`kokoro-local` needs `mlx-audio` and `misaki[en]`, which live in
`~/miniconda3/envs/local-ml-py311`, not in the base env):

```bash
LITELLM_BASE_URL=http://127.0.0.1:8000/v1 PET_TALK_SILENT=1 \
  doppler run --project unfoundbox --config dev_personal -- \
  ~/miniconda3/envs/local-ml-py311/bin/python -m uvicorn server.app:app \
    --host 127.0.0.1 --port 8089
```

No provider address is baked into the tree: `LITELLM_BASE_URL` names the proxy
and the loopback literal in `providers/_shared.py` is only a last-resort
fallback.

**Stub-provider numbers, for the harness not the product.** `qa/latency.py`
with `STT_PROVIDER=stub LLM_PROVIDER=stub TTS_PROVIDER=stub`: `stall_ms` 18.1ms
p50, `first_sentence_ms` 18.1ms, `barge_ms` 0.5ms. Hermetic slow-TTS barge test
(`qa/test_turn_lifecycle.py`): barge kill at 0.8ms, `dropped=4`, queue
high-water 3. These exercise the queue, the frame plumbing and the cancellation
path — not any backend's real latency. Treat them as "the harness is honest,"
never as user-facing latency.

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
- **PDF OCR now has a fixture and coverage (fixed this pass, 2026-09-12).** `qa/fixtures/eyes_sample.pdf` (a hand-built, stdlib-only, single-page real-text PDF) plus `qa/test_eyes.py::TestPdfOcr` — a hermetic path (`StubEngine`, always runs) and a `PET_TALK_REAL_ENGINE=1`-gated path that shells the real `zrv` CLI. Measured: `zrv ocr qa/fixtures/eyes_sample.pdf --task transcribe --json` returns `engine: apple-vision`, exact text match, in ~370ms — `zrv` reads the PDF's real text layer directly rather than rasterizing and OCRing, so there was no OCR noise to account for. `pdf_max_pages=5` (multi-page truncation) is still unexercised — this fixture is one page.
- **`EYES_DESCRIBE_ENGINE` now fails fast by name when unset (fixed this pass, 2026-09-12).** Previously an unset pin meant `describe` fell through to `zrv`'s own default engine (`apple-vision`, which refuses `--task describe` with its own unrelated error) instead of a clean `eyes_disabled`. `EyesProvider.resolve()` now raises `eyes_disabled` with detail `describe_engine_unset:EYES_DESCRIBE_ENGINE` before touching any engine when `task == "describe"` and `describe_engine_pin` is unset — tested hermetically in `qa/test_eyes.py::TestDescribeEngineGate` (unset fails closed naming the var; set routes through; transcribe is unaffected; an explicit `task: describe` on a PDF still hits the gate since a frame's explicit task wins over the PDF default per `task_for`'s own precedence).
- **Kokoro word timings are estimated, not measured.** `KokoroSpacePilotTTS` does not appear to report real per-word timestamps from the daemon; `word_times` for Kokoro-synthesized audio comes from `estimate_word_times()` (uniform-per-word or char-length-weighted), always carrying `estimated: true`. Any UI that highlights the currently-spoken word against Kokoro audio is highlighting a guess, not a measurement.
- **`cli/hotkey/build.sh` does not exist yet** in this checkout, though `Makefile`'s `build-hotkey` target and this repo's build comments both reference it as owned by another lane. `make build-hotkey` will fail until that script lands.
- **Fixed 2026-09-12b: the LLM could not stream text, and turn latency was unmeasurable.** Both are closed — see §9.1. The diagnosis in the previous pass ("`groq/compound-mini` cannot stream text") was wrong: reasoning models were never given enough token ceiling to reach `content`. `stall_ms`, `first_sentence_ms` and `barge_ms` are now measured live and all three PASS.
- **`tts_ms` FAILs at 236.6ms against 200ms, and cannot pass without a wire-contract change.** Kokoro's RTF here is ~0.06 and `agent.sentence` carries one finished WAV, so any sentence past ~9 words is over budget by construction. Chunked/streaming synthesis is the fix; see §9.1 for the full diagnosis. Down from 1285.9ms this pass.
- **The stall cache is cold on the first turn of a fresh process**, which costs one full synth (~1.2s) and is the only reason `turn_worst_ms` breaches. `KokoroLocalTTS` warms its model at startup; nothing warms the persona's stall phrases. A startup pre-synth of each active persona's stall set would close it.
- **`qa/live_ws_turn.py`'s stall gate is N=1 and flakes at about 1 run in 5** (measured: 183.4/199.2/213.7/319.5/401.3ms against a 400ms budget). It should take a median of a few turns the way `qa/latency.py` does; until then a single red run is not proof of a regression.
- **`kokoro-local` needs an interpreter that has `mlx-audio` and `misaki[en]`.** On this machine that is `~/miniconda3/envs/local-ml-py311`, not the base env — a server booted with the base `python3` gets a named `tts_mlx_audio_not_installed` from the first synth, not a silent fallback. Apple Silicon only.
- **The default STT is the least accurate setting in its family.** `tiny.en` with `beam_size=1` is what buys 143ms; it heard "Donna Paulsen" as "Donna Paulson" in the real-engine receipt. `WHISPER_MODEL=base.en` trades 190ms more for better words, without a code change.
- **`mlx-whisper` is not installed**, so `STT_PROVIDER=mlx` is untested here; it also reloads the model on every call, which would need the same load-once treatment `KokoroLocalTTS` got before it could be measured fairly. `whisperkit-cli` is not on PATH either.

## 11. A note on `cli/hotkey/*.swift`

These five files (`main.swift`, `hud_window.swift`, `earcons.swift`, `paste_injector.swift`, `config.swift`) may be under concurrent edit by another lane in this worktree. This spec describes their existence and role (native macOS Carbon hotkey listener, notch HUD, earcon engine, paste injector, config loader) per the module map in §3, but does not assert their current internal behavior in detail — verify against the live files before trusting a specific claim about them.
