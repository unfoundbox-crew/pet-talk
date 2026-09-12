#!/usr/bin/env bash
# bin/agent-ctl.sh — install/uninstall/status for the pet-talk-server launchd
# user agent. Driven by `make install-agent` / `make uninstall-agent` /
# `make agent-status`; safe to run directly too.
#
# install:   renders launchd/com.unfoundbox.pet-talk-server.plist.template
#            with this checkout's absolute paths, writes it to
#            ~/Library/LaunchAgents/, then
#            `launchctl bootstrap gui/$(id -u) <plist>`.
# uninstall: `launchctl bootout gui/$(id -u)/com.unfoundbox.pet-talk-server`,
#            then removes the installed plist.
# status:    `launchctl print gui/$(id -u)/com.unfoundbox.pet-talk-server`
#            (falls back to a plain "not installed" if it isn't loaded).
#
# --dry-run on install/uninstall prints exactly what would happen (rendered
# plist path + contents, and the launchctl commands) without touching
# ~/Library or calling launchctl.
set -u

LABEL="com.unfoundbox.pet-talk-server"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/launchd/${LABEL}.plist.template"
LAUNCHER="$REPO_ROOT/bin/pet-talk-server"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/${LABEL}.plist"
BOOTSTRAP_LOG="$HOME/Library/Application Support/pet-talk/launchd-bootstrap.log"
UID_N="$(id -u)"

usage() {
  cat <<EOF
Usage: bin/agent-ctl.sh <install|uninstall|status> [--dry-run]

  install    render $TEMPLATE and load it as a launchd user agent
  uninstall  unload and remove the installed agent
  status     report whether the agent is loaded, and where its log lives
EOF
}

render_plist() {
  # sed -e per placeholder, not a single regex, so a path containing '&' or
  # '\' can never corrupt the substitution.
  sed -e "s#__PET_TALK_LAUNCHER__#${LAUNCHER}#g" \
      -e "s#__PET_TALK_BOOTSTRAP_LOG__#${BOOTSTRAP_LOG}#g" \
      "$TEMPLATE"
}

cmd_install() {
  local dry_run="${1:-0}"

  if [[ ! -f "$TEMPLATE" ]]; then
    echo "agent-ctl: FAIL — template not found: $TEMPLATE" >&2
    return 1
  fi
  if [[ ! -x "$LAUNCHER" ]]; then
    echo "agent-ctl: FAIL — launcher not found or not executable: $LAUNCHER" >&2
    return 1
  fi

  local rendered
  rendered="$(render_plist)"

  if [[ "$dry_run" == "1" ]]; then
    echo "agent-ctl install --dry-run"
    echo "  would write: $INSTALLED_PLIST"
    echo "  would run:   mkdir -p \"$LAUNCH_AGENTS_DIR\" \"$(dirname "$BOOTSTRAP_LOG")\""
    echo "  would run:   launchctl bootout gui/$UID_N/$LABEL   (ignored if not loaded)"
    echo "  would run:   launchctl bootstrap gui/$UID_N \"$INSTALLED_PLIST\""
    echo "  --- rendered plist ---"
    echo "$rendered"
    return 0
  fi

  mkdir -p "$LAUNCH_AGENTS_DIR" "$(dirname "$BOOTSTRAP_LOG")"
  printf '%s\n' "$rendered" > "$INSTALLED_PLIST"
  echo "agent-ctl: wrote $INSTALLED_PLIST"

  # bootout first so a re-install after an edit picks up the new plist —
  # bootstrap on an already-loaded label is a no-op error otherwise.
  launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/$UID_N" "$INSTALLED_PLIST"; then
    echo "agent-ctl: bootstrapped gui/$UID_N/$LABEL"
  else
    echo "agent-ctl: FAIL — launchctl bootstrap failed" >&2
    return 1
  fi
}

cmd_uninstall() {
  local dry_run="${1:-0}"

  if [[ "$dry_run" == "1" ]]; then
    echo "agent-ctl uninstall --dry-run"
    echo "  would run:   launchctl bootout gui/$UID_N/$LABEL"
    echo "  would remove: $INSTALLED_PLIST"
    return 0
  fi

  launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1
  local status=$?
  if [[ -f "$INSTALLED_PLIST" ]]; then
    rm -f "$INSTALLED_PLIST"
    echo "agent-ctl: removed $INSTALLED_PLIST"
  else
    echo "agent-ctl: no installed plist at $INSTALLED_PLIST"
  fi
  if [[ $status -ne 0 ]]; then
    echo "agent-ctl: note — $LABEL was not loaded (bootout returned $status), plist removal still ran"
  fi
}

cmd_status() {
  if [[ ! -f "$INSTALLED_PLIST" ]]; then
    echo "agent-ctl: not installed — no $INSTALLED_PLIST"
    return 0
  fi
  echo "agent-ctl: plist present at $INSTALLED_PLIST"
  local tmp
  tmp="$(mktemp)"
  if launchctl print "gui/$UID_N/$LABEL" >"$tmp" 2>&1; then
    grep -E "state = |pid = |last exit code = " "$tmp" || cat "$tmp"
  else
    echo "agent-ctl: plist installed but not loaded in gui/$UID_N (run: make install-agent)"
  fi
  rm -f "$tmp"
  echo "agent-ctl: log at $HOME/Library/Application Support/pet-talk/server.log"
}

SUBCMD="${1:-}"
shift || true
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "agent-ctl: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

case "$SUBCMD" in
  install) cmd_install "$DRY_RUN" ;;
  uninstall) cmd_uninstall "$DRY_RUN" ;;
  status) cmd_status ;;
  -h|--help|"") usage; [[ "$SUBCMD" == "" ]] && exit 2 || exit 0 ;;
  *) echo "agent-ctl: unknown subcommand: $SUBCMD" >&2; usage >&2; exit 2 ;;
esac
