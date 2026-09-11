# pet-talk — Comprehensive Technical Design Specification

> **Full-Duplex Voice & Multimodal Agent Architecture: Phase 1 through Phase N**

---

## 1. Executive Summary & North Star

`pet-talk` is a local-first, full-duplex conversational agent engine. It bridges human speech, multimodal vision, and frontier reasoning with human conversational physics:
1. **Reflex Arc vs Deliberation Loop**: Fast stall reflex ($\le 400\text{ms}$) acknowledges human intent while background workers stream heavy reasoning and tool executions.
2. **Instant Barge-In**: User interruptions kill audio playback and flush speech queues in $\le 100\text{ms}$.
3. **Zero Token Waste**: Multimodal perception extracts structured text via `zero-vision` rather than burning thousands of vision tokens per frame.
4. **The Hippocampus Inversion**: *"Unlimited conversation with amnesia is just a longer demo."* Memory is a first-class, reboot-proof transactional ledger.
5. **Config-Only Tyre Swapping**: STT, LLM, and TTS backends hot-swap via environment configuration with zero code mutation.

---

## 2. Global System Topology

```
                  +-------------------------------------------------------------+
                  |                      HUMAN SENSES                           |
                  |     Voice (Mic)   ·   Eyes (Images/PDFs)   ·   Touch (UI)   |
                  +-------------------------------------------------------------+
                                       |                     |
                                [WebAudio AEC]       [user.attach]
                                       |                     |
                                       v                     v
                               +---------------+     +---------------+
                               |  Silero VAD   |     |  zero-vision  |
                               +---------------+     |  (zrv ocr)    |
                                       |             +---------------+
                                       v                     |
                               +---------------+             |
                               |  STT Provider |             |
                               | (Whisper/DG)  |             |
                               +---------------+             |
                                       |                     |
                                       +----------+----------+
                                                  |
                                                  v
                                      +-----------------------+
                                      |   PET-TALK ROUTER     |
                                      +-----------------------+
                                       /                     \
                                      /                       \
             [FAST REFLEX: <=400ms]  /                         \  [WORKER LANE: Heavy Reasoning]
                                    v                           v
                       +-------------------------+    +----------------------------------+
                       | Persona Stall Generator |    | Streaming Sentence Tokenizer     |
                       | ("Looking into that...") |    | (Claude / LiteLLM / Edge0)       |
                       +-------------------------+    +----------------------------------+
                                    |                                   |
                                    +-----------------+-----------------+
                                                      |
                                                      v
                                      +-------------------------------+
                                      |  SpeakQueue (FIFO / Resumable)|
                                      +-------------------------------+
                                                      |
                                                      v
                                      +-------------------------------+
                                      |  TTS Engine (Kokoro / DG / 11)|
                                      +-------------------------------+
                                                      |
                                                      v
                                      +-------------------------------+
                                      |   Browser AudioWorklet / WS   |
                                      +-------------------------------+
                                                      |
                       +------------------------------+------------------------------+
                       |                                                             |
              [BARGE-IN TRIGGER]                                           [SUCCESSFUL TURN]
                       |                                                             |
                       v                                                             v
             +--------------------+                                       +--------------------+
             | Kill audio <=100ms |                                       | Commit to Ledger   |
             | Flush SpeakQueue   |                                       | (Hippocampus)      |
             | Rollback in-flight |                                       | Update Episodic DB |
             +--------------------+                                       +--------------------+
```

---

## 3. Phase-by-Phase Roadmap (Phase 1 to Phase N)

### Phase 1: Duplex Reflex & Acoustic Hygiene (Core Foundation)
*Status: Staged & Verified*

