# Voices

How to pick a TTS voice, what we measured, and what Kokoro ships.

## 1. How to pick a voice (no code change)

Voice selection is config. Three layers apply, in this order — last one to touch a call wins:

1. **`TTS_PROVIDER`** picks the tyre. From `make_tts` in `server/providers/tts.py`, accepted values (and their aliases):

| Value | Aliases | What it is |
| --- | --- | --- |
| `kokoro-local` | `kokoro_local`, `kokoro-mlx`, `mlx` | In-process Kokoro-82M via mlx-audio. Default. |
| `kokoro` | — | Kokoro behind the SpacePilot daemon (HTTP + poll). |
| `smallest` | `smallest-ai`, `smallest_ai`, `waves` | Smallest AI Lightning TTS. |
| `deepgram` | — | Deepgram Aura speak API. |
| `elevenlabs` | — | ElevenLabs, `with-timestamps` endpoint. |
| `stub` | — | Sine-wave WAV, no model, no network. |
| `stub-chunked` | `stub_chunked` | Chunk-capable stub, proves the streaming wire path hermetically. |

   An unknown value raises `ProviderError("tts_unknown_provider", ...)` — it never falls back to Kokoro silently.

2. **`TTS_VOICE`** sets the tyre's default voice. An explicit `voice` argument to `make_tts` beats it; if neither is set, the provider falls back to its own built-in default (`af_heart` for Kokoro, `meher` for Smallest, `aura-2-thalia-en` for Deepgram, ElevenLabs' Rachel for ElevenLabs).

3. **A persona's own `voice:`** in `personas/<name>.md` still wins per call. `speech.py`'s `stream_chunks` passes `p.voice` and `p.speed` straight into `tts.synth_chunks(text, p.voice, p.speed, ...)` — so once a persona is loaded, its frontmatter voice is what actually gets spoken, regardless of `TTS_VOICE`.

### `POST /settings` — the runtime swap

Same field names as the env vars, no restart. `POST /settings` rebuilds the provider set under a swap lock (`server/runtime.py`), atomic with respect to in-flight turns — each turn snapshots providers once at its start, so a swap mid-turn cannot yank the tyre out from under it. Requires `X-Studio-Token` (`server/auth.py`). A key field round-trips masked (`"***"`) on `GET /settings`, and posting `"***"` back means "leave it alone" — it can never wipe a live key.

### `GET /voices` — known gap

