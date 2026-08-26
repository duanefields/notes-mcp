"""Render query results as text for the model to read.

The tools return both a text channel and structured content. This module builds
the text half; the dicts from ``db`` are the structured half.
"""

from __future__ import annotations


def _date(value: str | None) -> str:
    """An ISO timestamp trimmed to the minute.

    Seconds and microseconds are noise in a listing, and they are the bulk of
    the characters in a timestamp.
    """
    if not value:
        return "unknown"
    return value[:16].replace("T", " ")


def format_folder(folder: dict) -> str:
    count = folder["note_count"]
    parts = [folder["path"], f"({count} note{'s' if count != 1 else ''})"]
    if folder["is_trash"]:
        parts.append("[Recently Deleted]")
    return f"{' '.join(parts)}\n  folder_id: {folder['folder_id']}"


def format_folders(folders: list[dict]) -> str:
    if not folders:
        return "No folders found."
    return "\n\n".join(format_folder(folder) for folder in folders)


def format_note_summary(note: dict) -> str:
    """One note as a listing entry: what it is, where it is, how to fetch it."""
    marks = []
    if note.get("pinned"):
        marks.append("pinned")
    if note.get("has_checklist"):
        marks.append("checklist")
    suffix = f" [{', '.join(marks)}]" if marks else ""

    lines = [f"{note['title']}{suffix}"]
    detail = note.get("excerpt") or note.get("snippet")
    if detail:
        lines.append(f"  {detail}")
    lines.append(f"  in {note.get('folder') or 'unknown folder'}, modified {_date(note.get('modified'))}")
    lines.append(f"  note_id: {note['note_id']}")
    return "\n".join(lines)


def format_notes(notes: list[dict]) -> str:
    if not notes:
        return "No notes found."
    return "\n\n".join(format_note_summary(note) for note in notes)


def format_note(note: dict) -> str:
    """One note in full, body included."""
    header = [note["title"]]
    marks = []
    if note.get("pinned"):
        marks.append("pinned")
    if note.get("in_trash"):
        marks.append("in Recently Deleted")
    if note.get("attachment_count"):
        count = note["attachment_count"]
        marks.append(f"{count} attachment{'s' if count != 1 else ''}")
    if marks:
        header.append(f"[{', '.join(marks)}]")

    lines = [
        " ".join(header),
        f"Folder: {note.get('folder') or 'unknown'}",
        f"Created: {_date(note.get('created'))}   Modified: {_date(note.get('modified'))}",
        f"note_id: {note['note_id']}",
        "",
        note.get("body") or "[this note is empty]",
    ]
    return "\n".join(lines)
