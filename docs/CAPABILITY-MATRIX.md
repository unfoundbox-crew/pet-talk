# pet-talk — capability matrix (no vendor lock-in)

- **Status**: current
- **Decision date**: 2026-09-12
- **Verified against**: commit `844a685` on `feat/overnight-hardening`, plus this pass's own `server/settings.py` fix (see below)
- **Scope**: every capability layer pet-talk lets you swap — STT, LLM, TTS, VAD, eyes OCR engine, and LLM routing through fleet aliases
- **Proven by**: `qa/test_capability_matrix.py` — read that file's docstrings alongside this table; it is the executable version of every claim below, and it is what keeps this table from going stale.

The product rule (Saurabh, 2026-09-12): *"no vendor lock-in, full customisation, any vendor at each capability layer can be plugged in or out."* This file is that rule turned into a table you can check against the code, not a slogan. Six things this pass verified, and two it did not paper over — see **Known gaps** at the bottom.

## STT (`STT_PROVIDER`, `server/settings.py` field `stt_provider`)

| provider (env value) | settings field for its key | key var | local/cloud | measured latency |
| --- | --- | --- | --- | --- |
| `faster-whisper` (default; aliases `faster_whisper`/`whisper`/`local`/`whisper-local`) | — | none | local | 143.4ms p50 — `docs/SPEC.md` §9.1 |
| `groq` | `groq_api_key` | `GROQ_API_KEY` | cloud | 306ms p50 (measured, see `server/providers/stt.py` `FasterWhisperSTT` docstring) |
| `deepgram` | `deepgram_api_key` | `DEEPGRAM_API_KEY` | cloud | 1432ms p50 (measured, same docstring) |
| `openai` (aliases `openai-whisper`/`whisper-openai`) | `openai_api_key` | `OPENAI_API_KEY` | cloud | NOT-MEASURED |
| `whisperkit` (aliases `whisper-kit`/`coreml`/`ane`) | — | none (CLI path) | local | NOT-MEASURED |
| `mlx` (alias `mlx-whisper`) | — | none | local | NOT-MEASURED |
| `sensevoice` (aliases `fleet`/`sovereign`) | — | none (base URL only) | local daemon | NOT-MEASURED |
| `stub` | — | none | n/a (tests) | n/a |