Today `GET /voices` serves `personas/voices.yaml` (via `server/voices.py`'s `load_voices()`), not the active provider's actual voice list. That's a real gap, not a design choice you should rely on.

The function that actually enumerates a provider's voices is `provider_voices(provider)` in `server/providers/tts.py`. For `kokoro-local` it doesn't hardcode anything — it calls `kokoro_local_voices()`, which globs the HuggingFace cache (`~/.cache/huggingface/hub/models--*Kokoro*/snapshots/*/voices/*`) for `.safetensors`/`.pt`/`.npz`/`.bin`/`.json` files and returns their stems, sorted. No weights on disk means `[]`, honestly — never a remembered list standing in for it. For the cloud tyres it returns the static `CLOUD_VOICES` table (or `[]` for `elevenlabs`, since ElevenLabs voice ids are account-scoped and only `GET /v1/voices` with a key knows the truth).

### One command line per provider

Never print a secret — these name the env var only, and pull the value through `doppler run --project unfoundbox --config dev_personal --`.

```bash
# kokoro-local (default) — no key needed
TTS_PROVIDER=kokoro-local TTS_VOICE=af_bella doppler run --project unfoundbox --config dev_personal -- <start-server>

# kokoro (SpacePilot daemon) — token from STUDIO_TOKEN_FILE / SPACEPILOT_TOKEN / STUDIO_TOKEN / KOKORO_TOKEN
TTS_PROVIDER=kokoro doppler run --project unfoundbox --config dev_personal -- <start-server>

# smallest — needs SMALLEST_API_KEY
TTS_PROVIDER=smallest TTS_VOICE=meher doppler run --project unfoundbox --config dev_personal -- <start-server>

# deepgram — needs DEEPGRAM_API_KEY
TTS_PROVIDER=deepgram TTS_VOICE=aura-2-thalia-en doppler run --project unfoundbox --config dev_personal -- <start-server>

# elevenlabs — needs ELEVENLABS_API_KEY
TTS_PROVIDER=elevenlabs TTS_VOICE=CwhRBWXzGAHq8TQ4Fs17 doppler run --project unfoundbox --config dev_personal -- <start-server>

# stub / stub-chunked — no key, no model, for hermetic tests
TTS_PROVIDER=stub-chunked doppler run --project unfoundbox --config dev_personal -- <start-server>
```

## 2. Measured voice matrix (2026-09-12)

Sentence under test: "The weather today is bright and clear so the afternoon looks genuinely pleasant" — 13 words by plain split (an earlier brief called it 12; that was wrong).

N=3 per pair. One untimed warm-up for `kokoro-local` and `stub` only — cloud providers got no warm-up. Script: `qa/benchmarks/voice_matrix.py`.

`first_ms` equals total synth time for every provider here except kokoro-local, because none of the rest can return partial audio — say that once here rather than implying a second measurement per row.

| provider | voice | status | first_ms | p50_ms | min_ms | max_ms | wav_bytes | dur_ms | rtf | word_times |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| kokoro-local | af_heart | OK | 553.0 | 553.0 | 321.2 | 635.4 | 236444 | 4925 | 0.1123 | estimated |
| kokoro-local | af_bella | OK | 854.0 | 854.0 | 804.7 | 1091.2 | 254444 | 5300 | 0.1611 | estimated |
| kokoro-local | af_nicole | OK | 1105.9 | 1105.9 | 1003.1 | 1277.0 | 381644 | 7950 | 0.1391 | estimated |
| kokoro-local | af_sarah | OK | 442.8 | 442.8 | 436.3 | 475.9 | 250844 | 5225 | 0.0847 | estimated |
| kokoro-local | am_adam | OK | 907.0 | 907.0 | 442.0 | 1417.1 | 237644 | 4950 | 0.1832 | estimated |
| kokoro-local | am_michael | OK | 909.6 | 909.6 | 562.1 | 1605.2 | 261644 | 5450 | 0.1669 | estimated |
| kokoro-local | bf_emma | OK | 326.0 | 326.0 | 317.6 | 335.3 | 240044 | 5000 | 0.0652 | estimated |
| kokoro-local | bm_george | OK | 767.1 | 767.1 | 543.3 | 1262.2 | 274844 | 5725 | 0.1340 | estimated |
| deepgram | aura-2-thalia-en | OK | 3041.2 | 3041.2 | 2990.3 | 3131.0 | 226604 | 4720 | 0.6443 | estimated |
| deepgram | aura-2-andromeda-en | OK | 2894.0 | 2894.0 | 2812.5 | 3149.0 | 209324 | 4360 | 0.6638 | estimated |
| deepgram | aura-asteria-en | OK | 1767.7 | 1767.7 | 1724.9 | 1776.0 | 199548 | 4156 | 0.4253 | estimated |
| deepgram | aura-luna-en | OK | 1802.5 | 1802.5 | 1770.2 | 1776.0 | 184502 | 3842 | 0.4692 | estimated |
| smallest | meher | OK | 958.3 | 958.3 | 934.1 | 962.1 | 245804 | 5120 | 0.1872 | estimated |
| smallest | emily | ERROR | reason=tts_request_failed detail="smallest.ai: HTTP Error 400: Bad Request" | | | | | | | |
| smallest | radha | ERROR | reason=tts_request_failed detail="smallest.ai: HTTP Error 400: Bad Request" | | | | | | | |
| elevenlabs | 21m00Tcm4TlvDq8ikWAM | ERROR | reason=tts_request_failed detail="elevenlabs: HTTP Error 402: Payment Required" | | | | | | | |
| elevenlabs | CwhRBWXzGAHq8TQ4Fs17 (Roger) | OK | 526.7 | 526.7 | 494.0 | 654.4 | 67753 | NOT MEASURED (mp3, not WAV, at measurement time) | | real |
| stub | af_heart | OK (control row, not a product number) | 12.6 | 12.6 | 9.1 | 14.0 | 22094 | 500 | 0.0252 | estimated |

**These numbers were taken while four lanes were building concurrently on the same MacBook; load average reached 186.** Treat them as relative ordering between voices, not as absolute latency. The same `kokoro-local af_heart` sentence measured 236.6ms p50 on a quiet machine on the same day (`docs/SPEC.md` 9.1) against 553.0ms here. A clean re-measure is owed.

Deepgram's and ElevenLabs' numbers include network round trips from this machine.

The two ERROR rows are truthful results, not test failures: those voice ids are rejected by this account (Smallest, HTTP 400) and this key has no credit (ElevenLabs, HTTP 402).

## 3. Kokoro's shipped voices

54 `.safetensors` voice files, found at `/Users/saurabh/.cache/huggingface/hub/models--prince-canuma--Kokoro-82M/snapshots/e02c9eada7ce7416798af36b190a8a2dd2ecd566/voices` (verified with `ls`, 2026-09-12):

```
af_alloy, af_aoede, af_bella, af_heart, af_jessica, af_kore, af_nicole, af_nova, af_river, af_sarah, af_sky,
am_adam, am_echo, am_eric, am_fenrir, am_liam, am_michael, am_onyx, am_puck, am_santa,
bf_alice, bf_emma, bf_isabella, bf_lily, bm_daniel, bm_fable, bm_george, bm_lewis,
ef_dora, em_alex, em_santa, ff_siwis, hf_alpha, hf_beta, hm_omega, hm_psi, if_sara, im_nicola,
jf_alpha, jf_gongitsune, jf_nezumi, jf_tebukuro, jm_kumo,
pf_dora, pm_alex, pm_santa,
zf_xiaobei, zf_xiaoni, zf_xiaoxiao, zf_xiaoyi, zm_yunjian, zm_yunxi, zm_yunxia, zm_yunyang
```

20 of them are American English (`af_*` / `am_*` — 11 `af_` plus 9 `am_`, counted directly off the list above). The measured subset above is 8 of the 54.

`qa/benchmarks/voice_matrix.py` emits the full 54-name list under `meta.kokoro_voices_found`. `provider_voices("kokoro-local")` reads the same directory at runtime, via `kokoro_local_voices()` — an empty list when the weights are absent is honest, a remembered list is not.

Measured warning: `bf_emma` and `bm_george` log "Language mismatch, loading ... voice into American English pipeline" — mlx-audio runs the British voices through the American pipeline, which may cost prosody, even though they're the two fastest of the eight measured.

## 4. Word timings, per provider

| provider | word_times |
| --- | --- |
| kokoro-local | estimated |
| deepgram | estimated |
| smallest | estimated |
| stub | estimated |
| elevenlabs | real |

kokoro-local, deepgram, smallest, and stub all derive timing from WAV duration via `estimate_word_times()` — none of their backends return per-word or per-character timing. ElevenLabs is the one real source: its `with-timestamps` endpoint returns character-level alignment, and `_word_times_from_char_alignment()` in `server/providers/tts.py` groups it into words, each marked `estimated: False`. Checked against the docstrings in `server/providers/tts.py` — they say the same thing.

## 5. Which to choose

kokoro-local is the default for a reason: it runs in-process, needs no network and no key, and it's the only tyre that can stream partial audio (`synth_chunks`) — which is what puts first audio inside the 200ms `tts_ms` budget. Every other tyre returns one finished file and by contract cannot do better than "whole sentence, then play." Deepgram and Smallest exist so we're not locked to one vendor, not because they're faster from this machine — the matrix above shows them 2-6x slower than kokoro-local even under load. ElevenLabs is the one place we get real word timings instead of an estimate, but that costs a network hop and a credit balance, and today's key has neither credit nor headroom to spare.

## 6. Wiring `/voices` to the active provider

`provider_voices()` exists; the route does not call it yet. `server/routes_http.py`
is not this lane's file, so the hook is written here rather than applied. Four
lines, additive, and the existing response shape is untouched when the query
parameter is absent:

```python
# server/routes_http.py, inside the existing @router.get("/voices")
from .providers.tts import provider_voices   # add to the import block
from . import runtime

@router.get("/voices")
def voices(provider: str = "") -> Response:
    if provider or os.environ.get("PET_TALK_VOICES_FROM_PROVIDER") == "1":
        name = provider or runtime.settings().tts_provider
        return JSONResponse({"provider": name, "voices": provider_voices(name)})
    return JSONResponse(load_voices())   # unchanged default
```

`provider_voices` raises `tts_unknown_provider` on a name it does not know, so
`?provider=nonsense` fails closed rather than returning an empty list that looks
like "this tyre has no voices".
