"""Read-only queries against the Apple Notes database.

Everything here opens the database read-only and never writes. Writes go
through Notes.app over AppleScript, not through this file: the store is Core
Data backed by CloudKit, so a row written behind Notes' back is either reverted
on the next sync or corrupts it, and neither is recoverable.

Notes keeps almost everything in one wide table, ``ZICCLOUDSYNCINGOBJECT``,
with ``Z_ENT`` saying which kind of object a row is and each entity using its
own subset of the 215 columns. That is why the column names look mismatched --
a note's title is ``ZTITLE1`` and a folder's is ``ZTITLE2``, because Core Data
numbered them as it merged the entities into one table.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sqlite3

from . import notedata

DEFAULT_DB_PATH = (
    pathlib.Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.notes"
    / "NoteStore.sqlite"
)

# Core Data's reference date, same as the rest of Apple's frameworks.
APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

# Z_ENT values, read from Z_PRIMARYKEY. They are assigned when the store is
# created and are stable for a given Notes version, but they are not constants
# Apple promises, so they are looked up rather than hardcoded -- see
# ``entity_ids``.
NOTE_ENTITY = "ICNote"
FOLDER_ENTITY = "ICFolder"
ATTACHMENT_ENTITY = "ICAttachment"

# ZFOLDERTYPE on the Recently Deleted folder. A deleted note is *moved* here
# rather than flagged: on the reference archive ZMARKEDFORDELETION stayed 0
# after a delete and only ZFOLDER changed. Filtering on the flag alone would
# leave trashed notes in every listing.
FOLDER_TYPE_TRASH = 1


class NotFound(LookupError):
    """No note or folder with that identifier."""


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open the notes database read-only.

    Read-only works against the live database, WAL and all, and returns rows
    Notes wrote seconds earlier -- verified. What it does *not* do is force
    Notes to flush: the app holds changes in memory and checkpoints on its own
    schedule, so a write can be invisible here for a while. See
    ``server._confirm``.
    """
    if path is not None:
        target = pathlib.Path(path)
    else:
        # Read at call time, not import time, so a launcher or a test can set
        # it after the module is loaded.
        target = pathlib.Path(os.environ.get("NOTES_MCP_DB_PATH") or DEFAULT_DB_PATH)
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def to_iso(seconds: float | None) -> str | None:
    """Convert a Core Data timestamp to an ISO 8601 string.

    Notes stores seconds since 2001 as a float, unlike Messages, which stores
    nanoseconds as an integer.
    """
    if seconds is None:
        return None
    return (APPLE_EPOCH + datetime.timedelta(seconds=seconds)).isoformat()


def entity_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Map entity names to their ``Z_ENT`` values.

    Core Data assigns these per store. They happen to be 12, 15 and 5 on the
    reference machine, but a hardcoded 12 that silently becomes something else
    after a Notes update would make every query return nothing at all, with no
    error to explain it. One cheap query removes the whole class of problem.
    """
    rows = conn.execute("SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY").fetchall()
    return {row["Z_NAME"]: row["Z_ENT"] for row in rows}


def store_uuid(conn: sqlite3.Connection) -> str:
    """The Core Data store's UUID.

    AppleScript addresses objects as ``x-coredata://<store-uuid>/ICNote/p<pk>``,
    and this is where that UUID comes from. Verified: the value in
    ``Z_METADATA`` is exactly what Notes returns for ``id of note``.
    """
    row = conn.execute("SELECT Z_UUID FROM Z_METADATA").fetchone()
    if row is None or not row["Z_UUID"]:
        raise NotFound("the notes database has no store UUID")
    return row["Z_UUID"]


# A note is real if it has a folder and is not flagged deleted. 32 rows on the
# reference archive had no folder, no title, no dates and no body -- Core Data
# shells left behind by deletion. They are not notes and must never be listed.
_LIVE_NOTE = "n.Z_ENT = :note AND n.ZFOLDER IS NOT NULL AND n.ZMARKEDFORDELETION = 0"

_NOTE_COLUMNS = """
    n.Z_PK AS pk, n.ZIDENTIFIER AS id, n.ZTITLE1 AS title, n.ZSNIPPET AS snippet,
    n.ZCREATIONDATE3 AS created, n.ZMODIFICATIONDATE1 AS modified,
    n.ZISPINNED AS pinned, n.ZHASCHECKLIST AS has_checklist,
    f.ZIDENTIFIER AS folder_id, f.ZTITLE2 AS folder, f.ZFOLDERTYPE AS folder_type
