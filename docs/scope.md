# Scope and design

What this server does, what was measured to justify it, and what is
deliberately left out.

Every number here came from one real archive on one Mac: 1,193 note rows, 1,163
of them live, 37 folders, one iCloud account. It is a sample of one. It is also
the only sample that matters for deciding whether an approach is affordable,
and it is a great deal better than guessing.

## The split: read the database, write through the app

Reads come from `NoteStore.sqlite`. Writes go through Notes.app over
AppleScript. This is not a compromise between two half-good options; each half
is the only workable choice for its direction.

### Why reads do not use AppleScript

Driving AppleScript costs roughly 200ms per note. Reading and decoding the
whole archive from the database costs **24ms in total**:

```text
rows=1163  fetch=11ms  decode=13ms  ok=1163  errors=0
```

That is not a micro-optimization, it is a difference in what the server can
offer. A full-archive content search over AppleScript would take about four
minutes and time out long before finishing, so an AppleScript-based server can
only search *titles*. This one searches what the notes actually say, on every
query, and still answers in tens of milliseconds.

Three things also exist **only** in the database. AppleScript exposes none of
them at any speed:

- checklist items and whether each is ticked (2,090 items, 1,828 done)
- whether a note is pinned
- the note's full text, rather than the truncated `ZSNIPPET`

### Why writes do not use the database

The store is Core Data fronting CloudKit. Writing a row means forging Core
Data's change-tracking bookkeeping and the `ZSERVERRECORDDATA` CloudKit blob,
while Notes.app holds the file open. The realistic outcomes are a silent revert
on the next sync or a corrupted sync, against an archive with no undo. It was
not attempted and should not be.

AppleScript is the only supported way in. Everything below is a consequence of
that.

## What the note format turned out to be

`ZICNOTEDATA.ZDATA` is a compressed protobuf:

```text
NoteStoreProto { Document document = 2; }
Document       { Note note = 3; }
Note           { string text = 2; repeated AttributeRun runs = 5; }
```

`text` is the whole note as one flat string. Structure lives entirely in the
runs, each claiming a character count of that string.

Field numbers were established by counting occurrences across all 1,163 notes
and checking each against what the app displays, since Apple publishes no
schema. The survey that produced them:

```text
run fields:   1:80486  2:80446  13:68891  5:3742  9:299  7:241  12:170  ...
para fields:  3:70309  1:50469  9:43727   4:9139  5:2090 2:1823 ...
style types:  100:43846  0:3051  103:2090  102:509  101:415  2:286  4:149  1:123
checklist:    done=1828  not done=262
```

Two details that a specification would not have told us:

- **One note in 1,163 is bare zlib rather than gzip** (it starts `78 9c`). A
  decoder that only knows gzip loses it with a confusing `BadGzipFile`.
  Supporting both costs one branch, which is why `notedata.decompress` has it.
- **Runs do not respect line boundaries, and a styled phrase is often split
  across several.** One real note held "My position" as a bold run of `My po`
  followed by a bold run of `sition`; rendering them separately produced
  `**My po****sition**`, which no Markdown parser reads as bold. Hence
  `notedata._coalesce`.

## What AppleScript will and will not do

Tested against the real app by writing notes and reading the resulting
protobuf back, rather than trusting the dictionary.

| Markdown | Result |
| :--- | :--- |
| `- item` | bullet list (style 100) |
| `1. item` | numbered list (style 102) |
| nested by indentation | nests correctly |
| ` ``` ` | monospaced block (style 4) |
| `**bold**` `*italic*` `~~struck~~` | as expected |
| `[text](url)` | hyperlink, query string intact |
| `# Heading` | **plain bold text** |
| `- [ ] item` | **plain bullet** |

### Checklists cannot be created

Five HTML spellings were tried in one note: `<ul class="checklist">` with
`checked="false"`/`checked="true"`, `<ul class="Apple-checklist">`,
`<input type="checkbox">` bare, `<input>` inside an `<li>`, and a
`<span class="checkbox">` variant. Every one came back as paragraph style 100,
an ordinary bullet.

Reading checklist state works perfectly; only creation is impossible. Do not
add a tool claiming otherwise without a spelling that demonstrably works.

### Headings do not exist for scripts

`<h1>`, `<h2>` and `<h3>` all arrive as plain bold — `font_weight = 1`, no
paragraph style. Notes' own Title, Heading and Subheading styles are not
reachable. Bold is what the app renders closest to, so `#` becomes bold rather
than being dropped or left as a literal `#`.

### The title is the first line, and only the first line

`set name of note` returns without error and updates `ZTITLE1` alone. The
body's first line is untouched, so the list view and the open note disagree
permanently. Verified on a real note: title `Direct Name Set`, body still
beginning `Renamed Title`.

There is therefore no title parameter anywhere in this server.

### Reading a body back through AppleScript loses links

