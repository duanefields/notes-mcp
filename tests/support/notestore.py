"""Build a synthetic ``NoteStore.sqlite`` for the tests.

The real database cannot be used: this is a public repository and the tests
must run anywhere, on a machine that has never taken a note. So the fixture is
generated from Apple's own schema (``notestore_schema.sql``) and filled with
invented notes.

Building on the real schema rather than a hand-written subset means a query
that names a column wrong fails here, rather than passing against a convenient
approximation and failing on the real database. ``ZICCLOUDSYNCINGOBJECT`` has
215 columns and stores five different entities in them; that is exactly the
kind of shape a simplified fixture would quietly get wrong.
"""

from __future__ import annotations

import datetime
import pathlib
import sqlite3

from . import notewriter
from .notewriter import run

SCHEMA = pathlib.Path(__file__).with_name("notestore_schema.sql")

APPLE_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

# A fixed point in time, so every run produces the same database.
BASE = datetime.datetime(2026, 3, 1, 17, 0, tzinfo=datetime.timezone.utc)

# Arbitrary but fixed, and obviously synthetic.
STORE_UUID = "AAAAAAAA-0000-4000-8000-000000000001"

# Z_ENT values. Chosen to match a real store so a fixture that accidentally
# hardcodes one behaves the way the real thing would.
ENT_ATTACHMENT = 5
ENT_NOTE = 12
ENT_FOLDER = 15

FOLDER_TYPE_NORMAL = 0
FOLDER_TYPE_TRASH = 1


def apple_time(when: datetime.datetime) -> float:
    """Convert a datetime to Core Data's seconds-since-2001 encoding."""
    return (when - APPLE_EPOCH).total_seconds()


def _minutes(n: int) -> datetime.datetime:
    return BASE + datetime.timedelta(minutes=n)


def _folder(conn, pk, identifier, title, *, parent=None, folder_type=FOLDER_TYPE_NORMAL):
    conn.execute(
        """
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK, Z_ENT, Z_OPT, ZIDENTIFIER, ZTITLE2, ZPARENT, ZFOLDERTYPE,
             ZMARKEDFORDELETION)
        VALUES (?, ?, 1, ?, ?, ?, ?, 0)
        """,
        (pk, ENT_FOLDER, identifier, title, parent, folder_type),
    )


