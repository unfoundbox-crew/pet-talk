# pet-talk — design

pet-talk keeps no palette of its own. The identity — zinc neutrals, one
violet accent, Geist / Geist Mono, three-state theming — is AgentWorth's,
documented in full at `/Users/saurabh/code/unfoundbox/agentworth/docs/DESIGN.md`.
Read that first.

pet-talk adds only what is genuinely its own, on top of AgentWorth's base:

- **Vendored base**: `design/tokens.css` is AgentWorth's `packages/ui/tokens.css`,
  copied verbatim (no package dependency exists to point at — see
  `design/sync-agentworth.sh`). Run `design/sync-agentworth.sh --check` to
  detect drift, `design/sync-agentworth.sh` (no flag) to re-vendor.
- **pet-talk's own layer**: `design/tokens.pet-talk.json` — notch/capsule
  geometry, spring motion constants, the type scale, and the listening-line
  plus receipt colours (`signal`, `peg`). It never redefines an AgentWorth
  colour or neutral; it only adds.
- **Generator**: `design/build.py` reads both source files and emits the
  three generated consumers below, each carrying a "generated, do not edit"
  header naming its sources.

## Rebuild

```bash
design/sync-agentworth.sh --check   # fails if design/tokens.css has drifted
python3 design/build.py --check     # fails if a generated file is stale
python3 design/build.py --write     # regenerates all three from the sources
```

## Generated files (never hand-edit)

| File | Consumer |
| --- | --- |
| `web/src/styles/tokens.css` | AgentWorth's tokens.css, passed through |
| `web/src/styles/tokens.pet-talk.css` | pet-talk's layer as CSS custom properties |
| `cli/hotkey/DesignTokens.swift` | `enum DesignTokens { static let ... }` |

`web/src/main.tsx` imports both generated CSS files. `cli/hotkey/*.swift`
reads geometry/motion/spring constants from `DesignTokens` rather than its
own literals — `cli/hotkey/hud_window.swift` currently defines its own
`HUDMotionTokens` struct with the pre-fix literals (`capsuleWidth = 220.0`,
notch is actually 180pt) and is owned by a concurrent lane; wiring it to read
from `DesignTokens` is that lane's integration step, not this file's.

Product rules that bind here (see `AGENTS.md` / `docs/SPEC.md`): no vendor
lock-in on providers, no engineering numbers or persona name on user-facing
surfaces, the mascot is Archie's small form — never a new creature — and
this design layer carries none of that on its own; it only supplies tokens.