"""


def _params(conn: sqlite3.Connection, **extra) -> dict:
    ids = entity_ids(conn)
    return {
        "note": ids.get(NOTE_ENTITY, -1),
        "folder": ids.get(FOLDER_ENTITY, -1),
        "attachment": ids.get(ATTACHMENT_ENTITY, -1),
        **extra,
    }


def _note_row(row: sqlite3.Row) -> dict:
    return {
        "note_id": row["id"],
        "title": row["title"] or "Untitled",
        "snippet": row["snippet"] or "",
        "folder": row["folder"],
        "folder_id": row["folder_id"],
        "created": to_iso(row["created"]),
        "modified": to_iso(row["modified"]),
        "pinned": bool(row["pinned"]),
        "has_checklist": bool(row["has_checklist"]),
    }


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------


def list_folders(conn: sqlite3.Connection) -> list[dict]:
    """Every folder, with its parent and how many notes it holds.

    Unbounded on purpose. The reference archive has 37 folders against 1,163
    notes, and a folder listing that paginated would be useless -- this is the
    tool the other tools get their folder ids from, so it has to be complete.
    """
    rows = conn.execute(
        """
        SELECT f.Z_PK AS pk, f.ZIDENTIFIER AS id, f.ZTITLE2 AS title,
               f.ZFOLDERTYPE AS folder_type, p.ZIDENTIFIER AS parent_id,
               (SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT n
                 WHERE n.Z_ENT = :note AND n.ZFOLDER = f.Z_PK
                   AND n.ZMARKEDFORDELETION = 0) AS note_count
          FROM ZICCLOUDSYNCINGOBJECT f
          LEFT JOIN ZICCLOUDSYNCINGOBJECT p ON p.Z_PK = f.ZPARENT
         WHERE f.Z_ENT = :folder AND f.ZMARKEDFORDELETION = 0
         ORDER BY f.ZTITLE2 COLLATE NOCASE
        """,
        _params(conn),
    ).fetchall()

    by_id = {row["id"]: row for row in rows}

    def path(row: sqlite3.Row) -> str:
        """The folder's full path, so two "Archive" folders are tellable apart.

        ``seen`` guards against a parent cycle. One should be impossible, but
        this walk runs on every folder listing and a cycle would hang the
        server rather than return a wrong answer.
        """
        parts = [row["title"] or "Untitled"]
        seen = {row["id"]}
        cursor = row
        while cursor["parent_id"] and cursor["parent_id"] not in seen:
            parent = by_id.get(cursor["parent_id"])
            if parent is None:
                break
            seen.add(parent["id"])
            parts.append(parent["title"] or "Untitled")
            cursor = parent
        return "/".join(reversed(parts))

    return [
        {
            "folder_id": row["id"],
            "name": row["title"] or "Untitled",
            "path": path(row),
            "parent_id": row["parent_id"],
            "note_count": row["note_count"],
            "is_trash": row["folder_type"] == FOLDER_TYPE_TRASH,
        }
        for row in rows
    ]


def folder_pk(conn: sqlite3.Connection, folder_id: str) -> int:
    """The Z_PK for a folder identifier, for building an AppleScript id."""
    row = conn.execute(
        """
        SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT
         WHERE Z_ENT = :folder AND ZIDENTIFIER = :id AND ZMARKEDFORDELETION = 0
        """,
        _params(conn, id=folder_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"no folder with id '{folder_id}'")
    return row["Z_PK"]


def default_folder_id(conn: sqlite3.Connection) -> str | None:
    """A reasonable folder to create a note in when none was named.

    The most-used non-trash folder, rather than whatever Notes considers
    default: a note the operator cannot find is worse than one filed somewhere
    slightly unexpected, and the busiest folder is the one they look in.
    """
    row = conn.execute(
        """
        SELECT f.ZIDENTIFIER AS id,
               (SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT n
                 WHERE n.Z_ENT = :note AND n.ZFOLDER = f.Z_PK
                   AND n.ZMARKEDFORDELETION = 0) AS note_count
          FROM ZICCLOUDSYNCINGOBJECT f
         WHERE f.Z_ENT = :folder AND f.ZMARKEDFORDELETION = 0
           AND f.ZFOLDERTYPE != :trash
         ORDER BY note_count DESC
         LIMIT 1
        """,
        _params(conn, trash=FOLDER_TYPE_TRASH),
    ).fetchone()
    return row["id"] if row else None


# ----------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------

_SORTS = {
    "modified": "n.ZMODIFICATIONDATE1 DESC",
    "created": "n.ZCREATIONDATE3 DESC",
    "title": "n.ZTITLE1 COLLATE NOCASE ASC",
}


def list_notes(
    conn: sqlite3.Connection,
    *,
    folder_id: str | None = None,
    include_trash: bool = False,
    sort: str = "modified",
    limit: int = 25,
    offset: int = 0,
) -> list[dict]:
    """Notes as summaries, without decoding a single body.

    Pinned notes come first, matching what the app shows, then whatever ``sort``
    asks for.
    """
    order = _SORTS.get(sort, _SORTS["modified"])
    where = [_LIVE_NOTE]
    if folder_id is not None:
        where.append("f.ZIDENTIFIER = :folder_id")
    if not include_trash:
        where.append("f.ZFOLDERTYPE != :trash")

    rows = conn.execute(
        f"""
        SELECT {_NOTE_COLUMNS}
          FROM ZICCLOUDSYNCINGOBJECT n
          JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
         WHERE {" AND ".join(where)}
         ORDER BY n.ZISPINNED DESC, {order}
         LIMIT :limit OFFSET :offset
        """,
        _params(
            conn,
            folder_id=folder_id,
            trash=FOLDER_TYPE_TRASH,
            limit=limit,
            offset=offset,
        ),
    ).fetchall()
    return [_note_row(row) for row in rows]


def count_notes(
    conn: sqlite3.Connection,
    *,
    folder_id: str | None = None,
    include_trash: bool = False,
) -> int:
    where = [_LIVE_NOTE]
    if folder_id is not None:
        where.append("f.ZIDENTIFIER = :folder_id")
    if not include_trash:
        where.append("f.ZFOLDERTYPE != :trash")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
          FROM ZICCLOUDSYNCINGOBJECT n
          JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
         WHERE {" AND ".join(where)}
        """,
        _params(conn, folder_id=folder_id, trash=FOLDER_TYPE_TRASH),
    ).fetchone()
    return row["total"]


