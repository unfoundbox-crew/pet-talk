# Changelog

All notable changes to pet-talk are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.4.0] - 2026-09-12

Wave 2 plus this release pass. The cockpit became a real inspector, the notch
grew Archie's glyph, the eyes lane started feeding the prompt, and the first
turn after a boot stopped being the slow one.

Commit shas below name the individual `feat/wave-2` commits, which reached
`main` squashed as `a26eef1` — the sha is the audit trail, not something you
will find in `git log main`.

### Added

- **Inspector cockpit** on AgentWorth's design tokens (`4c4de45`, `e18d59c`):
  read-ahead buffer with arrow-key skip (`web/src/readAhead.ts`), a per-stage
  latency bar (`web/src/latency.ts`, `components/LatencyBar.tsx`), and a
  Developer toggle. Engineering numbers, frame names, provider class names and
  the persona name live behind that toggle and nowhere else — greppable in
  `web/src/devSurface.test.tsx`.
- **Archie's glyph**, small form, in the cockpit (`d0540de`) and in the notch
  capsule HUD (`8becdf9`, `cli/hotkey/ArchieGlyph.swift`). The mascot is
  Archie's existing small form; no new creature, no accessories.
- **Streaming STT** behind `PET_TALK_STT_STREAM` (`978354b`,
  `server/stt_stream.py`): 0.7s windows decode as they arrive so the stall
  stops waiting for a whole-utterance pass. **Default 0**, and the reason is a
  harness gap rather than a failure — `qa/latency.py` and
  `qa/live_ws_turn.py` both hand the whole utterance to one `user.stop` and
  never send a `user.chunk`, so every measured turn fell through to the
  whole-utterance path (`"path": "worker"` in all 6 captured turns, both flag
  settings). Turn-level benefit is NOT MEASURED.
- **Eyes OCR context in the prompt** (`8a0bf1e`): attached-image text reaches
  the turn, fenced as untrusted (see Security).
- **AX screen grounding exercised live** on this Mac, 2026-09-12: with the
  Accessibility permission granted, `pet-talk-hotkey ax` returned the focused
  app and window. The `ax` command itself landed in 0.3.0; this release is
  where it was documented (README, "Screen grounding"). Re-checking it during
  this release pass returned `ax_permission_denied` from both the rebuilt and
  the previously-granted binary — macOS ties Accessibility trust to the
  binary, and both had been rebuilt since the grant. Re-run
  `ax --request-permission` after any `make build-hotkey`.
- **STT warm-up at startup** (`server/warmup.py`): one throwaway
  transcription of 0.5s of silence through the active STT tyre, off-thread,
  logged as `stt_warm_ms`. `PET_TALK_STT_WARM=0` turns it off, loudly.
  Measured: first-turn stall 1264ms before, 198-288ms after.
- **Enter sends in the composer** (`web/src/components/composerKeys.ts`).
  Shift+Enter and Option+Enter insert a newline; Cmd+Enter and Ctrl+Enter send
  too; Enter on an empty box does nothing at all; an IME's commit keystroke
  does not fire a turn.
- `make build-cli`, and `pet-talk-hotkey --dump-state` now reports
  `cli: {resolved, path}`.

### Changed

- **The Noun Rule reaches every persona shape** (`dc84431`, `cec9def`): a
  persona that writes its own `instruction_spec` used to get no voice rules at
  all — no 20-word target, no 45-word ceiling, no subject requirement — while
  `server/speech.py` went on refusing its over-long sentences for a rule
  nobody had told it. The spec still leads and is still verbatim; the rules
  now follow it.
- **Stale-turn frames are ignored client-side** (`6bc105a`,
  `web/src/turnGuard.ts`): frames are applied only when their `turn_id`
  matches the live turn, so a barged turn cannot resume in the UI.
- **Stall frames carry `stream_url` and `chunked`**, so a stall is played by
  the same path as every other sentence.
- Skip now tells the server to stop synthesizing rather than only muting the
  client (`dfdd1a8`), and the streaming refusal is named once per socket
  instead of once per chunk (`4c97e7c`).
