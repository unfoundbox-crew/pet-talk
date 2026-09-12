# Pet-Talk Universal Dictation Matrix & Clean Prose Router Specification

## 1. Executive Summary & Architecture

The Pet-Talk Universal Dictation Matrix provides a pluggable, low-latency Speech-to-Text (STT) layer designed to decouple audio transcription from duplex voice conversations and hotkey prompt dictation.

The architecture comprises two coupled layers:
1. **Universal STT Router (`server/providers/stt.py`, factory in the same package)**: A provider resolution matrix routing between Cloud Flagships, Apple Silicon hardware acceleration, Sovereign Fleet nodes, and local zero-cost test stubs.
2. **Clean Prose Engine (`server/dictation.py`)**: A deterministic text normalizer inspired by Wispr Flow that converts raw spoken audio transcripts into publication-grade prompts and code syntax.

```
+-------------------------------------------------------------------------+
|                         Audio Capture / Ingestion                       |
|   (Browser Mic ScriptProcessor PCM16 / Terminal CLI arecord/sox PCM16)  |
+-------------------------------------------------------------------------+
                                    |
                         POST /transcribe {pcm_b64}
                                    v
+-------------------------------------------------------------------------+
|                        Universal STT Router                             |
|                           (make_stt())                                  |
|                                                                         |
|  +--------------------+  +--------------------+  +--------------------+ |
|  | Cloud Flagships    |  | Apple Silicon Accel|  | Sovereign Fleet    | |
|  | - Deepgram Nova-3  |  | - WhisperKit (ANE) |  | - SenseVoice HTTP  | |
|  | - Groq Whisper LPU |  | - MLX Whisper (GPU)|  |   (:8086)          | |
|  | - OpenAI Whisper-1 |  +--------------------+  +--------------------+ |
|  +--------------------+  +--------------------+                         |
|                          | Zero-Cost Mock     |                         |
|                          | - StubSTT          |                         |
|                          +--------------------+                         |
+-------------------------------------------------------------------------+
                                    |
                           Raw Transcript Text
                                    v
+-------------------------------------------------------------------------+
|                         Clean Prose Formatter                           |
|                        (CleanProseFormatter)                            |
|                                                                         |
|  1. Technical Syntax Formatter (app.py, git commit -m, snake_case)      |
|  2. Filler Token Eliminator ("uh", "um", "like", "you know", "er")      |
|  3. Stutter Deduplicator ("the the" -> "the")                           |
|  4. Sentence Capitalization & Terminal Punctuation                      |
+-------------------------------------------------------------------------+
                                    |
                        Formatted Prose Response
                                    v
+-------------------------------------------------------------------------+
|                  Client (Web PromptComposer / CLI TUI)                  |
+-------------------------------------------------------------------------+
```

---

## 2. STT Provider Resolution Matrix

Every STT provider implements `STTProvider.transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> str`.
All failures are **fail-closed** and raise a typed `ProviderError(reason, detail)`:

| Provider Identifier | Adapter Class | Hardware / Network Target | Latency Target | Default Model | Credentials / Environment |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `stub` | `StubSTT` | In-memory synthetic | <1ms | Fixed string | None |
| `deepgram` | `DeepgramSTT` | Cloud API (`api.deepgram.com`) | ~250ms | `nova-3` | `DEEPGRAM_API_KEY` |
| `groq` | `GroqSTT` | Cloud LPU (`api.groq.com`) | ~120ms | `whisper-large-v3-turbo` | `GROQ_API_KEY` |
| `openai` | `OpenAIWhisperSTT`| Cloud API (`api.openai.com`) | ~600ms | `whisper-1` | `OPENAI_API_KEY` |
| `whisperkit` | `WhisperKitSTT` | Apple Neural Engine (CoreML) | ~180ms | `whisper-large-v3_turbo` | Local CLI binary |
| `mlx` | `MLXWhisperSTT` | Apple Silicon GPU (Metal) | ~200ms | `whisper-large-v3-turbo` | `mlx_whisper` Python package |
| `sensevoice` | `SenseVoiceSTT` | Sovereign Fleet LAN/Tailscale | ~80ms | SenseVoice-Small | `http://127.0.0.1:8086` |

### Provider Topologies
- **Cloud Flagships**: Selected for maximum multi-lingual vocabulary, accuracy, and background noise tolerance.
- **Apple Silicon (ANE & GPU)**: Zero cloud egress, completely sovereign on macOS M1/M2/M3/M4 hardware.
- **Sovereign Fleet**: High-throughput self-hosted Linux nodes running SenseVoice or Whisper on local LAN/Tailscale.
- **Zero-Cost Mock**: For hermetic CI/CD, pure-math unit tests, and offline regression verification.

---

## 3. Clean Prose Formatting Engine

The Clean Prose formatting engine converts spontaneous, informal oral utterances into crisp, ready-to-run developer instructions and prose.

### Transformation Pipeline
1. **Technical Syntax Preservation & Standardization**:
   - File extensions: Spoken `"app dot py"` -> `"app.py"`, `"index dot html"` -> `"index.html"`.
   - Git CLI flags: Spoken `"git commit dash m"` -> `"git commit -m"`, `"git checkout dash b"` -> `"git checkout -b"`.
   - Identifiers: Preserves CamelCase (`SettingsModal`, `PromptComposer`) and normalizes spoken snake_case (`make underscore stt` -> `make_stt`).
2. **Filler Token Elimination**:
   - Strips speech hesitation tokens: `"uh"`, `"um"`, `"er"`, `"ah"`, `"you know"`.
   - Contextually eliminates oral hesitation `"like"` (e.g. `"like we should"`, `"was like thinking"`, `", like,"`), while preserving comparative and verb usages (`"I like"`, `"looks like"`, `"would like"`).
3. **Stutter Deduplication**:
   - Identifies and collapses immediate duplicate word occurrences (`"the the"` -> `"the"`, `"in in"` -> `"in"`).
4. **Punctuation & Grammar Normalization**:
   - Capitalizes sentence start (preserving filenames and code identifiers like `app.py` or `make_stt`).
   - Automatically attaches appropriate terminal punctuation (`.` for statements, `?` for interrogative clauses).
   - Collapses stray punctuation and spacing artifacts.

---

## 4. API & Integration Contracts

### REST Endpoint: `POST /transcribe`
- **Request Body**:
  ```json
  {
    "pcm_b64": "<base64 encoded PCM16 mono bytes>",
    "sample_rate": 16000,
    "clean_prose": true
  }
  ```
- **Response Body (200 OK)**:
  ```json
  {
    "ok": true,
    "text": "We need to fix the bug in app.py.",
    "raw_text": "uh um we need to fix the the bug in app dot py"
  }
  ```
- **Error Response (400 Bad Request / 502 Bad Gateway)**:
  ```json
  {
    "ok": false,
    "error": "stt_no_key: GROQ_API_KEY env not set",
    "reason": "stt_no_key"
  }
  ```

### REST Endpoint: `POST /settings`
Allows hot-swapping the active STT provider at runtime:
```json
{
  "stt_provider": "groq",
  "groq_api_key": "gsk_...",
  "openai_api_key": "sk-...",
  "sensevoice_base_url": "http://127.0.0.1:8086"
}
```
Re-querying `GET /settings` reflects the currently active provider instance (`active.stt`) without restarting the server process.