`body of note` returned `<u>link</u>` for a note whose stored protobuf clearly
held `https://example.com/`. **The href is gone.**

This is why `append_to_note` re-renders the note from the database and re-sends
the whole thing, rather than the obvious
`set body of n to (body of n) & extra`. The obvious version would strip every
URL in the note on every append. The same round trip also merged a dashed list
into a preceding numbered list.

### Folders can only be deleted through a filter clause

`folder id X` works as a *location* (`make new note at folder id X`) but not as
a direct object. These all fail with `-1728`:

```applescript
delete folder id "x-coredata://…"
set f to folder id "x-coredata://…"
delete folder "name"
tell account "iCloud" to delete folder id "x-coredata://…"
```

Only `delete (every folder whose id is "…")` works. It is also a **silent
no-op** when the id matches nothing — exit 0, no output — so the folder must be
confirmed to exist beforehand. An earlier draft of this project concluded
folder deletion was impossible, because it never tried the filter form.

## The database lags Notes.app

Notes checkpoints on its own schedule. Measured:

- **create** — visible within milliseconds
- **move** — visible immediately
- **delete a note** — minutes
- **delete a folder** — still absent from the database well after Notes.app had
  stopped listing the folder

So an unconfirmed write is reported as unconfirmed, never as failed. Calling it
failed would be wrong most of the time and would invite a retry that duplicates
the work. This was found the hard way: `delete_folder` reported a successful
deletion as a failure during the first end-to-end run.

## The rewrite guard

A write that is not a fresh create replaces the note's entire body. Two kinds
of content cannot be rebuilt, so `db.rewrite_hazards` names them and the tools
refuse unless the operator overrides:

| | Share of the reference archive |
| :--- | :--- |
| Notes with attachments | 74 of 1,163 — **6.4%** |
| Notes with checklists | 86 of 1,163 — **7.4%** |

Attachment types present: `public.tiff` (78), `public.png` (47),
`public.heic` (23), `public.jpeg` (7), `com.apple.notes.table` (5),
`public.url` (4), `com.apple.paper` (1), `com.adobe.pdf` (1).

So roughly **87% of notes edit with no compromise at all**, and the guard is
narrow enough to stay switched on.

Headings are deliberately not a hazard. They degrade to bold, which is
presentation rather than information — and Notes styles the first line of
nearly every note as a Title, so guarding on them would refuse almost every
write. A guard that fires on everything is a guard that gets disabled.

## Rows that are not notes

32 rows on the reference archive had `Z_ENT` of a note but no folder, no title,
no dates and no body — Core Data shells left behind by deletion. They must be
filtered out or they appear as 32 untitled empty notes. `db._LIVE_NOTE`
requires `ZFOLDER IS NOT NULL`.

A note in the trash is *moved* to the Recently Deleted folder rather than
flagged: after a delete, `ZMARKEDFORDELETION` stayed 0 and only `ZFOLDER`
changed. Filtering on the flag alone leaves trashed notes in every listing.

`Z_ENT` values themselves are assigned per store, so `db.entity_ids` looks them
up rather than hardcoding 12 and 15. A stale constant would make every query
return nothing at all, with no error to explain it.

## Knowing whether writes still work

Reads and writes fail independently here, and only one of them is visible.
Every read comes off the database, so a host whose Apple Events grant has been
revoked — from System Settings, or by the interpreter moving — serves every
read correctly and fails every write. Nothing else about it looks wrong.

So `/health` reports the outcome of the last write: when, which action, whether
it worked, and the exception's class name if it did not. `applescript.run`
records it, rather than each tool, so a write tool added later cannot forget.

Only `ok: false` is a fault. A null means the process has not been asked to
write since it started, which is ordinary after a restart and must not alert.

The class name is deliberate and the message is deliberately absent. `/health`
is unauthenticated and `healthcheck.sh` forwards what it finds to a ping
service off the host; a `ScriptError` carries osascript's stderr, and osascript
quotes the arguments it was given — which for a write is the whole note. This
is the same reasoning things-mcp applies to a Things URL, which embeds the auth
token, and dav-mcp to a `DavError`, which names the account principal.

## Deliberately not built

- **Attachment contents.** `get_note` reports that an image is there, not what
  is in it. Reading the files is a separate feature with its own size and
  privacy questions.
- **Tags and hashtags.** Notes stores them as `ICHashtag` rows; the reference
  archive has one. Not worth a tool until there is a reason.
- **Shared-note participants.** `ICNoteParticipant` exists and is unread.
- **Emptying the trash.** Not scriptable, and that is a good thing here.
- **Smart folders.** The reference archive has none, so there was nothing to
  test a reader against.
- **Multiple accounts.** The reference archive has exactly one (iCloud). The
  queries do not filter by account, so a second one would appear as extra
  folders rather than break — but this is untested, and `list_folders` would be
  the place to start.
- **Setting pin state.** Readable from the database, not settable from a
  script.