- `make build-hotkey` depends on `make build-cli`.

### Fixed

Six blockers from the adversarial review of the merged tree, each with a test:

- `0e8fd34` — OCR text could give the model orders. Now fenced (see Security).
- `de39cc2` — the eyes queue was not drained when the turn returned early, so
  an attachment could leak into the *next* turn's prompt.
- `39b84f3` — `seq` collision: the stall and the first direct-path sentence
  both claimed `seq` 0. Seq 0 belongs to the stall; the direct path starts at 1.
- `077dcb1` — a barge cancelled the turn but not the streaming-STT session,
  which kept decoding into a turn that no longer existed.
- `6bc105a` — frames from a barged turn were applied by the cockpit.
- `bfa7dd8` — the persona name, provider class names and raw wire reasons
  rendered with the Developer toggle **off**.

Also:

- `79a43a3` — a `NameError` in `_turn_pipeline` (`stall_sent` never threaded
  through) broke eight lifecycle tests.
- `1487713` — a caller buffer of a different length invalidated the
  streaming-STT tail seam.
- `bffd9c2` — the hotkey daemon could not find the CLI launcher after binaries
  were untracked; `make` builds it now. `bin/pet-talk-cli` was an untracked
  binary nothing in the tree built, so a clean checkout gave the daemon
  `Target CLI: (unresolved)` and Option+Tab was a silent no-op.
- `5cdaecf` — Enter did not send in the cockpit composer; only the Send button
  did, while the footer hint said "Enter".

### Security

- **OCR text is fenced as untrusted** (`0e8fd34`,
  `fence_ocr` in `server/persona_runtime.py`): the block sits inside a tag pair
  the payload cannot forge, carries an explicit disclaimer, and the voice rules
  are emitted *after* it so our rules — not the picture — are what the model
  read last. A screenshot of the words "ignore your instructions" is data.
- `archie` is invoked with an explicit `--` argv terminator, so a claim string
  can never be read as a flag.
- No `/Users/<name>` or `/home/<name>` path reaches a frame, a log line or a
  receipt (`redact_home`, `server/logs.py`). A home directory names the person
  at the keyboard.

### Known gaps

Named, not hidden. Each of these is a real limitation of 0.4.0:

- **Turn-level streaming-STT benefit is NOT MEASURED** — the harness never
  sends chunk frames, so the flag stays 0. See Added.
- The frame log retains `audio_b64`, so a developer-rail export carries raw
  audio bytes.
- `qa/budgets.json` ships to the client, which tells a page our internal
  budgets.
- No cross-socket decode lock: two sockets can drive two concurrent decodes on
  the same in-process model.
- `resume_from` is wired in `server/speak_queue.py` and nothing calls it.
- VAD is not swappable by env yet, unlike the other four capability layers:
  `server/providers/vad.py` ships one class and no `make_vad()`.
  `qa/test_capability_matrix.py` asserts the gap rather than hiding it.
- The `fleet` provider alias resolves to the house tyres (LiteLLM for LLM,
  SenseVoice for STT) and has since 0.3.0. The sovereign router's capability
  aliases — `fleet/vision` among them — are **not in this checkout** and
  **NOT-MEASURED**; see `docs/CAPABILITY-MATRIX.md`. There is no vision tyre
  in 0.4.0.
- `stall_ms` still FAILs its 400ms budget under ordinary background load
  (642.2ms p50 at load ~7, 2026-09-12d) — the stage receipt puts the breach in
  STT inside the server, not in isolation.
- Kokoro word timings are estimated, never measured per-word.
- PDF OCR has no real multi-page fixture.
- macOS ties Accessibility trust to the binary, so rebuilding
  `bin/pet-talk-hotkey` revokes the AX grant.

## [0.3.0] - 2026-09-12

One night of hardening driven by two independent code reviews (Sonnet and
Opus) plus an adversarial follow-up review of the merged tree. Five lanes,
merged into this branch.

### Breaking changes