def get_note(conn: sqlite3.Connection, note_id: str) -> dict:
    """One note, with its body decoded to Markdown.

    Raises ``NotFound`` rather than returning None: every caller has to handle
    the missing case, and an exception cannot be forgotten the way a None can.
    """
    row = conn.execute(
        f"""
        SELECT {_NOTE_COLUMNS}, d.ZDATA AS data
          FROM ZICCLOUDSYNCINGOBJECT n
          JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
          LEFT JOIN ZICNOTEDATA d ON d.Z_PK = n.ZNOTEDATA
         WHERE {_LIVE_NOTE} AND n.ZIDENTIFIER = :id
        """,
        _params(conn, id=note_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"no note with id '{note_id}'")

    note = _note_row(row)
    note["in_trash"] = row["folder_type"] == FOLDER_TYPE_TRASH
    note["attachment_count"] = attachment_count(conn, row["pk"])

    if row["data"]:
        try:
            note["body"] = notedata.to_markdown(row["data"])
        except notedata.NoteDataError as exc:
            # A body that will not decode is worth reporting as such. Returning
            # an empty note would read as "this note is empty", which is a
            # stronger and more misleading claim than "this could not be read".
            note["body"] = f"[note body could not be decoded: {exc}]"
            note["decode_failed"] = True
    else:
        note["body"] = ""
    return note


def note_pk(conn: sqlite3.Connection, note_id: str) -> int:
    """The Z_PK for a note identifier, for building an AppleScript id."""
    row = conn.execute(
        f"""
        SELECT n.Z_PK AS pk
          FROM ZICCLOUDSYNCINGOBJECT n
          JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
         WHERE {_LIVE_NOTE} AND n.ZIDENTIFIER = :id
        """,
        _params(conn, id=note_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"no note with id '{note_id}'")
    return row["pk"]


def note_id_for_pk(conn: sqlite3.Connection, pk: int) -> str | None:
    """The identifier for a Z_PK, to name a note AppleScript just created."""
    row = conn.execute(
        "SELECT ZIDENTIFIER AS id FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = :pk",
        {"pk": pk},
    ).fetchone()
    return row["id"] if row else None


def folder_id_for_pk(conn: sqlite3.Connection, pk: int) -> str | None:
    """The identifier for a folder Z_PK, to name a folder just created."""
    return note_id_for_pk(conn, pk)


def attachment_count(conn: sqlite3.Connection, pk: int) -> int:
    """How many attachments hang off a note."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS total FROM ZICCLOUDSYNCINGOBJECT
         WHERE Z_ENT = :attachment AND ZNOTE = :pk
        """,
        _params(conn, pk=pk),
    ).fetchone()
    return row["total"]


def rewrite_hazards(conn: sqlite3.Connection, note_id: str) -> list[str]:
    """What rewriting this note's body would destroy, in words.

    Every write that is not a fresh create replaces the entire body, because
    AppleScript offers no way to edit part of one. Two kinds of content cannot
    be put back:

    - **Attachments.** Images, scanned documents, tables and PDFs live outside
      the text and there is no HTML that recreates them.
    - **Checklists.** Notes accepts no HTML spelling that produces a real
      checkbox -- five were tried -- so a note's ticked items come back as
      plain bullets, losing the state along with the boxes.

    Headings are deliberately *not* listed. They degrade to bold, which is
    presentation rather than information, and since Notes styles the first line
    of nearly every note as a Title, guarding on them would refuse almost every
    write and the guard would be turned off within a day.
    """
    row = conn.execute(
        f"""
        SELECT n.Z_PK AS pk, n.ZHASCHECKLIST AS has_checklist
          FROM ZICCLOUDSYNCINGOBJECT n
          JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
         WHERE {_LIVE_NOTE} AND n.ZIDENTIFIER = :id
        """,
        _params(conn, id=note_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"no note with id '{note_id}'")

    hazards = []
    count = attachment_count(conn, row["pk"])
    if count:
        hazards.append(
            f"{count} attachment{'s' if count != 1 else ''} "
            "(images, tables or scanned documents)"
        )
    if row["has_checklist"]:
        hazards.append("checklist items, whose ticked state cannot be recreated")
    return hazards


def search_notes(
    conn: sqlite3.Connection,
    query: str,
    *,
    folder_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Search titles and full note bodies, case-insensitively.

    The whole archive is searched, not a recent window, so a total of 0 means
    the text genuinely is not there.

    Bodies are matched in Python rather than SQL because they are compressed
    protobuf that SQLite cannot see into. That sounds expensive and is not:
    fetching and decoding all 1,163 bodies on the reference archive takes 24ms,
    which is why this searches note *content* at all rather than titles only,
    the way an AppleScript-driven server has to.
    """
    where = [_LIVE_NOTE, "f.ZFOLDERTYPE != :trash"]
    if folder_id is not None:
        where.append("f.ZIDENTIFIER = :folder_id")

    rows = conn.execute(
        f"""
        SELECT {_NOTE_COLUMNS}, d.ZDATA AS data
          FROM ZICCLOUDSYNCINGOBJECT n
          JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
          LEFT JOIN ZICNOTEDATA d ON d.Z_PK = n.ZNOTEDATA
         WHERE {" AND ".join(where)}
         ORDER BY n.ZISPINNED DESC, n.ZMODIFICATIONDATE1 DESC
        """,
        _params(conn, folder_id=folder_id, trash=FOLDER_TYPE_TRASH),
    ).fetchall()

    needle = query.casefold()
    matches = []
    for row in rows:
        title = row["title"] or ""
        in_title = needle in title.casefold()
        excerpt = None
        if not in_title and row["data"]:
            try:
                text = notedata.note_text(row["data"])
            except notedata.NoteDataError:
                continue
            position = text.casefold().find(needle)
            if position < 0:
                continue
            excerpt = _excerpt(text, position, len(query))
        elif not in_title:
            continue

        note = _note_row(row)
        note["matched"] = "title" if in_title else "body"
        if excerpt:
            note["excerpt"] = excerpt
        matches.append(note)

    return matches[offset : offset + limit], len(matches)


_EXCERPT_CONTEXT = 60


def _excerpt(text: str, position: int, length: int) -> str:
    """A one-line window around a match, so a hit is legible without opening it."""
    start = max(0, position - _EXCERPT_CONTEXT)
    end = min(len(text), position + length + _EXCERPT_CONTEXT)
    window = " ".join(text[start:end].split())
    return ("…" if start else "") + window + ("…" if end < len(text) else "")
