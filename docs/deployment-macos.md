# Running as a background service on macOS

How to keep the HTTP server running on a Mac that nobody is sitting at, and the
macOS behaviors that will otherwise cost you an afternoon.

Real hostnames, ports and tunnel URLs belong in `docs/local/`, which is
gitignored. This file uses `notes.example.com` throughout.

## It must be a LaunchAgent, not a LaunchDaemon

Two reasons, and the second one only bites later.

Reading the notes database is protected by Full Disk Access, which is granted
per user. And writing drives Notes.app through Apple Events, which needs a
logged-in GUI session. A root LaunchDaemon has no session, so every write would
fail with nothing useful in the log.

Install into `~/Library/LaunchAgents/`, and make sure the machine auto-logs in
so the session exists after a reboot.

## Invoke the interpreter directly, not `uv run`

`uv run` spawns the interpreter as a child, so launchd supervises the wrapper.
Killing the job leaves the real server holding the port. Point
`ProgramArguments` at the venv's interpreter.

## Template

Save as `~/Library/LaunchAgents/com.example.notes-mcp.plist`, replacing the
placeholders. It contains a password, so `chmod 600` it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.notes-mcp</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/path/to/notes-mcp/.venv/bin/python</string>
    <string>-m</string>
    <string>notes_mcp</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/path/to/notes-mcp</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>NOTES_MCP_TRANSPORT</key><string>http</string>
    <key>NOTES_MCP_HOST</key><string>127.0.0.1</string>
    <key>NOTES_MCP_PORT</key><string>18792</string>
    <key>NOTES_MCP_AUTH</key><string>password</string>
    <key>NOTES_MCP_PASSWORD</key><string>REPLACE-WITH-A-LONG-RANDOM-VALUE</string>
    <key>NOTES_MCP_BASE_URL</key><string>https://notes.example.com</string>
    <key>NOTES_MCP_STATE_DIR</key><string>/Users/USERNAME/.notes-mcp</string>
    <!-- Without this, Python block-buffers to the log file and it stays empty,
         which makes a startup problem look like total silence. -->
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USERNAME/.notes-mcp/server.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.notes-mcp/server.log</string>
</dict>
</plist>
```

```bash
chmod 600 ~/Library/LaunchAgents/com.example.notes-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.notes-mcp.plist
curl -s localhost:18792/health
```

Bind to `127.0.0.1` and reach it through a tunnel or reverse proxy.

Pick a port that does not collide with the other MCP servers on the same host.

## `/health` is public, so keep it boring

`/health` is a custom route, and custom routes do **not** sit behind the auth
provider. Verified against a deployed server: the endpoint answered `200` to an
unauthenticated request from the open internet while `/mcp` did not.

That is deliberate and worth keeping. An external uptime monitor has to be able
to poll it without credentials, and a monitor running on the host cannot report
that the host is gone.

The consequence is a rule about the payload, not about the routing: **nothing
goes in it that you would not publish.** The interpreter path is reported
because a uv upgrade moving it is what silently voids Full Disk Access — but it
is reported with the home directory replaced by `~`, since the absolute form
begins with the operator's account name and publishing that buys nothing. The
version-stamped part, which is the half that carries the warning, survives.

If you would rather it not be reachable at all, a `404` rule ahead of the
service rule will do it — but then only the on-host cron check can see it:

```yaml
  - hostname: notes.example.com
    path: ^/health
    service: http_status:404
