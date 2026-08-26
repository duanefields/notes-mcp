"""The tools.

Every write test goes through the ``script`` fixture, which stands in at the
``applescript`` boundary. Nothing here may reach the real Notes.app: a suite
that did would create and delete notes on whatever Mac it ran on.
"""

import json

import pytest

from notes_mcp import server

from .support import notestore


def text(result) -> str:
    return result.content if isinstance(result.content, str) else str(result.content)


def data(result) -> dict:
    return result.structured_content


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


async def test_list_folders(db_env):
    result = await server.list_folders()
    assert data(result)["total"] == 4
    assert "Projects/Archive" in text(result)
    assert notestore.INBOX in text(result)


async def test_list_notes(db_env):
    result = await server.list_notes()
    assert data(result)["total"] == 7
    assert "note_id:" in text(result)


async def test_list_notes_reports_the_true_total_when_it_truncates(db_env):
    result = await server.list_notes(limit=2)
    assert data(result)["count"] == 2
    assert data(result)["total"] == 7
    assert "Showing 1-2 of 7" in text(result)


async def test_list_notes_rejects_a_bad_folder(db_env):
    result = await server.list_notes(folder_id="SYNTHETIC-FOLDER-NOPE")
    assert "no folder with id" in data(result)["error"]


async def test_list_notes_rejects_an_unknown_sort(db_env):
    result = await server.list_notes(sort="sideways")
    assert "unknown sort" in data(result)["error"]


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (5, -1)])
async def test_pagination_is_validated(db_env, limit, offset):
    result = await server.list_notes(limit=limit, offset=offset)
    assert "error" in data(result)


async def test_get_note(db_env):
    result = await server.get_note(notestore.PLAIN_NOTE)
    assert data(result)["body"] == "Grocery list\nmilk\neggs"
    assert "Grocery list" in text(result)


async def test_get_note_reports_attachments(db_env):
    result = await server.get_note(notestore.ATTACHMENT_NOTE)
    assert data(result)["attachment_count"] == 1
    assert "1 attachment" in text(result)


async def test_get_note_rejects_an_unknown_id(db_env):
    result = await server.get_note("SYNTHETIC-NOTE-NOPE")
    assert "no note with id" in data(result)["error"]


async def test_search_notes(db_env):
    result = await server.search_notes("eggs")
    assert data(result)["total"] == 1
    assert data(result)["items"][0]["matched"] == "body"


async def test_search_says_so_when_nothing_matched(db_env):
    result = await server.search_notes("definitely-not-there")
    assert data(result)["total"] == 0
    assert "No notes matching" in text(result)


async def test_search_rejects_an_empty_query(db_env):
    result = await server.search_notes("   ")
    assert "must not be empty" in data(result)["error"]


async def test_reads_are_json_safe(db_env):
    """structured_content crosses the wire as JSON, so it has to survive that."""
    for result in (
        await server.list_folders(),
        await server.list_notes(),
        await server.get_note(notestore.RICH_NOTE),
        await server.search_notes("e"),
    ):
        json.dumps(data(result))


# ----------------------------------------------------------------------
# Creating
# ----------------------------------------------------------------------


async def test_create_note(script):
    result = await server.create_note("Shopping\n- milk")
    assert data(result)["created"]
    assert script.names == ["create_note"]
    assert "<ul><li>milk</li></ul>" in script.html


async def test_create_note_uses_a_named_folder(script):
    await server.create_note("Note", folder_id=notestore.PROJECTS)
    folder_argument = script.calls[0][1]
    assert folder_argument.endswith("/ICFolder/p11")
    assert notestore.STORE_UUID in folder_argument


async def test_create_note_rejects_an_unknown_folder(script):
    result = await server.create_note("Note", folder_id="SYNTHETIC-FOLDER-NOPE")
    assert "no folder with id" in data(result)["error"]
    assert script.calls == []


async def test_create_note_refuses_an_empty_body(script):
    result = await server.create_note("   ")
    assert "empty note" in data(result)["error"]
    assert script.calls == []


async def test_create_note_warns_that_checkboxes_became_bullets(script):
    result = await server.create_note("List\n- [ ] todo")
    assert "cannot create checklists" in text(result)


