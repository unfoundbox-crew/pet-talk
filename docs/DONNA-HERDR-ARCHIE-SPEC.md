# Donna Fleet Orchestration & Ambient Pet Relay: Tech Design Spec

**Document ID**: SPEC-PET-TALK-005  
**Status**: DRAFT  
**Author**: Saurabh & Antigravity  
**Date**: September 2026  
**Target Version**: Pet-Talk v0.3.0  

---

## 1. Executive Summary & Problem Statement

### 1.1 The Problem
When running multi-agent coding workflows, developers delegate work across specialized autonomous CLI agents—**"the pets"** (e.g. Codex in pane 1, Claude Code in pane 2, AGY in pane 3, OpenCode in pane 4). This architecture introduces three operational bottlenecks:

1. **Terminal Tab Saccades**: Constant visual context-switching between multiplexer panes to check if an agent has finished, errored, or asked for clarification.
2. **Friction in Task Delegation**: Manually navigating to a pane, formatting bracketed-paste instructions, and pasting commands breaks flow state.
3. **Forensic Amnesia**: No unified ambient awareness of machine-wide token burn, blunder rate, or past session handoffs across different agent harnesses.

### 1.2 The Solution
**Donna** acts as the voice-driven **Chief of Staff & Co-Founder Brain**, sitting on top of:
- **Herdr**: The local terminal workspace and agent multiplexer managing interactive agent PTYs.
- **Archie (AgentWorth)**: The local-first forensic flight recorder indexing 4,600+ sessions, token spend, unverified commits, and outcome rates.

Through Pet-Talk's sub-second voice loop, macOS Carbon hotkey (`Option + Tab`), and Dynamic Island HUD, Donna:
1. **Dispatches** voice-spoken tasks to specific pets via Herdr (`herdr agent prompt`).
2. **Monitors** pane execution in the background without stealing user focus.
3. **Audits** execution integrity and token burn through Archie.
4. **Relays** concise, ambient spoken updates when agents finish, stall, or require human intervention.

---

## 2. System Architecture & Component Topology

```
+-----------------------------------------------------------------------------+
|                                HUMAN (Saurabh)                              |
|   - Voice input: Option + Tab (Carbon global hotkey, sub-2ms mic open)      |
|   - Visual sensory: MacBook Notch Dynamic Island HUD (Obsidian Zinc)        |
|   - Audio sensory: CoreAudio sub-2ms earcons & Smallest.ai natural speech   |
+-----------------------------------------------------------------------------+
                                       ^
                                       |
                       (Duplex Audio / WebSocket :8089)
                                       |
                                       v
+-----------------------------------------------------------------------------+
|                     DONNA (Pet-Talk Executive Server)                       |
|                                                                             |
|   +-- [STT Matrix] Deepgram Nova-3 / Groq Whisper / MLX CoreML              |
|   +-- [Spoken Brain] Groq LPU (compound-mini) / OpenAI (gpt-5-nano)         |
|   +-- [Intent Classifier] Direct Answer vs Fleet Delegation vs Archie Audit |
|   +-- [VoiceProseFormatter] Sanitizes terminal output to <= 20 spoken words |
|   +-- [TTS Matrix] Smallest.ai (lightning_v3.1_pro) / Kokoro-82M            |
+-----------------------------------------------------------------------------+
                    |                                         |
     (UNIX Socket / CLI JSON)                 (Subprocess CLI / MCP)
                    |                                         |
                    v                                         v
+-------------------------------------+   +-----------------------------------+
|      HERDR (Agent Multiplexer)      |   |    ARCHIE (AgentWorth Forensics)  |
|      /Users/saurabh/.config/herdr/  |   |    ~/.agentworth/index.db         |
|                                     |   |                                   |
|  - Manages interactive PTY panes    |   |  - 4,647 indexed agent sessions   |
|  - Native bracketed paste framing   |   |  - Machine-wide token spend audit |
|  - Agent state observer:            |   |  - Blunder / suspect commit radar |
|    (idle, running, waiting_input)   |   |  - Cross-agent session handoffs   |
+-------------------------------------+   +-----------------------------------+
          |            |           |
          |            |           |
          v            v           v
    +-----------+ +-----------+ +-----------+
    |   Codex   | |   Claude  | |    AGY    |
    |  (Pet 1)  | |  (Pet 2)  | |  (Pet 3)  |
    +-----------+ +-----------+ +-----------+
```

---

## 3. Operational Interaction Flows

### 3.1 Flow A: Voice-Driven Task Dispatch (`Saurabh -> Donna -> Herdr -> Pet`)

```
[ Saurabh presses Option+Tab ]
       |
       v  (Acoustic Earcon: Tink chime <1.2ms, HUD slides down)
[ Saurabh Speaks: "Donna, have Codex fix the auth test failure in pane 2." ]
       |
       v  (Deepgram / Groq STT ~110ms)
[ Intent Filter: FLEET_DISPATCH detected (target: codex, pane: 2) ]
       |
       +---> [ Donna calls HerdrBridge: herdr agent prompt codex "<instruction>" ]
       |        |
       |        v  (Herdr injects bracketed-paste into Codex PTY)
       |     [ Codex begins execution ]
       |
       v  (Smallest.ai Lightning TTS ~350ms)
[ Donna Spoken Reply: "Dispatched to Codex. I'll let you know when the suite passes." ]
       |
       v  (HUD dynamic island slides up into camera notch)
[ Saurabh continues editing in Cursor uninterrupted ]
```

