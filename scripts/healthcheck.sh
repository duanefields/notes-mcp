#!/usr/bin/env bash
#
# Check a running notes-mcp server and optionally report to a dead-man's-switch
# service such as healthchecks.io.
#
# Run it from cron or a LaunchAgent on the machine hosting the server. Reporting
# outward matters: a monitor running on the same machine cannot tell you that the
# machine is gone, whereas a service expecting a regular ping can.
#
# Configuration comes from ~/.notes-mcp/check.env, if it exists:
#
#   HEALTH_URL=http://127.0.0.1:18793/health
#   PING_URL=https://hc-ping.com/your-uuid-here
#   EXPECTED_PYTHON=/Users/USERNAME/.local/share/uv/python/cpython-3.12.13-.../bin/python3.12
#   VENV_PYTHON=/Users/USERNAME/Code/notes-mcp/.venv/bin/python
#
# Everything is optional except HEALTH_URL. Without PING_URL it just prints its
# findings and exits non-zero on a problem, which is useful for running by hand.

set -uo pipefail

CONFIG="${NOTES_MCP_CHECK_ENV:-$HOME/.notes-mcp/check.env}"
# shellcheck source=/dev/null
[[ -f "$CONFIG" ]] && source "$CONFIG"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
PING_URL="${PING_URL:-}"
EXPECTED_PYTHON="${EXPECTED_PYTHON:-}"
VENV_PYTHON="${VENV_PYTHON:-}"

# There is deliberately no staleness check here. imessage-mcp has one because
# an archive that stops receiving is evidence of a broken sync; notes are
# different. Nobody writes a note on a schedule, so "no note has changed in a
# week" is an ordinary week, not a fault, and a threshold that fired on it
# would be turned off within a month. The note and folder counts are reported
# on every run instead, which is what a rebuilt or half-synced store would
# actually move.

problems=()
report=""

ping_hc() {
  [[ -z "$PING_URL" ]] && return 0
  curl -fsS -m 10 --retry 3 --data-raw "$2" "${PING_URL}${1}" >/dev/null 2>&1 || true
}

ping_hc "/start" ""

# --- is the server answering, and does it think it is healthy? ---------------
# A timeout here is meaningful: the documented failure mode on macOS is a
# privacy prompt that hangs the process rather than crashing it.
body=$(curl -fsS -m 15 "$HEALTH_URL" 2>/dev/null)
if [[ -z "$body" ]]; then
  problems+=("no response from $HEALTH_URL (down, or hung on a permission prompt)")
else
  # Parsed with plutil, which ships with macOS. Deliberately not /usr/bin/python3:
  # that is a Command Line Tools shim, and an OS update can leave it prompting to
  # install developer tools -- which would break this check exactly when an OS
  # update is the thing most likely to have broken something.
  jget() { printf '%s' "$body" | plutil -extract "$1" raw -o - - 2>/dev/null; }
  status=$(jget status);  database=$(jget database)
  notes=$(jget notes); folders=$(jget folders)
  python=$(jget python_version); notes_app=$(jget notes_running)

  if [[ -z "$status" ]]; then
    problems+=("could not parse the health response from $HEALTH_URL")
  fi

  report+="status=${status:-?} database=${database:-?} notes_app=${notes_app:-?}"
  report+=" notes=${notes:-?} folders=${folders:-?} python=${python:-?}"

  [[ -n "$status" && "$status" != "ok" ]] && problems+=("health status is '$status'")
  [[ -n "$database" && "$database" != "ok" ]] && problems+=("database is $database")
  # Reads still work without Notes; every write would fail. Worth reporting,
  # because nothing else about the server looks wrong when this happens.
  [[ "$notes_app" == "false" || "$notes_app" == "False" ]] &&
    problems+=("Notes.app is not running; every write will fail")

  # An archive that has gone empty means the store was rebuilt or the wrong
  # path is configured, not that the notes are gone. Either way the server is
  # serving nothing and nobody would otherwise notice.
  [[ "$notes" == "0" ]] && problems+=("the database reports 0 notes; wrong path, or the store was rebuilt")
fi

# --- has the interpreter moved out from under its privacy approval? ----------
# macOS grants Full Disk Access against a path, and tool-managed interpreters
# live at version-stamped paths. An upgrade silently revokes the grant, and the
# service then hangs on its next restart with no error anywhere. Catching the
# move is the only warning available before that happens.
if [[ -n "$EXPECTED_PYTHON" && -n "$VENV_PYTHON" ]]; then
  # readlink -f resolves the whole chain; stat -f %Y follows only one link, which
  # would stop at uv's stable alias and miss the versioned path that matters.
  actual=$(readlink -f "$VENV_PYTHON" 2>/dev/null)
  if [[ -z "$actual" ]]; then
    problems+=("cannot resolve $VENV_PYTHON")
  elif [[ "$actual" != "$EXPECTED_PYTHON" ]]; then
    problems+=("interpreter moved to $actual (approval was granted to $EXPECTED_PYTHON); re-grant Full Disk Access and update EXPECTED_PYTHON")
  fi
fi

# --- report ------------------------------------------------------------------
now=$(date '+%Y-%m-%d %H:%M:%S')

if (( ${#problems[@]} )); then
  msg="notes-mcp check FAILED"$'\n'"$report"$'\n'
  for p in "${problems[@]}"; do msg+="- $p"$'\n'; done
  printf '[%s] %s' "$now" "$msg"
  ping_hc "/fail" "$msg"
  exit 1
fi

# Logged on every run, not just failures, so the log doubles as a record of how
# the archive has been growing.
printf '[%s] notes-mcp OK %s\n' "$now" "$report"
ping_hc "" "OK $report"
