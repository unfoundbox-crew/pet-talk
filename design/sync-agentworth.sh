#!/usr/bin/env bash
# design/sync-agentworth.sh — vendor AgentWorth's packages/ui/tokens.css into
# pet-talk at design/tokens.css, or check that the vendored copy hasn't
# drifted from the upstream source.
#
# pet-talk does not depend on AgentWorth as a package (packages/ui has no
# package.json to depend on), so this is a plain vendored copy plus a drift
# check, not a symlink or a build-time fetch.
#
# Usage:
#   design/sync-agentworth.sh            # copy AgentWorth's tokens.css here
#   design/sync-agentworth.sh --check    # exit 1 if the vendored copy differs
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${AGENTWORTH_TOKENS_CSS:-/Users/saurabh/code/unfoundbox/agentworth/packages/ui/tokens.css}"
DEST="$ROOT/design/tokens.css"

if [ ! -f "$SRC" ]; then
  echo "sync-agentworth: source not found at $SRC (set AGENTWORTH_TOKENS_CSS to override)" >&2
  exit 1
fi

if [ "${1:-}" = "--check" ]; then
  if [ ! -f "$DEST" ]; then
    echo "sync-agentworth --check: FAIL — $DEST does not exist" >&2
    exit 1
  fi
  if ! diff -u "$SRC" "$DEST" >/tmp/pet-talk-tokens-drift.diff 2>&1; then
    echo "sync-agentworth --check: FAIL — design/tokens.css has drifted from $SRC" >&2
    cat /tmp/pet-talk-tokens-drift.diff >&2
    rm -f /tmp/pet-talk-tokens-drift.diff
    exit 1
  fi
  rm -f /tmp/pet-talk-tokens-drift.diff
  echo "sync-agentworth --check: OK — design/tokens.css matches $SRC"
  exit 0
fi

cp "$SRC" "$DEST"
echo "sync-agentworth: copied $SRC -> $DEST"
