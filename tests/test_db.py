"""Queries against the synthetic NoteStore.

The fixture is built on Apple's real schema, so a query that names a column
wrong fails here rather than on the first real note.
"""

import pytest

from notes_mcp import db

from .support import notestore


def test_entity_ids(conn):
    """Core Data assigns Z_ENT per store, so they are looked up rather than
    hardcoded: a stale constant would return nothing at all, with no error."""
    ids = db.entity_ids(conn)
    assert ids["ICNote"] == notestore.ENT_NOTE
    assert ids["ICFolder"] == notestore.ENT_FOLDER


def test_store_uuid(conn):
    assert db.store_uuid(conn) == notestore.STORE_UUID


def test_to_iso_handles_the_core_data_epoch():
    # Core Data counts seconds from 2001-01-01 UTC.
    assert db.to_iso(0).startswith("2001-01-01T00:00:00")
    assert db.to_iso(None) is None


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------


def test_list_folders_returns_every_folder(conn):
    folders = {f["folder_id"]: f for f in db.list_folders(conn)}
    assert set(folders) == {
        notestore.INBOX,
        notestore.PROJECTS,
        notestore.ARCHIVE,
        notestore.TRASH,
    }


def test_folder_paths_show_nesting(conn):
    folders = {f["folder_id"]: f for f in db.list_folders(conn)}
    assert folders[notestore.ARCHIVE]["path"] == "Projects/Archive"
    assert folders[notestore.PROJECTS]["path"] == "Projects"


def test_folder_counts_exclude_nothing_but_deleted_rows(conn):
    folders = {f["folder_id"]: f for f in db.list_folders(conn)}
    assert folders[notestore.INBOX]["note_count"] == 3
    assert folders[notestore.ARCHIVE]["note_count"] == 1


def test_the_trash_folder_is_marked(conn):
    folders = {f["folder_id"]: f for f in db.list_folders(conn)}
    assert folders[notestore.TRASH]["is_trash"]
    assert not folders[notestore.INBOX]["is_trash"]


def test_folder_pk_raises_for_an_unknown_folder(conn):
    with pytest.raises(db.NotFound):
        db.folder_pk(conn, "SYNTHETIC-FOLDER-NOPE")


def test_default_folder_is_never_the_trash(conn):
    assert db.default_folder_id(conn) != notestore.TRASH


# ----------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------


def test_trashed_notes_are_hidden_by_default(conn):
    ids = {n["note_id"] for n in db.list_notes(conn, limit=50)}
    assert notestore.TRASHED_NOTE not in ids
    ids = {n["note_id"] for n in db.list_notes(conn, limit=50, include_trash=True)}
    assert notestore.TRASHED_NOTE in ids


def test_core_data_shells_are_not_notes(conn):
    """32 rows on the reference archive had no folder, title, dates or body."""
    ids = {n["note_id"] for n in db.list_notes(conn, limit=50, include_trash=True)}
    assert "SYNTHETIC-NOTE-SHELL" not in ids
    assert db.count_notes(conn, include_trash=True) == 8


def test_pinned_notes_come_first(conn):
    notes = db.list_notes(conn, limit=50)
    assert notes[0]["note_id"] == notestore.PINNED_NOTE


def test_notes_can_be_filtered_by_folder(conn):
    notes = db.list_notes(conn, folder_id=notestore.INBOX, limit=50)
    assert {n["folder"] for n in notes} == {"Inbox"}
    assert db.count_notes(conn, folder_id=notestore.INBOX) == 3


def test_sort_by_title(conn):
    titles = [n["title"] for n in db.list_notes(conn, sort="title", limit=50)]
    # Pinned still leads; the rest are alphabetical.
    assert titles[1:] == sorted(titles[1:], key=str.casefold)


def test_pagination_is_a_window_on_a_stable_order(conn):
    everything = db.list_notes(conn, limit=50)
    assert db.list_notes(conn, limit=2, offset=2) == everything[2:4]


