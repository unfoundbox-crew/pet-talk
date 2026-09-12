---
title: pet-talk roadmap
product: pet-talk
version: 1.0.0
status: living
updated: 2026-09-12
horizon: 2026-Q4
---

## Now (this week)

- [ ] Measure real-engine latency (real STT/LLM/TTS, not stub) — why it matters: every number in `docs/SPEC.md` §9.1 is stub-only, so we don't know actual stall/barge/turn timing — done when: `qa/latency.py` runs against `STT_PROVIDER=deepgram LLM_PROVIDER=<real> TTS_PROVIDER=kokoro` and the numbers land in SPEC.md replacing "NOT MEASURED".
- [ ] Ship cockpit token UX — why it matters: a first-run web user has no confirmed path to find and paste the generated `.qa-scratch/studio.token` value — done when: `web/src/components/SettingsModal.tsx` has a visible "connect" flow that surfaces the token source, verified by opening the cockpit fresh with no `localStorage` entry.
- [ ] Prove the per-turn queue buffers ≥4-5 sentences under sustained real TTS — why it matters: gate 3 in `docs/SPEC.md` §9.2 is only partially exercised by `StubLLM`'s hardcoded 3 sentences — done when: a `qa/test_turn_lifecycle.py` fixture streams an unbounded answer against real or slow-TTS timing and asserts no gap past the 4th sentence.
- [ ] Land or confirm `pet-talk-hotkey ax` — why it matters: `server/grounding.py` shells out to it for `PET_TALK_AX=1` screen grounding but `main.swift`'s command switch (as of `97cdd69`) has no `"ax"` case — done when: `pet-talk-hotkey ax` returns the `{"ok","app","window","selection","path"}` JSON line `grounding.py` expects, or the SPEC is corrected to say it doesn't exist yet.
- [ ] Add PDF OCR test coverage — why it matters: `eyes.py`'s PDF path (`pdf_max_pages=5`) has zero fixture coverage — done when: `qa/test_eyes.py` includes a real PDF fixture exercising `task_for()` forcing `transcribe`.

## Next (this month)

- [ ] Land `cli/hotkey/build.sh` on the air lane — why it matters: `make build-hotkey` fails today with no script — done when: `make build-hotkey` succeeds via `ssh air` and produces `bin/pet-talk-hotkey`.
- [ ] WebRTC/Opus duplex audio streaming — why it matters: current PCM16-over-WS has packetization overhead against the 400ms first-audio target — done when: a streaming STT session receives phonemes continuously without waiting for endpointing, measured against the target in `docs/SPEC.md`.
- [ ] Acoustic echo cancellation for built-in speaker + open mic — why it matters: Donna's own speech can falsely re-trigger STT or cancel playback — done when: `kAudioUnitSubType_VoiceProcessingIO` is wired and a bleed test shows no false re-trigger.
- [ ] Measure Kokoro real per-word timing, or document the estimate gap loudly in the cockpit UI — why it matters: any word-highlight UI on Kokoro audio is highlighting a guess (`estimated: true`) — done when: either Kokoro reports real timestamps or the cockpit visibly marks estimated words.

## Later (this quarter)

- [ ] On-device small LLM (Apple MLX) — why it matters: removes network egress and per-token cost for the common case — done when: a quantized 3B-class model runs locally at >65 tok/s within 2.5GB unified memory and passes `qa/test_providers.py` as a provider.
- [ ] Long-term episodic memory beyond the session-scoped ledger — why it matters: past decisions aren't grounded across days today — done when: a local store (SQLite or similar) indexes past turns and `grounding.py` can pull from it.
- [ ] Multi-agent delegation from a turn (Donna dispatches a background subagent) — why it matters: long tasks currently block the voice loop — done when: a turn can spawn a background task and deliver a one-sentence spoken summary on completion without blocking new turns.
- [ ] AX-based selected-text / terminal-buffer grounding beyond the current app/window/selection snapshot — why it matters: `pet-talk-hotkey ax` (once it exists, see Now) only returns a coarse snapshot — done when: grounding can pull a terminal pane's visible buffer or an editor's selection into the prompt automatically.

## Not doing (and why)

- HyperFrames or any third-party video framework — repo law (`~/code/CLAUDE.md`): video work runs on the MotionVector DocIR pipeline, never HyperFrames. pet-talk has no video surface, but this stays explicit so nobody adds one via a third-party framework.
- A machine-wide kill switch — pet-talk's kill switch (`pet-talk-hotkey kill`) is process-scoped by design; a machine-wide switch is out of scope for a voice-loop daemon.
- Hardcoded fleet hosts — provider base URLs come from env/settings only (`docs/SPEC.md` §5, §6.1 egress allowlist); a hardcoded fleet IP was already removed once (commit `16d923a`) and stays removed.

## Shipped