### 3.2 Flow B: Ambient Completion Relay (`Pet -> Herdr/Archie -> Donna -> Saurabh`)

```
[ Codex completes execution in Herdr pane 2 ]
       |
       v
[ Herdr State Observer detects: running -> idle (Exit Code: 0) ]
       |
       v
[ HerdrBridge emits internal event to Donna Daemon ]
       |
       v
[ Donna synthesizes 1-sentence executive brief via VoiceProseFormatter ]
("Codex finished the auth fix; all 14 unit tests are green.")
       |
       +---> Dynamic Island HUD expands: [Codex: 14 tests green]
       +---> Acoustic Earcon: Pop turn chime
       +---> Smallest.ai TTS streams spoken update to system speakers
```

### 3.3 Flow C: Forensic Audit & Blunder Detection (`Saurabh -> Donna -> Archie`)

```
[ Saurabh presses Option+Tab: "Donna, what's our token burn and blunder rate today?" ]
       |
       v
[ Intent Filter: ARCHIE_AUDIT detected ]
       |
       v
[ ArchieBridge executes: archie stats usage --today --json && archie repo suspect --json ]
       |
       v
[ Archie returns structured JSON: 4.2M tokens spent, 2 unverified commits in server/app.py ]
       |
       v
[ Donna Spoken Reply: "We've burned 4.2M tokens today. Archie flagged two unverified commits on server/app.py." ]
```

---

## 4. Subsystem Technical Specifications

### 4.1 Herdr Bridge (`server/herdr_bridge.py`)
Responsible for reliable, non-blocking communication with the active Herdr server over `/Users/saurabh/.config/herdr/herdr.sock` or CLI fallback.

- **Methods**:
  - `list_pets() -> list[PetAgent]`: Queries `herdr agent list` to discover active agents, their assigned pane IDs, and current state (`idle`, `running`, `waiting_for_input`, `dead`).
  - `prompt_pet(name_or_id: str, prompt: str) -> bool`: Calls `herdr agent prompt <name_or_id> "<prompt>"` using canonical bracketed-paste framing. **NEVER** uses raw PTY character sends.
  - `read_pet_output(name_or_id: str, lines: int = 50) -> str`: Calls `herdr agent read <name_or_id>` to inspect the latest terminal buffer.
  - `wait_pet(name_or_id: str, timeout_s: float = 300.0) -> str`: Polls or listens for state transition to `idle` or `waiting_for_input`.

### 4.2 Archie Forensics Bridge (`server/archie_bridge.py`)
Responsible for extracting verified provenance and flight recordings from `/Users/saurabh/.local/bin/archie`.

- **Methods**:
  - `get_session_summary(limit: int = 5) -> list[dict]`: Queries `archie session list --json --limit <n>` for recent agent sessions.
  - `get_daily_usage() -> dict`: Queries `archie stats usage --json` for rolling spend, cache hit ratio, and token burn.
  - `get_suspect_commits() -> list[dict]`: Queries `archie repo suspect --json` to detect code committed without passing verified test outcomes.
  - `get_session_asks(session_id: str) -> list[str]`: Queries `archie session asks` to recover past prompt intentions.

### 4.3 Fleet Intent Classifier & Spoken Formatter
Integrated into `server/app.py` request processing:
- **Routing Rules**:
  - If text contains `ask <pet>`, `tell <pet>`, `run in <pet>`, `dispatch to <pet>` $\rightarrow$ `FLEET_DISPATCH`.
  - If text contains `status of pets`, `how are the agents`, `pet status` $\rightarrow$ `FLEET_STATUS`.
  - If text contains `token burn`, `blunders`, `archie`, `sessions` $\rightarrow$ `ARCHIE_AUDIT`.
  - Everything else $\rightarrow$ Standard Donna conversational LLM brain.
- **Voice Output Constraints**:
  - Maximum 2 sentences.
  - Strict maximum 20 words per turn.
  - Zero markdown backticks, code fences, or terminal escape sequences.

---

## 5. Failure Modes & Safety Guarantees

1. **Zero Keyboard Focus Stealing**: All HUD notifications and dispatch confirmations remain `.nonactivatingPanel`. The user's active editor cursor (Cursor/VSCode/Terminal) is never interrupted.
2. **Bracketed Paste Protection**: To prevent multiline prompt mangling in interactive agent TUIs (especially Codex), prompts are strictly dispatched via `herdr agent prompt`.
3. **Dead Socket Fallback**: If the Herdr server socket is closed or unreachable, Donna fails gracefully: *"Herdr multiplexer is offline; I can't reach the agent panes right now."*
4. **Emergency Audio Kill Switch**: Single-tap `Option + Tab` continues to guarantee `<2ms` absolute mute and turn cancellation, ensuring Donna never babbles over ongoing work.

---

## 6. Phased Implementation Roadmap

- **Phase 1 (v0.2.1)**: `HerdrBridge` read-only integration (`herdr agent list` & `herdr agent read`) + `ArchieBridge` stats queries.
- **Phase 2 (v0.2.2)**: Full voice dispatch via `herdr agent prompt` with execution ack.
- **Phase 3 (v0.2.3)**: Background agent watcher daemon emitting ambient Dynamic Island completions and earcon chimes.