def test_get_note_returns_the_decoded_body(conn):
    note = db.get_note(conn, notestore.PLAIN_NOTE)
    assert note["title"] == "Grocery list"
    assert note["body"] == "Grocery list\nmilk\neggs"
    assert note["folder"] == "Inbox"


def test_get_note_raises_for_an_unknown_note(conn):
    with pytest.raises(db.NotFound):
        db.get_note(conn, "SYNTHETIC-NOTE-NOPE")


def test_an_undecodable_body_says_so_rather_than_looking_empty(conn):
    """"This note is empty" is a stronger claim than "this could not be read"."""
    note = db.get_note(conn, notestore.BROKEN_NOTE)
    assert note["decode_failed"]
    assert "could not be decoded" in note["body"]


def test_attachment_count(conn):
    assert db.get_note(conn, notestore.ATTACHMENT_NOTE)["attachment_count"] == 1
    assert db.get_note(conn, notestore.PLAIN_NOTE)["attachment_count"] == 0


# ----------------------------------------------------------------------
# The rewrite guard
# ----------------------------------------------------------------------


def test_a_plain_note_can_be_rewritten(conn):
    assert db.rewrite_hazards(conn, notestore.PLAIN_NOTE) == []


def test_attachments_are_a_hazard(conn):
    hazards = db.rewrite_hazards(conn, notestore.ATTACHMENT_NOTE)
    assert len(hazards) == 1
    assert "attachment" in hazards[0]


def test_checklists_are_a_hazard(conn):
    hazards = db.rewrite_hazards(conn, notestore.CHECKLIST_NOTE)
    assert len(hazards) == 1
    assert "checklist" in hazards[0]


def test_headings_are_not_a_hazard(conn):
    """Notes styles the first line of almost every note as a Title. Guarding
    on that would refuse nearly every write, so a style downgrade is not
    treated as data loss."""
    assert db.rewrite_hazards(conn, notestore.RICH_NOTE) == []


def test_rewrite_hazards_raises_for_an_unknown_note(conn):
    with pytest.raises(db.NotFound):
        db.rewrite_hazards(conn, "SYNTHETIC-NOTE-NOPE")


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


def test_search_matches_titles(conn):
    matches, total = db.search_notes(conn, "Grocery")
    assert total == 1
    assert matches[0]["matched"] == "title"


def test_search_reads_note_bodies_not_just_titles(conn):
    """The whole point of reading the database rather than driving AppleScript."""
    matches, total = db.search_notes(conn, "eggs")
    assert total == 1
    assert matches[0]["matched"] == "body"
    assert matches[0]["note_id"] == notestore.PLAIN_NOTE


def test_a_body_match_carries_an_excerpt(conn):
    matches, _ = db.search_notes(conn, "eggs")
    assert "eggs" in matches[0]["excerpt"]


def test_search_is_case_insensitive(conn):
    assert db.search_notes(conn, "EGGS")[1] == 1


def test_search_finds_nothing_when_there_is_nothing(conn):
    matches, total = db.search_notes(conn, "definitely-not-in-any-note")
    assert (matches, total) == ([], 0)


def test_search_skips_the_trash(conn):
    """A note the operator threw away should not come back in a search."""
    assert db.search_notes(conn, "Thrown away")[1] == 0


def test_search_can_be_scoped_to_a_folder(conn):
    assert db.search_notes(conn, "milk", folder_id=notestore.INBOX)[1] == 1
    assert db.search_notes(conn, "milk", folder_id=notestore.PROJECTS)[1] == 0


def test_search_survives_a_note_it_cannot_decode(conn):
    """One corrupt body must not take the whole query down with it."""
    assert db.search_notes(conn, "eggs")[1] == 1


def test_search_totals_are_true_counts_not_page_sizes(conn):
    _, total = db.search_notes(conn, "e", limit=1)
    assert total > 1


def test_search_pagination(conn):
    everything, total = db.search_notes(conn, "e", limit=50)
    page, page_total = db.search_notes(conn, "e", limit=1, offset=1)
    assert page_total == total
    assert page == everything[1:2]
