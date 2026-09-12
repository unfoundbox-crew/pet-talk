# CI

`ci.yml` runs on GitHub-hosted `ubuntu-latest` only — no macOS runners (10x minutes) and no self-hosted runners (this is a public repo; fork PRs would run arbitrary code on them). The Swift hotkey/HUD daemon is never built here; `qa/test_hotkey.py` and `qa/test_hud.py` self-skip on Linux (no `swiftc`) and read as SKIP, not a quiet green.
Three jobs: `python-gate` (`bash qa/run_all.sh` under `PET_TALK_SILENT=1 PET_TALK_HEADLESS=1`, stub providers), `web-build` (`npm ci && npm run build` plus vitest), `design-tokens` (`design/build.py --check`, and `design/sync-agentworth.sh --check` only when the sibling AgentWorth checkout is present — otherwise it prints a loud `::warning::` and skips rather than failing on a repo this runner will never have).
