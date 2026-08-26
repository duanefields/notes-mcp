import pathlib
import sqlite3

import pytest

from .support import notestore


@pytest.fixture(scope="session")
def notes_db_path(tmp_path_factory) -> pathlib.Path:
    """A synthetic NoteStore.sqlite, built once for the session."""
    path = tmp_path_factory.mktemp("notes") / "NoteStore.sqlite"
    notestore.build(path).close()
    return path


@pytest.fixture
def conn(notes_db_path) -> sqlite3.Connection:
    from notes_mcp.db import connect

    connection = connect(notes_db_path)
    yield connection
    connection.close()


@pytest.fixture
def db_env(notes_db_path, monkeypatch):
    """Point the server's own ``db.connect()`` at the synthetic database.

    The tools open their own connections, so pointing them anywhere but the
    fixture would read the machine's real notes -- which must never happen,
    including on a developer's Mac where it would appear to work.
    """
    monkeypatch.setenv("NOTES_MCP_DB_PATH", str(notes_db_path))
    return notes_db_path


@pytest.fixture
def notes_running(monkeypatch):
    """Pretend Notes.app is up, since every write tool checks first."""
    from notes_mcp import applescript

    monkeypatch.setattr(applescript, "notes_is_running", lambda: True)


class FakeScript:
    """Records what would have been sent to osascript, and answers for it.

    The suite must never actually drive Notes: a test run would create and
    delete real notes on whatever Mac it ran on. This stands in at the
    ``applescript`` module boundary, which is the last point before the
    process is spawned.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if self.error is not None:
            raise self.error

    def create_note(self, folder_id, body_html):
        self._record("create_note", folder_id, body_html)
        return f"x-coredata://{notestore.STORE_UUID}/ICNote/p100"

    def set_body(self, note_id, body_html):
        self._record("set_body", note_id, body_html)

    def move_note(self, note_id, folder_id):
        self._record("move_note", note_id, folder_id)

    def delete_note(self, note_id):
        self._record("delete_note", note_id)

    def create_folder(self, name, parent_id=None):
        self._record("create_folder", name, parent_id)
        return f"x-coredata://{notestore.STORE_UUID}/ICFolder/p10"

    def delete_folder(self, folder_id):
        self._record("delete_folder", folder_id)

    @property
    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def html(self) -> str:
        """The body HTML from the last write that carried one."""
        for call in reversed(self.calls):
            if call[0] in ("create_note", "set_body"):
                return call[2]
        raise AssertionError("no write carried a body")


@pytest.fixture
def script(monkeypatch, db_env, notes_running):
    """Replace every AppleScript entry point with a recorder."""
    from notes_mcp import applescript, server

    fake = FakeScript()
    for name in (
        "create_note",
        "set_body",
        "move_note",
        "delete_note",
        "create_folder",
        "delete_folder",
    ):
        monkeypatch.setattr(applescript, name, getattr(fake, name))

    # Confirmation polls the database for a change the fake never makes, so
    # without shortening this every write test would sit through the timeout.
    monkeypatch.setattr(server, "_CONFIRM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(server, "_CONFIRM_POLL_SECONDS", 0.001)
    return fake