| Date | Item | Commit |
| --- | --- | --- |
| 2026-09-12 | Studio token wired through CLI client and web cockpit | `97cdd69` |
| 2026-09-12 | Studio token sent on WS handshake, never printed in the URL log line | `9e79546` |
| 2026-09-12 | Per-turn queue, cancellable synth, named route failure, safe delete | `962acda` |
| 2026-09-12 | Barge lifecycle test against the real `Session`, not an inline copy | `6ad873b` |
| 2026-09-12 | Bounded stall cache, quantised client speed, dropped per-turn persona leak | `391daa4` |
| 2026-09-12 | Empty LLM stream surfaces `agent.error llm_no_sentences` instead of a fake-green turn | `ceae019` |
| 2026-09-12 | Barge arriving before turn task registers is no longer lost | `1d6382a` |
| 2026-09-12 | Studio token required on every mutating route; egress allowlist on `base_url` | `b20133c` |
| 2026-09-12 | `/settings` redaction matches contract; hardcoded fleet IP dropped from SenseVoice default | `16d923a` |
| 2026-09-12 | Settings hot-swap supplies a key correctly; fail-closed swap confirmed as intended | `74cfce1` |
| 2026-09-12 | TECH-SPEC/TECH-DESIGN/SPEC merged into one contract, synced to current code | `5b71112` |
| 2026-09-12 | Async turn loop, real speak queue, `app.py` split into modules (lane merge) | `2e03367` |
| 2026-09-12 | Speech pipeline split out of `turn.py` | `92d4b79` |
| 2026-09-12 | Base URL / model env chains fixed, `LITELLM_BASE_URL` takes precedence | `0c6f1c0` |
| 2026-09-12 | AX grounding honours the 300ms budget, `swallowed()` unbroken | `14207f9` |
| 2026-09-12 | Bounded QA runner kills the whole process group on timeout | `a29476a` |
| 2026-09-12 | `providers/llm.py` rebuilt after a duplicated module body | `88d99ab` |
| 2026-09-12 | Agent worktrees ignored in git | `5b83aee` |
| 2026-09-12 | Eyes suite added to the QA gate | `eabae29` |
| 2026-09-12 | Providers package, fail-closed factories, `word_times` (lane merge) | `b9dc4ec` |
| 2026-09-12 | Providers unreachable-host test uses a closed port, no fleet IP | `3a2f8aa` |
| 2026-09-12 | No fleet host in tree; LiteLLM base URL from `LITELLM_BASE_URL` | `f47aa80` |
| 2026-09-12 | Blocking host-specific reachability probe dropped; connect timeout maps to `provider_unreachable` | `3cd5c44` |
| 2026-09-12 | Built binaries untracked, `Makefile` with `qa`/`build` targets added | `67e6ffe` |
| 2026-09-12 | `say.sh`/`serve.sh` bounded, silent-capable, no absolute-path surprises | `ddfbe3d` |
| 2026-09-12 | QA fan-rule + silent audit for hotkey/HUD/earcons suites | `cb57f00` |
| 2026-09-12 | Hermetic turn-lifecycle gates | `7ee8bea` |
| 2026-09-12 | Real-engine suite gated, `afplay` test silenced, hardcoded `/tmp` paths dropped | `c09dcd3` |
| 2026-09-12 | Providers package, fail-closed factories (lane merge) | `697650d` |
| 2026-09-12 | Event loop unblocked, barge made real, `app.py` split | `25ce0ac` |
| 2026-09-12 | `redacted_repr` import hoisted to `stt.py` module top | `79f74d5` |
| 2026-09-12 | QA gate rewritten as an honest, bounded, silent-capable runner | `9bfbcd6` |
| 2026-09-12 | Runtime/settings/frames/grounding/queue modules added | `65b32bd` |
| 2026-09-12 | `qa/test_providers.py` added for lane-B fixes | `d0eabf5` |
| 2026-09-12 | Providers split into a package, hardcoded secrets stripped, `stream()` NameError fixed | `6ffa291` |
| 2026-09-12 | Zero-vision eyes lane merged | `c22b5bb` |
| 2026-09-12 | Attach affordance + eyes transcript blocks in the cockpit | `652bf74` |
| 2026-09-12 | Eyes lane: `server/eyes.py` + hermetic gates | `56a7b58` |
| earlier (Wave 1) | Full-duplex WS loop, personas, deterministic humanizer, QA harness | pre-`844a685` |
| earlier (Wave 2) | Native Carbon hotkey, notch HUD, earcons, universal dictation matrix | pre-`844a685` |

## Decision log

| Date | Decision | Alternatives rejected | Link |
| --- | --- | --- | --- |
| 2026-09-12 | One SPEC.md is the contract; TECH-SPEC.md and TECH-DESIGN.md become pointer stubs | Keeping three overlapping docs in sync by hand | `docs/SPEC.md`, commit `5b71112` |
| 2026-09-12 | `describe` OCR task ships disabled by default (`EYES_DESCRIBE_ENGINE` unset) | Defaulting to `apple-fm` and eating a 100+s hang per screenshot | `docs/SPEC.md` §8 |
| 2026-09-12 | Egress allowlist on every `*_base_url` in `POST /settings` | Trusting any host a client sends | `docs/SPEC.md` §6.1, commit `b20133c` |