- `LLM_PROVIDER` now defaults to `litellm` (the house proxy) instead of
  falling back to `groq` when `GROQ_API_KEY` is present. A provider that
  only appears on machines holding one key was never actually measured.
- `TTS_PROVIDER` now defaults to `kokoro-local` (in-process Kokoro via
  `mlx-audio`, 5x faster than the old daemon path). Needs `mlx-audio`
  installed in the `local-ml-py311` env; the daemon tyre (`TTS_PROVIDER=kokoro`)
  still works if you'd rather not add the dependency.
- `X-Studio-Token` is now required on every mutating route (`POST`/`DELETE`
  on `/settings`, `/personas`, `/ledger`, `/transcribe`) and on the WS
  handshake. `GET /health`, `/voices`, and `/audio/{id}` stay open. See
  README/SPEC §6.1 for the three ways to supply it.
- Hotkey chords changed: hand-over is now Option+Shift+Tab; pause is a
  double-tap of Option+Tab inside 400ms (previously a single tap).

### Added

- **Chunked TTS synthesis** (`agent.chunk`, SPEC §4.2.1): sentences are
  split at clause boundaries and synthesized as they're produced instead of
  waiting for the whole sentence. New wire frame `agent.chunk` (`seq`,
  `chunk_no`, `audio_b64`, `url`, `final`), plus `stream_url`/`chunked` on
  `agent.sentence`. `audio_url` keeps working for clients that ignore
  chunks. Only `kokoro-local` streams today — cloud tyres still return one
  finished file and report `chunked=false` with a named reason
  (`tts_no_chunk_support`), never a silent fallback.
- **Hand-over chord** (`user.handover` / `handover.received`): Option+Shift+Tab
  marks the next turn as delegated work and opens the mic.
- **Receipts on spoken claims** (`agent.receipt`): a sentence that claims
  work — tests green, a commit landed, a file changed — either names the
  session/commit that proves it or is refused. Backed by the `archie` CLI;
  refuses with `receipt_stale_index` when the index predates HEAD rather
  than guessing.
- **Zero-vision eyes lane** (`server/eyes.py`): `user.attach` →
  `eyes.received` / `eyes.text`. Local OCR via `zrv` on Apple Vision, no
  pixels or vision tokens leave the machine. Cockpit gets an attach dock
  (paste/drag-drop/file picker) and inline transcript blocks.
- **AX grounding** (`pet-talk-hotkey ax`, opt-in via `PET_TALK_AX=1`):
  frontmost app, window, selection as one JSON line. Typechecked; needs the
  Accessibility permission the docs now document.
- `docs/VOICES.md`: how to pick a provider/voice by env, `POST /settings`,
  or persona frontmatter; the measured matrix across kokoro-local, deepgram,
  smallest, and elevenlabs.
- `docs/CAPABILITY-MATRIX.md` and an executable no-vendor-lock-in test: every
  capability layer (STT/LLM/TTS/VAD/eyes-OCR) proves >=2 providers construct
  from env with no code change.
- Makefile with `build-hotkey`, `qa`, `qa-silent`, `qa-real` targets.

### Changed

- `app.py` split from 948 lines into 16 modules, each under ~400 lines
  (`runtime`, `settings`, `provider_factory`, `frames`, `speak_queue`,
  `speech`, `turn`, `stall`, `control`, `grounding`, `ws`, `routes_http`,
  `eyes`, plus the `providers` package).
- `docs/SPEC.md` is now the single source of truth (architecture, wire
  protocol, provider contract, settings API, budgets, gate proof, known
  gaps). `TECH-SPEC.md` and `docs/TECH-DESIGN.md` are now 5-line pointer
  stubs; their prior content lives in git history at `844a685`.
- `docs/ARCHITECTURE.md` and `docs/ROADMAP.md` rewritten in the living-docs
  format.
- STT default is now tuned `faster-whisper tiny.en` (float32 ndarray input,
  `beam_size=1`, `vad_filter=False`) — 143ms p50, down from 193ms.