async def test_create_note_says_nothing_about_checklists_when_none_were_asked_for(script):
    result = await server.create_note("List\n- todo")
    assert "checklist" not in text(result)


# ----------------------------------------------------------------------
# The rewrite guard
# ----------------------------------------------------------------------


async def test_update_note(script):
    result = await server.update_note(notestore.PLAIN_NOTE, "New\nbody")
    assert data(result)["updated"]
    assert script.names == ["set_body"]
    assert script.html == "<div>New</div><div>body</div>"


async def test_update_refuses_a_note_with_attachments(script):
    result = await server.update_note(notestore.ATTACHMENT_NOTE, "Replacement")
    assert "refusing to rewrite" in data(result)["error"]
    assert "attachment" in data(result)["error"]
    # The refusal has to happen before Notes is touched, or it half-happened.
    assert script.calls == []


async def test_update_refuses_a_note_with_checklists(script):
    result = await server.update_note(notestore.CHECKLIST_NOTE, "Replacement")
    assert "refusing to rewrite" in data(result)["error"]
    assert "checklist" in data(result)["error"]
    assert script.calls == []


async def test_the_guard_can_be_overridden_deliberately(script):
    result = await server.update_note(
        notestore.ATTACHMENT_NOTE, "Replacement", replace_attachments=True
    )
    assert data(result)["updated"]
    assert data(result)["discarded"]
    assert "Discarded" in text(result)
    assert script.names == ["set_body"]


async def test_update_refuses_to_empty_a_note(script):
    result = await server.update_note(notestore.PLAIN_NOTE, "  ")
    assert "delete_note" in data(result)["error"]
    assert script.calls == []


async def test_update_rejects_an_unknown_note(script):
    result = await server.update_note("SYNTHETIC-NOTE-NOPE", "Body")
    assert "no note with id" in data(result)["error"]
    assert script.calls == []


# ----------------------------------------------------------------------
# Appending
# ----------------------------------------------------------------------


async def test_append_keeps_what_was_already_there(script):
    await server.append_to_note(notestore.PLAIN_NOTE, "bread")
    assert "milk" in script.html
    assert "eggs" in script.html
    assert "bread" in script.html


async def test_append_refuses_a_note_with_attachments(script):
    """Notes has no append, so this is a whole-note rewrite underneath."""
    result = await server.append_to_note(notestore.ATTACHMENT_NOTE, "more")
    assert "refusing to append" in data(result)["error"]
    assert script.calls == []


async def test_append_refuses_a_note_it_could_not_read(script):
    """Appending to a body that would not decode would replace it wholesale."""
    result = await server.append_to_note(notestore.BROKEN_NOTE, "more")
    assert "could not be decoded" in data(result)["error"]
    assert script.calls == []


async def test_append_refuses_nothing(script):
    result = await server.append_to_note(notestore.PLAIN_NOTE, "  ")
    assert "refusing to append" in data(result)["error"]
    assert script.calls == []


# ----------------------------------------------------------------------
# Moving and deleting
# ----------------------------------------------------------------------


async def test_move_note(script):
    result = await server.move_note(notestore.PLAIN_NOTE, notestore.PROJECTS)
    assert data(result)["moved"]
    assert script.names == ["move_note"]
    assert script.calls[0][2].endswith("/ICFolder/p11")


async def test_move_rejects_an_unknown_folder(script):
    result = await server.move_note(notestore.PLAIN_NOTE, "SYNTHETIC-FOLDER-NOPE")
    assert "no folder with id" in data(result)["error"]
    assert script.calls == []


async def test_delete_note_says_it_is_recoverable(script):
    result = await server.delete_note(notestore.PLAIN_NOTE)
    assert data(result)["deleted"]
    assert "Recently Deleted" in text(result)
    assert "30 days" in text(result)
    assert script.names == ["delete_note"]


async def test_delete_rejects_an_unknown_note(script):
    result = await server.delete_note("SYNTHETIC-NOTE-NOPE")
    assert "no note with id" in data(result)["error"]
    assert script.calls == []


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------


async def test_create_folder(script):
    result = await server.create_folder("Ideas")
    assert data(result)["created"]
    assert script.calls == [("create_folder", "Ideas", None)]


