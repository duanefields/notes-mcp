#!/usr/bin/env bash
#
# Pull the latest code and restart the server if anything changed.
#
# Intended for a machine that hosts the server and is not sat in front of, so
# that pushing to the tracked branch is enough to deploy.
#
# Configuration comes from ~/.notes-mcp/update.env, if it exists:
#
#   REPO_DIR=/Users/USERNAME/Code/notes-mcp
#   BRANCH=main
#   LAUNCH_LABEL=com.example.notes-mcp   # omit to skip restarting anything
#   PING_URL=https://hc-ping.com/your-uuid-here
#   RESET_HARD=true                       # discard local edits; deploy-only hosts
#
# Nothing happens when the branch has not moved, so this is cheap to run often.

set -uo pipefail

CONFIG="${NOTES_MCP_UPDATE_ENV:-$HOME/.notes-mcp/update.env}"
# shellcheck source=/dev/null
[[ -f "$CONFIG" ]] && source "$CONFIG"

REPO_DIR="${REPO_DIR:-$HOME/Code/notes-mcp}"
BRANCH="${BRANCH:-main}"
LAUNCH_LABEL="${LAUNCH_LABEL:-}"
PING_URL="${PING_URL:-}"
UV="${UV:-/opt/homebrew/bin/uv}"
# true makes the checkout match the branch exactly, discarding local edits. Right
# for a host that is only ever deployed to; wrong anywhere someone might be
# working in place, which is why it is off by default.
RESET_HARD="${RESET_HARD:-false}"

# mkdir is atomic and works without flock, which macOS lacks.
LOCKDIR="/tmp/notes-mcp-self-update.lock"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
ping_hc() { [[ -z "$PING_URL" ]] && return 0; curl -fsS -m 10 --retry 3 --data-raw "${2:-}" "${PING_URL}${1}" >/dev/null 2>&1 || true; }

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "another run is in progress, skipping"
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

ping_hc "/start"

cd "$REPO_DIR" || { log "no such directory: $REPO_DIR"; ping_hc "/fail" "missing $REPO_DIR"; exit 1; }

before=$(git rev-parse HEAD 2>/dev/null)
if ! out=$(git fetch --quiet origin "$BRANCH" 2>&1); then
  # A failed fetch is usually the network or the forge having a bad day. Leave
  # the checkout alone and let the next run pick it up.
  log "fetch failed, leaving the checkout untouched: $out"
  ping_hc "/fail" "fetch failed: $out"
  exit 1
fi

remote=$(git rev-parse "origin/$BRANCH" 2>/dev/null)
dirty=$(git status --porcelain --untracked-files=no)

# Nothing to do only when the branch has not moved AND there is nothing to undo.
# With RESET_HARD the promise is that the checkout matches the branch, so a stray
# edit sitting on the current commit still needs reverting -- otherwise it would
# survive indefinitely, since no future commit would ever trigger a correction.
if [[ "$before" == "$remote" ]]; then
  if [[ "$RESET_HARD" != "true" || -z "$dirty" ]]; then
    log "already up to date at ${before:0:8}"
    ping_hc "" "no change ${before:0:8}"
    exit 0
  fi
  log "at ${before:0:8} but the checkout is modified; restoring it"
fi

if [[ "$RESET_HARD" == "true" ]]; then
  # For a host that only ever runs the code and is never edited on. Matching the
  # branch exactly is more robust than fast-forwarding: it recovers by itself
  # from anything that would otherwise wedge deploys permanently -- a stray
  # edit, a half-applied change, a diverged history.
  [[ -n "$dirty" ]] && log "WARNING: discarding local modifications:"$'\n'"$dirty"
  if ! out=$(git reset --hard "origin/$BRANCH" 2>&1); then
    log "reset failed: $out"; ping_hc "/fail" "reset failed: $out"; exit 1
  fi
  log "reset to ${remote:0:8}"
else
  # Default. Somebody might be debugging in place, and discarding that silently
  # would be worse than skipping a deploy. Only tracked files count; untracked
  # ones are none of our business, and treating them as edits would let a stray
  # file block every future deploy.
  if [[ -n "$dirty" ]]; then
    log "tracked files are modified, refusing to update (set RESET_HARD=true to override)"
    ping_hc "/fail" "local modifications in $REPO_DIR; not updating"
    exit 1
  fi
  log "updating ${before:0:8} -> ${remote:0:8}"
  if ! out=$(git merge --ff-only "origin/$BRANCH" 2>&1); then
    log "fast-forward failed, leaving the checkout alone: $out"
    ping_hc "/fail" "ff-only merge failed: $out"
    exit 1
  fi
fi

# Dependencies may have moved with the code.
if ! out=$("$UV" sync 2>&1); then
  log "uv sync failed: $out"
  ping_hc "/fail" "uv sync failed"
  exit 1
fi

if [[ -n "$LAUNCH_LABEL" ]]; then
  plist="$HOME/Library/LaunchAgents/${LAUNCH_LABEL}.plist"
  log "restarting $LAUNCH_LABEL"
  launchctl unload "$plist" 2>/dev/null
  sleep 2
  launchctl load "$plist" 2>/dev/null
fi

log "updated to ${remote:0:8}: $(git log -1 --pretty=%s)"
ping_hc "" "updated to ${remote:0:8}"