- Reasoning LLM models (`gpt-oss`, `qwen3.6`, `compound-mini`) get a floored
  400-token ceiling and `reasoning_effort: low` so they reach `content`
  instead of exhausting their budget on reasoning deltas. An empty content
  stream now raises `llm_no_content` by name instead of returning quietly.
- Provider base URLs resolve through an env chain
  (`LITELLM_BASE_URL` → `LLM_BASE_URL` → `127.0.0.1`) instead of one
  hardcoded tailnet address.
- `qa/run_all.sh` rewritten: bounded per-suite (process-group kill on
  timeout), `PET_TALK_SILENT=1` gates every sound-producing suite,
  `PET_TALK_REAL_ENGINE=1` gates real-provider suites, budgets load from one
  `qa/budgets.json`, honest `NOT-MEASURED` instead of a faked number.
- HUD panel motion now runs on a real damped spring (120Hz semi-implicit
  Euler) driven by the measured notch width; the three previously-declared
  spring tokens are wired up instead of unused.
- One design-token source: `design/tokens.css` vendors AgentWorth's palette
  verbatim; `design/tokens.pet-talk.json` holds only what's genuinely
  pet-talk's own (notch/capsule geometry, motion, receipt colors).
- Compiled binaries (`bin/pet-talk-cli`, `bin/pet-talk-hotkey`) are no
  longer tracked in git; build them with `make build-hotkey`.

### Fixed
- Chunked audio now reaches the cockpit: `ChunkPlayer` is wired into `App.tsx` and plays `agent.chunk` frames as they arrive; whole-sentence playback is suppressed when a sentence is chunked (`acf90b7`).
- Receipts no longer block the event loop: the Archie lookup runs off-thread under a 2 s timeout, so a slow index cannot freeze other sockets (`f0544b7`).
- A reused `turn_id` barges the incumbent turn instead of overwriting it; each turn keeps its own queue (`464f140`).
- `archie` is called with `--` before positionals and path hints starting with `-` or containing `..` are refused (`2b1713c`).
- The hand-over flag is consumed by the next turn: `transcript.user` carries `handover: true` once and the prompt gets one delegation line (`e586a94`).
- Hotkey config rejects non-finite spring values and clamps ranges; the error earcon honours the silent and headless flags; AX casts are conditional (`dcc3697`, `cd3320e`).
- `agent.chunk` payloads are capped (`PET_TALK_CHUNK_MAX_BYTES`, default 512 KiB) and every sentence ends with exactly one `final` chunk (`756570a`).
- No absolute home paths on the wire in receipts or error details (`0a2eaca`).
- `/health` reports `ok: false` while any provider is degraded; the stale capability-matrix note is corrected (`a44117d`).

- **Barge could be lost.** The event loop blocked on STT/TTS/git calls, so
  a barge frame couldn't be read while a turn was in flight; a barge
  arriving before the turn task registered was silently ignored. Provider
  calls now run off-thread, the reader task is always live, and
  registration happens synchronously at task creation. Measured barge ack:
  0.4-1.1ms, `dropped=4` reported under slow TTS.
- **`tts_ms` failed by construction**: every backend returned one finished
  WAV per sentence, so nothing could arrive before the whole sentence
  synthesized (303.1ms p50 whole-sentence vs a 200ms budget). Fixed by
  chunked synthesis, above — a 4-word first chunk is the measured sweet
  spot; below that, a shorter chunk buys no latency and costs an extra
  call (a ~110ms floor of fixed per-call overhead).
- Deepgram's TTS endpoint wrote a streaming placeholder into the WAV header,
  so `pcm_duration_ms` read a 5-second clip as ~12 hours; the frame count is
  now cross-checked against actual bytes.
- ElevenLabs' TTS returned MP3 while every caller treated it as WAV — now
  requests `pcm_24000` and wraps the PCM itself.
- `make_tts` dropped a runtime-supplied key for elevenlabs/deepgram from
  `POST /settings`, so a hot-swap landed as `missing_api_key` with the key
  sitting right there.
- Groq's transcription endpoint 403'd on Python's default User-Agent
  (Cloudflare WAF) — every provider request now sends a real UA.