async def test_create_folder_can_nest(script):
    await server.create_folder("Ideas", parent_id=notestore.PROJECTS)
    assert script.calls[0][2].endswith("/ICFolder/p11")


async def test_create_folder_needs_a_name(script):
    result = await server.create_folder("  ")
    assert "needs a name" in data(result)["error"]
    assert script.calls == []


async def test_delete_folder_refuses_one_that_still_holds_notes(script):
    result = await server.delete_folder(notestore.INBOX)
    assert "3 notes" in data(result)["error"]
    assert script.calls == []


async def test_delete_folder_refuses_one_with_subfolders(script):
    result = await server.delete_folder(notestore.PROJECTS)
    assert "subfolder" in data(result)["error"]
    assert script.calls == []


async def test_delete_folder_will_not_touch_the_trash(script):
    result = await server.delete_folder(notestore.TRASH)
    assert "Recently Deleted is not a folder you can delete" in data(result)["error"]
    assert script.calls == []


async def test_delete_folder_does_not_call_a_slow_write_a_failure(script):
    """Folder deletes are the slowest write to reach disk -- a folder gone from
    Notes.app was still in the database long past the confirm window -- so an
    unconfirmed delete is reported as unconfirmed, not as a failure."""
    result = await server.delete_folder(notestore.INBOX, delete_notes_inside=True)
    assert data(result)["deleted"]
    assert data(result)["confirmed"] is False
    assert "has not caught up" in text(result)
    assert script.names == ["delete_folder"]


# ----------------------------------------------------------------------
# Notes.app being down
# ----------------------------------------------------------------------


@pytest.fixture
def notes_down(monkeypatch, db_env):
    from notes_mcp import applescript

    monkeypatch.setattr(applescript, "notes_is_running", lambda: False)


async def test_reads_work_without_notes_running(notes_down):
    """The database is on disk either way."""
    assert data(await server.list_notes())["total"] == 7


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.create_note("Note"),
        lambda: server.update_note(notestore.PLAIN_NOTE, "Body"),
        lambda: server.append_to_note(notestore.PLAIN_NOTE, "more"),
        lambda: server.move_note(notestore.PLAIN_NOTE, notestore.PROJECTS),
        lambda: server.delete_note(notestore.PLAIN_NOTE),
        lambda: server.create_folder("Ideas"),
        lambda: server.delete_folder(notestore.INBOX),
    ],
)
async def test_every_write_says_so_when_notes_is_not_running(notes_down, call):
    result = await call()
    assert "Notes is not running" in data(result)["error"]


# ----------------------------------------------------------------------
# AppleScript failing
# ----------------------------------------------------------------------


async def test_a_script_error_is_returned_not_raised(script):
    """A raised exception reaches the model as an opaque failure."""
    from notes_mcp import applescript

    script.error = applescript.ScriptError("Notes refused the operation: -1728")
    result = await server.create_note("Note")
    assert "-1728" in data(result)["error"]


async def test_a_timeout_explains_the_consent_dialog(script):
    from notes_mcp import applescript

    script.error = applescript.ScriptTimeout("Notes did not respond within 30s.")
    result = await server.update_note(notestore.PLAIN_NOTE, "Body")
    assert "did not respond" in data(result)["error"]


# ----------------------------------------------------------------------
# /health is public
# ----------------------------------------------------------------------


def test_health_does_not_publish_the_operators_username():
    """Custom routes are not behind the auth provider -- verified against a
    deployed sibling, which answered 200 to an unauthenticated request from the
    open internet. So the interpreter path must not start with /Users/<name>."""
    import os

    home = os.path.expanduser("~")
    assert server._tilde(f"{home}/.local/share/uv/python/x/bin/python3.12") == (
        "~/.local/share/uv/python/x/bin/python3.12"
    )


def test_tilde_leaves_a_path_outside_the_home_directory_alone():
    assert server._tilde("/usr/bin/python3") == "/usr/bin/python3"


async def test_health_payload_carries_no_home_path(db_env):
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/health", "headers": []}
    response = await server.health(Request(scope))
    body = response.body.decode()
    assert "/Users/" not in body
    assert '"status":"ok"' in body.replace(" ", "")