def _note(
    conn,
    pk,
    identifier,
    title,
    body,
    *,
    folder,
    created,
    modified,
    pinned=0,
    has_checklist=0,
    snippet=None,
    marked_for_deletion=0,
):
    """Insert a note and its body.

    The body lives in ``ZICNOTEDATA``, a separate table joined through
    ``ZNOTEDATA`` -- the same indirection the real store uses, so a query that
    forgets the join fails here too.
    """
    data_pk = pk
    conn.execute(
        "INSERT INTO ZICNOTEDATA (Z_PK, Z_ENT, Z_OPT, ZNOTE, ZDATA) VALUES (?, 19, 1, ?, ?)",
        (data_pk, pk, body),
    )
    conn.execute(
        """
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK, Z_ENT, Z_OPT, ZIDENTIFIER, ZTITLE1, ZSNIPPET, ZFOLDER, ZNOTEDATA,
             ZCREATIONDATE3, ZMODIFICATIONDATE1, ZISPINNED, ZHASCHECKLIST,
             ZMARKEDFORDELETION)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pk,
            ENT_NOTE,
            identifier,
            title,
            snippet if snippet is not None else title,
            folder,
            data_pk,
            apple_time(created),
            apple_time(modified),
            pinned,
            has_checklist,
            marked_for_deletion,
        ),
    )


def _attachment(conn, pk, note_pk, uti):
    conn.execute(
        """
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK, Z_ENT, Z_OPT, ZNOTE, ZTYPEUTI, ZIDENTIFIER, ZMARKEDFORDELETION)
        VALUES (?, ?, 1, ?, ?, ?, 0)
        """,
        (pk, ENT_ATTACHMENT, note_pk, uti, f"SYNTHETIC-ATTACHMENT-{pk}"),
    )


# Identifiers the tests refer to by name. Obviously synthetic, so a real UUID
# appearing in a failure message is a bug rather than a fixture value.
INBOX = "SYNTHETIC-FOLDER-0001"
PROJECTS = "SYNTHETIC-FOLDER-0002"
ARCHIVE = "SYNTHETIC-FOLDER-0003"
TRASH = "SYNTHETIC-FOLDER-TRASH"

PLAIN_NOTE = "SYNTHETIC-NOTE-0001"
RICH_NOTE = "SYNTHETIC-NOTE-0002"
CHECKLIST_NOTE = "SYNTHETIC-NOTE-0003"
ATTACHMENT_NOTE = "SYNTHETIC-NOTE-0004"
PINNED_NOTE = "SYNTHETIC-NOTE-0005"
ZLIB_NOTE = "SYNTHETIC-NOTE-0006"
TRASHED_NOTE = "SYNTHETIC-NOTE-0007"
BROKEN_NOTE = "SYNTHETIC-NOTE-0008"


def build(path: str | pathlib.Path) -> sqlite3.Connection:
    """Create a synthetic NoteStore.sqlite at ``path`` and return a connection."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA.read_text())

    conn.execute(
        "INSERT INTO Z_METADATA (Z_VERSION, Z_UUID, Z_PLIST) VALUES (1, ?, NULL)",
        (STORE_UUID,),
    )
    conn.executemany(
        "INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX) VALUES (?, ?, 0, 0)",
        [
            (ENT_ATTACHMENT, "ICAttachment"),
            (ENT_NOTE, "ICNote"),
            (13, "ICNoteContainer"),
            (14, "ICAccount"),
            (ENT_FOLDER, "ICFolder"),
            (19, "ICNoteData"),
        ],
    )

    _folder(conn, 1, TRASH, "Recently Deleted", folder_type=FOLDER_TYPE_TRASH)
    _folder(conn, 10, INBOX, "Inbox")
    _folder(conn, 11, PROJECTS, "Projects")
    # Nested, so path building and the parent chain are exercised.
    _folder(conn, 12, ARCHIVE, "Archive", parent=11)

    _note(
        conn,
        100,
        PLAIN_NOTE,
        "Grocery list",
        notewriter.plain("Grocery list\nmilk\neggs\n"),
        folder=10,
        created=_minutes(0),
        modified=_minutes(5),
        snippet="milk",
    )

    # Exercises every inline attribute and both list kinds at once.
    rich_text = (
        "Meeting notes\n"          # 14, title style
        "Plain line.\n"            # 12
        "bold"                     # 4
        " and "                    # 5
        "italic"                   # 6
        "\n"                       # 1
        "first\n"                  # 6, numbered
        "second\n"                 # 7, numbered
        "struck"                   # 6, strikethrough
        "\n"                       # 1
        "Anthropic"                # 9, link
        "\n"                       # 1
        "code line\n"              # 10, monospaced
    )
    _note(
        conn,
        101,
        RICH_NOTE,
        "Meeting notes",
        notewriter.note_body(
            rich_text,
            [
                run(14, style=0),
                run(12),
                run(4, bold=True),
                run(5),
                run(6, italic=True),
                run(1),
                run(6, style=102),
                run(7, style=102),
                run(6, strikethrough=True),
                run(1),
                run(9, link="https://example.com/docs", underlined=True),
                run(1),
                run(10, style=4),
            ],
        ),
        folder=11,
        created=_minutes(10),
        modified=_minutes(20),
    )

    checklist_text = "Packing\nsocks\ntent\nnested\n"
    _note(
        conn,
        102,
        CHECKLIST_NOTE,
        "Packing",
        notewriter.note_body(
            checklist_text,
            [
                run(8, style=0),
                run(6, style=103, checked=True),
                run(5, style=103, checked=False),
                run(7, style=103, checked=False, indent=1),
            ],
        ),
        folder=11,
        created=_minutes(30),
        modified=_minutes(40),
        has_checklist=1,
    )

    attach_text = "Receipts\n￼\ntotal was 12.00\n"
    _note(
        conn,
        103,
        ATTACHMENT_NOTE,
        "Receipts",
        notewriter.note_body(
            attach_text,
            [
                run(9, style=0),
                run(1, attachment="public.png"),
                run(1),
                run(15),
            ],
        ),
        folder=11,
        created=_minutes(50),
        modified=_minutes(60),
    )
    _attachment(conn, 200, 103, "public.png")

    _note(
        conn,
        104,
        PINNED_NOTE,
        "Always first",
        notewriter.plain("Always first\npinned content\n"),
        folder=10,
        created=_minutes(1),
        modified=_minutes(2),
        pinned=1,
    )

    _note(
        conn,
        105,
        ZLIB_NOTE,
        "Old import",
        notewriter.note_body(
            "Old import\nzlib rather than gzip\n",
            [run(32)],
            use_zlib=True,
        ),
        folder=12,
        created=_minutes(70),
        modified=_minutes(80),
    )

    _note(
        conn,
        106,
        TRASHED_NOTE,
        "Thrown away",
        notewriter.plain("Thrown away\n"),
        folder=1,
        created=_minutes(90),
        modified=_minutes(95),
    )

    # A body that will not decode, so the failure path is covered rather than
    # only imagined. Real archives do contain rows that cannot be read.
    _note(
        conn,
        107,
        BROKEN_NOTE,
        "Corrupt",
        b"this is not compressed protobuf",
        folder=10,
        created=_minutes(100),
        modified=_minutes(105),
    )

    # Core Data shells: no folder, no title, no body. 32 of these were on the
    # reference archive and none of them is a note.
    conn.execute(
        """
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, Z_OPT, ZIDENTIFIER, ZMARKEDFORDELETION)
        VALUES (900, ?, 1, 'SYNTHETIC-NOTE-SHELL', 0)
        """,
        (ENT_NOTE,),
    )

    conn.commit()
    return conn