All eight construct hermetically (no network I/O at `__init__`, per each class's own docstring); `qa/test_capability_matrix.py::TestAtLeastTwoProvidersFromEnv.test_stt_has_at_least_two` builds all eight from env with a stub key and asserts zero degraded entries.

## LLM (`LLM_PROVIDER`, field `llm_provider`)

| provider (env value) | settings field for its key | key var(s) | local/cloud | measured latency |
| --- | --- | --- | --- | --- |
| `litellm` (default; aliases `fleet`/`local`) | `llm_api_key` (falls back to `key_for_llm()`) | `LITELLM_MASTER_KEY` or `LLM_API_KEY` | local proxy | 617ms p50 / 649ms p95 first-content-delta — `server/providers/llm.py` module docstring |
| `haiku` (aliases `claude-haiku`/`claude`) | `anthropic_api_key` | `ANTHROPIC_API_KEY` | cloud | rejected on evidence (HTTP 500, model not enabled) — NOT-MEASURED as a working route |
| `opencode` (aliases `zen`/`opencode-zen`) | `opencode_api_key` | `OPENCODE_API_KEY` (chain: `OPENCODE_GO_KEY`/`OPENCODE_LENOVO_KEY`/`OPENCODE_API_KEY`) | cloud | NOT-MEASURED |
| `gemini` (aliases `google`/`flash`) | `gemini_api_key` | `GEMINI_PRIMARY_API_KEY` (or `GOOGLE_API_KEY`) | cloud | rejected on evidence (HTTP 429, quota) — NOT-MEASURED as a working route |
| `groq` | `groq_api_key` | `GROQ_API_KEY` | cloud | 450ms p50 / 532ms p95 with `reasoning_effort=low` — same docstring |
| `openai` (alias `gpt`) | `openai_api_key` | `OPENAI_API_KEY` | cloud | NOT-MEASURED |
| `stub` | — | none | n/a (tests) | n/a |

**Known limitation, not hidden**: every real LLM tyre above is the same wire-format class, `OpenAICompatibleLLM` — the vendor lives in `llm_base_url`/`llm_model`, not in a distinct Python type. `GET /health`'s `providers.llm` therefore reads `"OpenAICompatibleLLM"` for `groq`, `openai`, `haiku`, `opencode`, and `gemini` alike; only `stub` shows a different class. Switching *to or from* `stub` changes what `/health` reports; switching between two cloud vendors does not — the vendor identity is only visible via `GET /settings`'s `settings.llm_provider`/`llm_base_url`/`llm_model` fields. Proven (both the working mechanism and this exact limitation) by `TestEnvSwapChangesHealth` in `qa/test_capability_matrix.py`.

## TTS (`TTS_PROVIDER`, field `tts_provider`)

| provider (env value) | settings field for its key | key var | local/cloud | measured latency |
| --- | --- | --- | --- | --- |
| `kokoro-local` (default; aliases `kokoro_local`/`kokoro-mlx`/`mlx`) | — | none | local, in-process | 255ms p50 — **FAILs** the 200ms budget, `docs/SPEC.md` §9.1 |
| `kokoro` (SpacePilot daemon) | — (token chain, not a settings field — see gap below) | `SPACEPILOT_TOKEN`/`STUDIO_TOKEN`/`KOKORO_TOKEN`, or auto-fetched from the daemon | local daemon | 1235ms p50 (measured, `server/providers/tts.py` docstring) |
| `smallest` (aliases `smallest-ai`/`smallest_ai`/`waves`) | `smallest_api_key` | `SMALLEST_API_KEY` | cloud | NOT-MEASURED |
| `elevenlabs` | `elevenlabs_api_key` **(added this pass — see fix below)** | `ELEVENLABS_API_KEY` | cloud | NOT-MEASURED |
| `deepgram` | `deepgram_api_key` (shared with STT's Deepgram key) | `DEEPGRAM_API_KEY` | cloud | 1939ms p50 (measured, `server/providers/tts.py` docstring, Aura-2) |
| `stub` | — | none | n/a (tests) | n/a |

## VAD — **not actually swappable today**

| provider | env selector | settings field | key var | local/cloud | measured latency |
| --- | --- | --- | --- | --- | --- |
| `EnergyGateVAD` (`server/providers/vad.py`) | none — no `VAD_PROVIDER`, no `make_vad()` | none | none | local | n/a — **this class is never imported outside its own module and the package `__init__.py` re-export.** It is dead code: no turn path constructs it. |
| `EnergyVAD` (`cli/audio.py`, used by `cli/client.py`) | none (only its threshold/silence-ms are tunable: `PET_TALK_VAD_THRESHOLD`, `PET_TALK_VAD_SILENCE_MS`) | n/a — client-side, not `RuntimeSettings` | none | local | n/a |

VAD is the one layer where the no-lock-in claim does not hold yet: there is exactly one implementation actually running (client-side, hardcoded), no factory, and no env var that swaps it for an alternative. `AGENTS.md` law 2 has only ever named STT/LLM/TTS as the config-only-swap contract — VAD was never in it, which this pass makes explicit rather than leaving as an implied gap. Fixing this needs a second VAD implementation plus a factory in `server/providers/vad.py` and wiring through `server/provider_factory.py`/`server/settings.py` into whichever of `server/ws.py`/`server/turn.py` would actually call it server-side — all outside this lane's owned paths (and `vad.py` isn't one of the two files this lane may touch). Reported, not silently patched around. `qa/test_capability_matrix.py::test_vad_has_only_one_provider_today_this_is_a_real_gap` is the tripwire: it fails loudly the day a second provider or a `make_vad()` appears, so this row gets updated instead of drifting.

## Eyes / OCR engine (`EYES_ENGINE`, `server/eyes.py`'s own `EyesConfig` — not `RuntimeSettings`)

| provider (env value) | settings surface | key var | local/cloud | measured latency |
| --- | --- | --- | --- | --- |
| `zrv` (default) | `EYES_ENGINE`, `EYES_ENGINE_PIN`, `EYES_DESCRIBE_ENGINE` (all `server/eyes.py`, not `/settings`) | none (local CLI subprocess) | local | NOT-MEASURED (no OCR latency row in `qa/budgets.json`) |
| `stub` | same | none | n/a (tests) | n/a |

Eyes is genuinely swappable by env with no code change (`make_engine`, `server/eyes.py`), but through its **own** config surface, not `POST /settings`/`RuntimeSettings` — there is no `GET/POST /settings` field for it and `GET /health` does not report which OCR engine is active. Worth naming precisely rather than assuming it follows the same contract as STT/LLM/TTS.

## LLM routing through fleet aliases — pending, not shipped

The sovereign router (`docs/BRIEF-2026-09-12-sovereign-quota.md`, capability aliases `fleet/frontier`/`fleet/fast`/`fleet/vision`/`fleet/long`/`fleet/embed`/`fleet/free`, header `x-fleet-route`, strict-mode `x-fleet-strict`) is in flight in a separate session as of 2026-09-12 and is not in this checkout. **NOT-MEASURED** — there is nothing to measure yet.

What already exists and is real today: `LLM_PROVIDER=litellm|fleet|local` are three names for the *same* LiteLLM proxy base URL (`server/settings.py` `LLM_BASE_URL_DEFAULTS`) — an alias, not per-capability routing. `qa/test_capability_matrix.py`'s `TestFleetRoutingAliases` carries one honest `SkipTest` (prints why, counts as skip not pass) and one test of what's actually true today.

## Fixed this pass (in `server/settings.py`, my owned file)

`TTS_REQUIRED_KEY` (`server/settings.py`) has always listed `elevenlabs` -> `ELEVENLABS_API_KEY` and `deepgram` -> `DEEPGRAM_API_KEY`, but `RuntimeSettings.key_for_tts()` only ever returned a value for the `smallest` family — so `provider_factory._require_key` degraded `TTS_PROVIDER=elevenlabs` or `=deepgram` with `missing_api_key` **even when the real env var was set**. Fixed: added the `elevenlabs_api_key` field to `RuntimeSettings` (populated from `ELEVENLABS_API_KEY` in `from_env()`), and `key_for_tts()` now returns `elevenlabs_api_key`/`deepgram_api_key` for those two providers. Proven by `qa/test_capability_matrix.py::TestAtLeastTwoProvidersFromEnv.test_tts_has_at_least_two`.

## Known gap NOT fixed here (belongs to lane 2, `server/providers/tts.py`)

`make_tts()`'s `elevenlabs` and `deepgram` branches (`return ElevenLabsTTS()` / `return DeepgramTTS()`) ignore the `api_key` argument `provider_factory._tts()` passes them — unlike the `smallest`/`kokoro` branches, which forward it correctly. Practical effect: a key supplied *only* in a `POST /settings` body, with no matching process env var, never reaches the constructed provider for these two tyres; both keep reading `ELEVENLABS_API_KEY`/`DEEPGRAM_API_KEY` straight from `os.environ` at synth time instead. Boot-time env selection still works (the env var is already in the process's environment either way); a pure runtime credential swap via `/settings` alone does not, for these two providers specifically. `server/providers/tts.py` is lane 2's owned path, not one of the two files (`server/provider_factory.py`, `server/settings.py`) this lane may touch, so this is reported for lane 2/the coordinator rather than patched here. `qa/test_capability_matrix.py::test_tts_elevenlabs_and_deepgram_key_not_forwarded_by_make_tts_KNOWN_GAP` is the tripwire — it fails loudly (in a good way) the day `make_tts()` forwards the key, which is the cue to delete this paragraph and the matching table caveat above.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-09-12 | First capability matrix, written for lane 4c's no-vendor-lock-in test pass. VAD and the eyes/elevenlabs/deepgram gaps above are the real findings; everything else in this table is a passing, hermetic assertion in `qa/test_capability_matrix.py`. |