* **Goal**: Establish the zero-feedback full-duplex audio loop between browser and server.
* **Architecture**:
  * Hardware Acoustic Echo Cancellation (AEC), noise suppression, and auto gain control via `navigator.mediaDevices.getUserMedia` in [`web/src/App.tsx`](file:///Users/saurabh/code/unfoundbox-crew/pet-talk/web/src/App.tsx).
  * Elimination of microphone loopback monitor using a muted WebAudio `GainNode` connected to `ctx.destination`.
  * FastAPI WebSocket duplex server on `/ws` implementing canonical frames: `user.start`, `user.stop`, `agent.stall`, `agent.sentence`, `agent.done`, `state.*`, and `barge`.
  * Client-side audio cancellation on barge-in with sample-accurate playback stoppage.
* **Exit Gates**:
  * Barge-in kill latency $\le 100\text{ms}$.
  * Zero speaker-to-mic feedback loop during continuous open-mic speech.
  * Web build compiles clean with zero TypeScript errors.

---

### Phase 2: Live Reasoning Fleet & Streaming Sentence Chunking
*Status: In Progress*

* **Goal**: Replace canned stub lines with live frontier LLM reasoning while preserving sub-400ms reflex speed.
* **Architecture**:
  * **The Reflex Router**: Distinguish immediate conversational answers from deep worker inquiries. Trigger phrases route to the fast stall branch; direct queries stream immediately.
  * **Streaming Sentence Accumulator**: Raw token fragments from OpenAI-compatible / LiteLLM proxy (`http://100.99.50.84:8000/v1`) are buffered into syntactic clauses and sentence boundaries (`.`, `?`, `!`, `\n`) before being passed to TTS.
  * **Pluggable Provider Factory**: Standardized `make_llm()`, `make_stt()`, and `make_tts()` dynamic factories driven purely by environment variables (`LLM_PROVIDER`, `STT_PROVIDER`, `TTS_PROVIDER`).
* **Exit Gates**:
  * First stall audio begins playback $\le 400\text{ms}$ after user stops speaking.
  * Worker streams $\ge 3$ complete sentences gaplessly behind the playing stall audio.
  * Zero 1-word audio chunks dispatched to TTS.

---

### Phase 3: Hippocampus (Reboot-Proof Episodic Memory)
*Status: Implemented & Unit-Tested*

* **Goal**: Eliminate agent amnesia across turns and server restarts.
* **Architecture**:
  * **Episodic Ledger**: Durable append-only ledger in [`server/memory.py`](file:///Users/saurabh/code/unfoundbox-crew/pet-talk/server/memory.py) (`ledger.jsonl`).
  * **Context Grounding**: On every turn, the server reads the active persona's instruction spec (`personas/{persona}.md`) and injects the last $K$ conversational turns into the LLM system prompt.
  * **Barge-In Rollback**: Turns interrupted by barge-in are never committed as completed agent utterances, preventing hallucinations and conversational drift.
  * **Compaction Protocol**: Background summarization compacts historical turns into dense persona memories without ballooning token context.
* **Exit Gates**:
  * 100% dialogue context survival across complete server restarts.
  * Zero orphan commits to ledger when turns are cancelled mid-stream.
  * `qa/test_memory.py` passes 100% green.

---

### Phase 4: Senses — Eyes (Zero-Vision Multimodal Ingestion)
*Status: Specified in `docs/WAVE3.md`, Ready for Build*

* **Goal**: Ingest screenshots, PDFs, and camera snapshots over the duplex WebSocket without burning raw multimodal vision tokens.
* **Architecture**:
  * **Wire Protocol**: Client emits `user.attach` with base64 data and mime type (`image/png`, `application/pdf`). Server responds with `eyes.received`.
  * **Zero-Vision Processor**: Shells out to local `zrv ocr` with engine pinning (`--engine tesseract` or local binary).
  * **Task Selection**:
    * `transcribe`: Verbatim text extraction for receipts, tables, code, and PDFs (capped at first 5 pages).
    * `describe`: Compact semantic scene summary for photographs and UI screenshots.
  * **Context Tagging**: Cleanly injects into the LLM turn context as `[eyes:ref:kind|task] <extracted_text>`.
* **Exit Gates**:
  * OCR extraction completed in $\le 1.5\text{s}$.
  * Zero vision tokens burned for text-renderable documents.
  * Temporary files purged immediately after extraction; hashes only in telemetry.

---

### Phase 5: Heartbeat (Proactive Presence & Circadian Ticks)
*Status: Ready for Build*

* **Goal**: Allow the pet/agent to initiate autonomous actions, check-ins, and digests without user prompting.
* **Architecture**:
  * **Asyncio Tick Lane**: Single background scheduler running inside `server/app.py` (`server/heartbeat.py`, default 15-minute tick).
  * **Job Hierarchy**:
    1. *Overnight Digest*: Summarizes late-night tasks and commits once after 07:00 local time.
    2. *Idle Nudge*: Friendly check-in after $2\times$ heartbeat intervals of silence (capped at 1/hour).
    3. *Long-Task Keepalive*: Periodically updates user during background tool execution exceeding 60 seconds (*"Still analyzing the logs..."*).
  * **Circadian Quiet Hours**: 23:00 to 07:00 local time suppresses all spoken output unless explicitly flagged `house_on_fire`.
  * **Instant Floor Yield**: Any incoming `user.start` immediately suppresses pending heartbeat tasks.
* **Exit Gates**:
  * Zero unprompted speech events between 23:00 and 07:00.
  * Long-running worker tasks emit keepalive ticks before user wonders if the agent hung.

---

### Phase 6: Edge Speech & Multilingual Expansion
*Status: Future Wave*

* **Goal**: Native edge STT/TTS with instantaneous language switching.
* **Architecture**:
  * **Silero Neural VAD**: Replace RMS energy thresholds with a 1.8MB Silero ONNX model running in $<1\text{ms}$ per 30ms audio frame.
  * **Streaming STT Ear**: Fast streaming STT via Sherpa-ONNX or Deepgram streaming WebSocket, eliminating temporary file disk I/O.
  * **Multilingual Kokoro Binding**: Dynamic voice resolution in `personas/voices.yaml` based on detected speech language (English, Hindi, Spanish, French).
* **Exit Gates**:
  * VAD false-trigger rate on ambient typing/breathing reduced by $>90\%$.
  * STT streaming latency $\le 150\text{ms}$.

---

### Phase N: Distributed Fleet Burst & MoQ Relay
*Status: Vision*

* **Goal**: Multi-device roaming duplex loops with $0 sovereign burst.
* **Architecture**:
  * **Media over QUIC (MoQ)**: Ultra-low latency relay for audio and DocIR state synchronization across edge devices.
  * **SkyPilot Compute Burst**: Automatically spin up sovereign GPU instances for heavy offline batch synthesis, model fine-tuning, or large context digestion.
  * **Self-Contained Edge Packaging**: Tauri / mobile wrapper packaging Kokoro and Whisper ONNX models directly on-device.

---

## 4. Work Division & Lane Matrix

| Lane | Code Subsystem | Core Responsibilities | Dependencies | Owner |
|---|---|---|---|---|
| **Lane A: Audio & Reflex Core** | `web/src/App.tsx`, `server/app.py` (WS) | WebAudio Worklet, Silero VAD, hardware AEC, barge-kill $<100\text{ms}$. | WebAudio API | Lead Agent |
| **Lane B: Reasoning & Streaming** | `server/providers.py`, `server/app.py` | Streaming sentence chunker, fast-stall classifier, LiteLLM / Fleet proxy wiring. | LiteLLM Proxy (:8000) | Donna / Co-Founder |
| **Lane C: Hippocampus (Memory)** | `server/memory.py`, `server/ledger.jsonl` | Episodic memory ledger, compaction, context injection, barge-safe transactions. | File system / SQLite | Shared |
| **Lane D: Sensory Eyes** | `server/eyes.py`, `web/src/` | WS `user.attach` frames, `zero-vision` (`zrv ocr`) runner, persona task mapping. | `zrv` CLI | Lane D Agent |
| **Lane E: Heartbeat & Scheduler** | `server/heartbeat.py` | 15-min circadian tick, quiet hours enforcement, keepalive & digest jobs. | `server/app.py` loop | Lane E Agent |
| **Lane F: QA & Gate Enforcement** | `qa/`, `./qa/run_all.sh` | Automated end-to-end latency profiler, live WS turn runner, protocol tests. | All Lanes | CI / Automated |

---

## 5. Non-Negotiable QA Gates (All Green or No Ship)

```
+-- GATE 1: FAST STALL LATENCY     <= 400ms from user voice cessation to speaker sound
+-- GATE 2: BARGE-IN AUDIO KILL    <= 100ms from user speech onset to playback abort
+-- GATE 3: WORKER GAPLESS STREAM  >= 3 sentences generated without audio stutter or underrun
+-- GATE 4: MEMORY RETENTION       100% turn context preserved across server process reboot
+-- GATE 5: ZERO TOKEN BURNING     0 multimodal tokens burned on readable text/DOMs (Eyes)
+-- GATE 6: SILENT CIRCADIAN       0 unprompted audio events between 23:00 and 07:00 (Heartbeat)
+-- GATE 7: CONFIG-ONLY TYRE SWAP  100% provider swapping via environment variables (0 code diff)
```
