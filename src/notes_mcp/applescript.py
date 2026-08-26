"""Write to Notes by driving Notes.app.

This is the only part of the server that changes anything. Writing to the
database directly is not an option: the store is Core Data fronting CloudKit,
so a row written behind Notes' back is reverted on the next sync at best and
corrupts it at worst, against notes that have no undo.

Every user-supplied value -- note text, folder names, object ids -- is passed
as an ``argv`` element rather than pasted into the script source, so there is no
escaping to get wrong and no injection surface. Verified that argv survives
quotes, backslashes, ``$(...)`` and backticks untouched.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"

# Generous, because this is not waiting on a network. It is the ceiling on the
# consent-prompt hang described in ``run``.
DEFAULT_TIMEOUT_SECONDS = 30

# AppleScript addresses objects by a Core Data URL. The store UUID comes from
# Z_METADATA and the number is the row's Z_PK -- verified identical to what
# Notes returns for `id of note`.
_OBJECT_ID = re.compile(r"^x-coredata://[0-9A-Fa-f-]+/IC(?:Note|Folder)/p\d+$")


class ScriptError(Exception):
    """Notes refused the operation, or could not be reached."""


class ScriptTimeout(ScriptError):
    """Notes did not answer, most likely waiting on a consent dialog."""


def object_id(store_uuid: str, entity: str, pk: int) -> str:
    """Build the AppleScript id for a note or folder row."""
    return f"x-coredata://{store_uuid}/{entity}/p{pk}"


def run(script: str, *args: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run an AppleScript with arguments and return its trimmed output.

    The timeout is not defensive padding. Controlling Notes needs Apple Events
    permission, and the first attempt raises a consent dialog that **blocks
    until somebody clicks it**. On an unattended host nobody does, so without a
    timeout the tool call hangs forever and the client eventually gives up with
    no explanation. Failing after 30 seconds with a message that names the real
    cause is far more useful.
    """
    try:
        result = subprocess.run(
            [OSASCRIPT, "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScriptTimeout(
            f"Notes did not respond within {timeout:.0f}s. This is usually the "
            "macOS consent dialog asking to control Notes, which blocks until "
            "someone answers it at the machine. Grant it there once, then retry."
        ) from exc
    except OSError as exc:
        raise ScriptError(f"Could not run osascript: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit status {result.returncode}"
        raise ScriptError(f"Notes refused the operation: {detail}")
    return result.stdout.strip()


def _returned_id(output: str) -> str:
    if not _OBJECT_ID.match(output):
        raise ScriptError(f"Notes returned something that is not an object id: {output!r}")
    return output


_CREATE_NOTE = """
on run argv
    set folderId to item 1 of argv
    set noteBody to item 2 of argv
    tell application "Notes"
        set theNote to make new note at folder id folderId with properties {body:noteBody}
        return id of theNote
    end tell
end run
"""


def create_note(folder_id: str, body_html: str) -> str:
    """Create a note in a folder and return its AppleScript id.

    There is no title parameter, here or in Notes: a note is called whatever
    its first line says. ``set name of note`` appears to succeed and only
    changes the title shown in the list, leaving the body's first line alone,
    so the two disagree from then on -- confirmed on a real note. The first
    line is the only title there is.
    """
    return _returned_id(run(_CREATE_NOTE, folder_id, body_html))


_SET_BODY = """
on run argv
    set noteId to item 1 of argv
    set newBody to item 2 of argv
    tell application "Notes"
        set body of note id noteId to newBody
    end tell
    return "ok"
end run
"""


def set_body(note_id: str, body_html: str) -> None:
    """Replace a note's entire body.

    Whole-body replacement is the only edit AppleScript offers; there is no way
    to change part of a note. Callers must check ``db.rewrite_hazards`` first --
    attachments and checklists do not survive this.
    """
    run(_SET_BODY, note_id, body_html)


_MOVE_NOTE = """
on run argv
    tell application "Notes"
        move note id (item 1 of argv) to folder id (item 2 of argv)
    end tell
    return "ok"
end run
"""


def move_note(note_id: str, folder_id: str) -> None:
    run(_MOVE_NOTE, note_id, folder_id)


_DELETE_NOTE = """
on run argv
    tell application "Notes"
        delete note id (item 1 of argv)
    end tell
    return "ok"
end run
"""


def delete_note(note_id: str) -> None:
    """Move a note to Recently Deleted.

    Not a purge: Notes keeps it for 30 days and the operator can restore it
    from the app. There is no scriptable way to empty the trash, which is a
    good thing here.
    """
    run(_DELETE_NOTE, note_id)


_CREATE_FOLDER = """
on run argv
    set folderName to item 1 of argv
    tell application "Notes"
        set theFolder to make new folder with properties {name:folderName}
        return id of theFolder
    end tell
end run
"""

_CREATE_SUBFOLDER = """
on run argv
    set parentId to item 1 of argv
    set folderName to item 2 of argv
    tell application "Notes"
        set theFolder to make new folder at folder id parentId with properties {name:folderName}
        return id of theFolder
    end tell
end run
"""


def create_folder(name: str, parent_id: str | None = None) -> str:
    """Create a folder, optionally inside another, and return its id."""
    if parent_id:
        return _returned_id(run(_CREATE_SUBFOLDER, parent_id, name))
    return _returned_id(run(_CREATE_FOLDER, name))


# Deleting a folder needs a filter clause, not a direct object.
#
# Notes accepts `folder id X` as a *location* -- `make new note at folder id X`
# works -- but refuses it as a direct object. `delete folder id X`, the
# two-step `set f to folder id X`, `delete folder "name"` and scoping under the
# account all fail with -1728, "Can't get folder". Only the filter form below
# works, which is why it is written this way rather than to match the other
# scripts here.
#
# It has a sharp edge: an id that matches nothing is a **silent no-op**, exit 0
# and no output. So the caller has to establish the folder exists beforehand
# and confirm it is gone afterwards; AppleScript will not say either way.
_DELETE_FOLDER = """
on run argv
    tell application "Notes"
        delete (every folder whose id is (item 1 of argv))
    end tell
    return "ok"
end run
"""


def delete_folder(folder_id: str) -> None:
    """Move a folder, and everything in it, to Recently Deleted.

    Deleting a folder takes its notes with it. Callers must count them first
    and make the operator agree -- this function cannot tell a folder that was
    deleted from one that never matched.
    """
    run(_DELETE_FOLDER, folder_id)


def notes_is_running() -> bool:
    """Whether Notes.app is up.

    Reads work without it -- the database is on disk either way -- but every
    write goes through Notes, so a host where it has quietly quit serves every
    read correctly and fails every write.

    Checked with pgrep rather than by asking Notes over AppleScript. Asking
    would need Apple Events permission, and that prompt cannot be pre-granted
    or answered on an unattended host, so a liveness check written that way
    would itself hang the thing it is meant to be checking.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Notes"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