- An empty LLM stream reported a green `agent.done sentences=0` with no
  error; now sends `agent.error reason=llm_no_sentences` first.
- Client-driven memory leaks: the stall-audio cache was an unbounded dict
  keyed on client-controlled speed/voice (now an LRU capped at 64); client
  speed is quantised to 0.05 and clamped to [0.7, 1.4] instead of accepting
  any value.
- AX hook could blow its own 300ms budget by 5s waiting on an inherited
  child pipe; killed children now reap on a detached task. `swallowed()`
  raised `TypeError` on the one call site that logged why AX was skipped.
- HUD show/dismiss race: an in-flight dismiss animation could hide a panel
  that a newer `show()` had just re-shown; fixed with a generation counter.
- Settings modal no longer prefills a credential field with the server's
  `***` mask; hardcoded fleet IP (`100.99.50.84:8000`) removed from web and
  server defaults everywhere it appeared.

### Security

- **LiteLLM master key** was hardcoded in 8 places with a tailnet IP as the
  default host. Removed entirely — a missing key now fails closed with
  `missing_api_key`, and no fleet host ships in the tree.
- **`POST /settings` was unauthenticated and returned every API key in
  plaintext.** Any mutating route and the WS handshake now require
  `X-Studio-Token` (env `STUDIO_TOKEN`, else `STUDIO_TOKEN_FILE`, else
  generated at startup and written 0600); unauthorized requests get 401 /
  WS close 4401. `GET /settings` redacts every credential to `***` and a
  masked round-trip no longer wipes a live key.
- **Base-URL egress allowlist**: any `*_base_url` must be loopback,
  RFC1918, a host in `PET_TALK_ALLOWED_HOSTS`, or a provider's own default
  hostname — closing a path where an attacker-supplied base URL plus a
  triggered turn could exfiltrate the stored key. CORS is now
  `allow_credentials=False` with an explicit origin allowlist.
- **Kill switch was `SIGKILL` by process-name substring** — capable of
  killing any `afplay` process on the machine, not just pet-talk's own.
  Now tracks daemon-spawned children by PID, and any other PID is killed
  only after verifying UID ownership and executable scope, TERM before
  KILL. State files moved out of world-writable `/tmp` into a private
  0700 directory.
- The leaked key already in git history is left as-is, per Saurabh's call.

### Removed

- Tautological latency assertion, the flat 4000ms stall ceiling, and hangs
  in the old QA gate.
- Closed-gap test for the stall-warm-cache fix that shipped this pass.
- Blocking, host-specific reachability probe that ran a synchronous socket
  connect inside the async LLM stream.

### Measured

Turn-level, live WS, real speech, real providers, 2026-09-12c (chunked TTS
pass). Read with one caveat stated in the SPEC: three other lanes were
building on the same machine during this run — load average hit 186 — so
figures marked FAIL below are a measured ceiling under load, not a proven
regression in the code; the turn's own telemetry receipt traces the breach
to the STT stage in both cases.

| Metric | p50 | p95 | Budget | Result |
|---|---|---|---|---|
| `tts_ms` (first playable audio, chunked, kokoro-local) | 127.3ms | 246.1ms | 200ms | PASS at p50 |
| `first_audio_ms` (live WS) | 696.0ms | 775.8ms | 800ms | PASS |
| `turn_worst_ms` (cold first turn) | — | 433.9ms | 1200ms | PASS |
| `barge_ms` | 1.1ms | 1.2ms | 100ms | PASS |
| `stt_ms` (faster-whisper tiny.en) | 164.1ms | 285.6ms | 150ms | **FAIL** — machine load |
| `stall_ms` | 695.6ms | 775.6ms | 400ms | **FAIL** — machine load, traced to the STT stage |

`llm_ms` was not re-measured this pass; unchanged from the 2026-09-12b pass
(512.4ms p50 / 805.4ms p95, PASS at p50).

### Credits

Built by five Claude Code lanes working in separate worktrees overnight,
integrated and reviewed on this branch.
