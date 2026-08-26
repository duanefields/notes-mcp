# Apple Notes MCP

An MCP server for Apple Notes on macOS. It reads Notes' own SQLite store
directly and writes through Notes.app over AppleScript.

It runs over stdio for local use and over HTTP for remote use, so a phone or
tablet can reach the same notes the laptop does. That is the point of the
project: the desktop-only Notes integrations work well, but the desktop is
often the wrong place — the note you need to check or add to is usually needed
while you are away from it.

## Why read the database

Every other Apple Notes integration drives AppleScript for reads as well as
writes, which costs roughly 200ms per note. Reading the store instead means
fetching *and* decoding all 1,163 notes on the reference machine takes **24ms**
— about four orders of magnitude cheaper for a full-archive query.

That is not a micro-optimization. It is the difference between searching note
titles and searching what the notes actually say. It is also the only way to
read checklist state, pin state, or a note's full text at all: AppleScript
exposes none of them.

## Tools

**Read** — from the database, and they work whether or not Notes.app is running.

| Tool | |
| :--- | :--- |
| `list_folders` | The folder tree with paths and note counts. Where `folder_id`s come from. |
| `list_notes` | Note summaries, pinned first. Paginated. |
| `get_note` | One note in full, body rendered as Markdown. |
| `search_notes` | Searches titles **and full note bodies**, across the whole archive. |

**Write** — through Notes.app, so it must be running.

| Tool | |
| :--- | :--- |
| `create_note` | Markdown in; the first line becomes the title. |
| `update_note` | Replaces the whole body. Guarded — see below. |
| `append_to_note` | Adds to the end. Guarded — see below. |
| `move_note` | Move to another folder. |
| `delete_note` | To Recently Deleted, recoverable for 30 days. |
| `create_folder` | Optionally nested. |
| `delete_folder` | Refuses a folder that still holds notes unless told otherwise. |

## What Apple's scripting interface cannot do

These are limits of the Notes scripting interface, not choices made here. Each
was tested against the real app rather than assumed; `docs/scope.md` has the
evidence.

- **Checklists cannot be created.** Five HTML spellings were tried and every
  one came back as an ordinary bullet, so `- [ ]` becomes a plain bullet.
  Reading checklist state works fine — that comes from the database.
- **Headings become bold.** `<h1>`, `<h2>` and `<h3>` all arrive as plain bold
  text; Notes' own Title and Heading styles are not reachable from a script.
- **Editing replaces the entire note.** There is no partial edit, so
  attachments and checklists cannot survive one. `update_note` and
  `append_to_note` refuse rather than destroy them; the operator can override
  it deliberately.
- **A note's title is its first line.** `set name of note` appears to work and
  only changes the title in the list view, leaving the body's first line alone,
  so the two disagree from then on. There is no separate title to set.

## Requirements

- macOS, with Notes signed in
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Full Disk Access for the Python interpreter, to read the Notes store
- Permission to control Notes, which macOS prompts for on the first write

## Development

```bash
uv sync --extra test
uv run pytest
```

The test suite is offline and runs against a synthetic database built from
Apple's schema. It never opens the real one, so it works on a machine that has
never taken a note.

## Deployment

`docs/deployment-macos.md` covers running it as a LaunchAgent behind a tunnel,
including the privacy prompt that will otherwise hang the service on first
start.

## License

MIT