```

Do not put Cloudflare Access on the hostname either way — it intercepts the
OAuth callbacks and breaks the connector handshake.

## The privacy prompt that hangs the service

**This is the one that will catch you.** Reading
`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite` means
reading data macOS protects. A LaunchAgent has no approval for that, so on
first start the system raises a consent dialog — and if the Mac is headless or
unattended, that dialog sits unanswered and the process **blocks indefinitely
inside `open()`**.

It does not crash, does not log, and does not time out. `launchctl list`
reports it running with a healthy PID, the port is never bound, and the log
file is empty. With `KeepAlive` set, launchd keeps restarting it and each
attempt stacks another dialog.

Running the same command over SSH works fine, which is thoroughly misleading:
your shell inherits an approval that the LaunchAgent does not have.

**Fix:** grant Full Disk Access to the interpreter, before first launch. System
Settings → Privacy & Security → Full Disk Access → `+`, then `Cmd+Shift+G` in
the picker to type the path, since it is usually hidden. Grant the **resolved**
binary, not the venv symlink:

```bash
python3 -c "import os; print(os.path.realpath('.venv/bin/python'))"
```

If other uv-managed servers already run on the host, check before granting:
`uv` reuses one interpreter across every project on the same Python version, so
`readlink -f .venv/bin/python` may already point at a binary that has Full Disk
Access. If it does, there is nothing to grant. The same sharing is why an
interpreter upgrade breaks *every* such server at once.

Clicking Allow on the popup is often not enough. Interpreters installed by `uv`
and similar tools are ad-hoc signed with an empty identifier, and macOS binds
approvals to a code-signing identity. With nothing to bind to, the prompt
returns on every launch and the approval never sticks. An explicitly added Full
Disk Access entry is recorded against the path and does work.

### It will break again when the interpreter is upgraded

Tool-managed interpreters live at version-stamped paths:

```text
~/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12
```

Approval is granted against that path. A patch upgrade moves the binary,
silently invalidating it, and the service goes back to hanging on startup with
no error anywhere. A `.python-version` holding only `3.12` permits exactly that
upgrade.

`GET /health` reports the resolved interpreter path so the change is visible
before it bites. `scripts/healthcheck.sh` compares it against `EXPECTED_PYTHON`
and fails when it moves.

## Notes.app must be running

Only for writes — reads come off the database whether or not it is up. But a
host where Notes has quietly quit serves every read correctly and fails every
write, which is a confusing way to find out. Set the host to launch Notes at
login, and let `/health` report `notes_running` so the monitor catches it.

## The second prompt, the first time something is written

Writing drives Notes.app through Apple Events. That is a separate protection
from file access and produces its own dialog the first time a note is created:

> "python3.12" wants access to control "Notes".

Until it is answered the tool call hangs, the same way startup does. Every read
keeps working, so this can lie dormant and then surface the first time a note
is created from a phone.

Trigger it deliberately while you are at the machine rather than letting it
ambush a remote client later — create a throwaway note, then delete it. Like
Full Disk Access, it cannot be granted ahead of time: System Settings → Privacy
& Security → Automation only lets you toggle pairs macOS has already recorded.

`applescript.run` has a 30-second timeout so that an unanswered prompt fails
with an explanation rather than hanging the client forever.

## Monitoring

`scripts/healthcheck.sh` checks a running server and reports to a
dead-man's-switch service such as healthchecks.io. Configure it in
`~/.notes-mcp/check.env`:

```bash
HEALTH_URL=http://127.0.0.1:18792/health
PING_URL=https://hc-ping.com/your-uuid-here
EXPECTED_PYTHON=/Users/USERNAME/.local/share/uv/python/cpython-3.12.13-.../bin/python3.12
VENV_PYTHON=/Users/USERNAME/path/to/notes-mcp/.venv/bin/python
```

Expand `$REPO` and `~` yourself; cron does neither.

```cron
*/10 * * * * $REPO/scripts/healthcheck.sh >> ~/.notes-mcp/check.log 2>&1
```

`chmod 600` the config: the ping URL is a capability, not just an address.

It reports failure on four things:

- **No response.** Either down, or hung on a permission prompt. A timeout is
  meaningful here, since the documented failure mode is a hang rather than a
  crash.
- **The database is unreachable.** `/health` returns 503 and says so.
- **Notes.app is not running.** Reads keep working, so nothing else looks
  wrong, but every write would fail.
- **The interpreter moved.** The early warning for the privacy-approval problem
  above. Re-grant Full Disk Access and update `EXPECTED_PYTHON` together.

There is deliberately **no staleness check**, unlike the sibling iMessage
server. An archive that stops receiving messages is evidence of a broken sync;
notes are different. Nobody writes a note on a schedule, so "nothing has
changed in a week" is an ordinary week rather than a fault, and a threshold
that fired on it would be switched off within a month. The note and folder
counts are logged on every run instead — those are what a rebuilt or
half-synced store would actually move, and a sudden 0 is reported as a failure.

An outward ping is what makes the whole machine being gone detectable. A
monitor running on the same host cannot report its own host's death.

## Deploying by pushing

`scripts/self-update.sh` pulls the tracked branch, syncs dependencies if they
moved, and restarts the service, so a push is a deploy. Configure it in
`~/.notes-mcp/update.env`:

```bash
REPO_DIR=/Users/USERNAME/path/to/notes-mcp
BRANCH=main
LAUNCH_LABEL=com.example.notes-mcp   # omit to skip the restart
PING_URL=https://hc-ping.com/a-different-uuid
```

By default it refuses to touch a checkout whose tracked files have been
modified, assuming somebody is debugging in place. On a host that is only ever
deployed to, that assumption is wrong and expensive: one stray edit wedges
every future deploy and nobody is reading the log. Set `RESET_HARD=true` there.

One trap: **build the virtualenv where it will finally live.** `uv` records
absolute paths, so a venv created in one directory and then moved leaves the
editable install pointing at the old location, and the service fails with
`No module named notes_mcp`. Re-run `uv sync` after any move.

## Checking on it

```bash
launchctl list | grep notes-mcp      # pid, last exit code
curl -s localhost:18792/health       # liveness, counts, interpreter path
tail -f ~/.notes-mcp/server.log
```

A hang looks like: a live PID, nothing on the port, and an empty log. That is
the privacy prompt above, not a crash.
