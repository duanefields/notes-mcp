# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## This repository is public

It is a server for reading and writing a personal notes archive, so the
ordinary rules about secrets are not enough. Assume every file here is
world-readable forever, including the git history, where a mistake cannot be
quietly deleted later.

**Never commit any of the following, in code, tests, fixtures, comments, docs,
commit messages, or example output:**

| Category         | Examples                                                        |
| :--------------- | :-------------------------------------------------------------- |
| Network identity | Hostnames, machine names, subdomains, IP addresses, tunnel URLs |
| Credentials      | Passwords, API keys, tokens, OAuth client secrets, certificates |
| Personal handles | Real phone numbers, email addresses, Apple IDs                  |
| Note content     | Real note text, titles, snippets, or decoded bodies             |
| Identifiers      | Real `ZIDENTIFIER` UUIDs, Core Data store UUIDs, `Z_PK` values  |
| Filesystem       | Real attachment paths, absolute paths containing a username     |

Use these instead:

- Phone numbers: the `555-01xx` range, which is reserved for fiction.
- Email addresses: `@example.com`.
- Hosts: `notes.example.com`, or "the host Mac" in prose. **Never the real
  machine's name**, even in a deployment doc where it would be convenient.
- Paths: `/Users/USERNAME/...`.
- Note and folder ids: an obvious synthetic form such as `SYNTHETIC-NOTE-0001`.
- The Core Data store UUID: `AAAAAAAA-0000-4000-8000-000000000001`.

Note identifiers deserve particular care. They are real UUIDs, they look like
harmless hex, and they are the one value that gets copied into a test while
debugging against the real archive. `scripts/scan-secrets.sh` greps for the
UUID shape for exactly this reason.

### Local notes are the escape hatch

Deployment genuinely needs real values -- the host's name, the tunnel URL, the
port. Those go in `docs/local/`, which is gitignored. Keep them there and keep
them out of tracked files, rather than inventing awkward workarounds in the
committed docs.

Anything tracked refers to the machine as "the host Mac" and uses
`notes.example.com`.

### What is safe

Apple's schema DDL is safe and is checked in at
`tests/support/notestore_schema.sql`. It is `CREATE TABLE` and `CREATE INDEX`
captured from a real database and contains no rows. Column names are not
private information.

### Tests must never touch the real archive

No test may open `~/Library/Group Containers/group.com.apple.notes/`, not even
read-only and not even skipped-by-default, and no test may drive Notes.app. The
suite runs against the synthetic fixture in `tests/support/notestore.py` and
fakes the AppleScript boundary in `conftest.FakeScript`, so it works on a
machine that has never taken a note and cannot leak anything if it fails
loudly in CI output.

A test that reached Notes.app would not merely leak -- it would **create and
delete real notes on whatever machine ran it**, including a contributor's.

Spot checks against the real archive are done by hand, from a scratch
directory, and the results are never pasted into the repository -- including
into a commit message or a test name.

### Before committing

```bash
./scripts/scan-secrets.sh
```

It masks the placeholders above and reports whatever survives. A hit is not
automatically a problem, but every hit needs a look before the commit lands.

## Commands

```bash
uv sync --extra test
uv run pytest
uv run pytest -k markdown
uvx ruff@0.16.4 check src tests
```

The suite is offline and touches no real data. Pin the ruff version here and in
`.github/workflows/ci.yml` together; ruff adds rules in minor releases and an
unpinned linter turns `main` red on a morning nobody touched anything. The rule
set is narrow on purpose (`E9`, `F`, `B`): bugs, not style.

## Architecture

Reads come from Notes' own SQLite store; writes go through Notes.app over
AppleScript. See `docs/scope.md` for what was measured to justify that split
and what each of Apple's limits cost to discover.

- `src/notes_mcp/proto.py` -- just enough protobuf to read a note body. Apple
  publishes no `.proto`, so the field numbers were read off a real archive.
- `src/notes_mcp/notedata.py` -- decodes `ZICNOTEDATA.ZDATA` into Markdown.
  This is where the format knowledge lives.
- `src/notes_mcp/markdown.py` -- the write half: Markdown to the HTML Notes
  accepts. A deliberate subset; see its docstring for what Notes honours.
- `src/notes_mcp/db.py` -- read-only queries. Also `rewrite_hazards`, which
  decides whether a note can be safely rewritten.
- `src/notes_mcp/applescript.py` -- the only code that changes anything.
- `src/notes_mcp/auth.py` -- password-guarded OAuth 2.1 provider for the HTTP
  transport. Ported from things-mcp by way of dav-mcp and imessage-mcp; there
  are now four copies, so fixes belong in the others too.

## Things Apple's scripting interface will not do

Each of these cost a real experiment to establish. Check here before assuming
the obvious approach works.

- **Checklists cannot be created.** `class="checklist"`,
  `class="Apple-checklist"`, `<input type="checkbox">` and two nesting variants
  were all tried; every one came back as an ordinary bullet (paragraph style
  100 rather than 103). Do not add a "create checklist" tool without first
  showing a spelling that works.
- **Headings do not exist for scripts.** `<h1>`, `<h2>` and `<h3>` all arrive
  as plain bold. There is no Title/Heading/Subheading style to set.
- **A note's title is its first line.** `set name of note` returns without
  error and updates `ZTITLE1` only, leaving the body's first line alone, so the
  list view and the note disagree from then on. Never expose a title setter.
- **`body of note` is lossy when read back.** It returns `<u>link</u>` for an
  `<a href>` -- **the URL is gone**. So append must re-render from the database
  (which keeps the link) rather than read-modify-write through AppleScript.
  `append_to_note` does it that way for exactly this reason.
- **A folder cannot be deleted by direct reference.** `delete folder id X`,
  `set f to folder id X`, `delete folder "name"` and scoping under the account
  all fail with `-1728`. Only `delete (every folder whose id is X)` works --
  and it is a **silent no-op** on an id that matches nothing, exit 0 with no
  output. Establish the folder exists before calling it.

## The database lags Notes.app

Notes holds changes in memory and checkpoints on its own schedule. A create
appeared within milliseconds on the reference machine; a **delete took
minutes**, and a folder delete was still not visible long after Notes.app had
stopped showing the folder.

So a write that has not shown up in the database is *unconfirmed*, never
*failed*. Saying it failed would be wrong far more often than it was right, and
would invite a retry that duplicates the work. `server._confirm` polls briefly
and every caller reports the difference; `delete_folder` had this backwards
once and called successful deletes failures.

## Never write to the database

The store is Core Data fronting CloudKit. A row written behind Notes' back is
reverted on the next sync at best and corrupts it at worst, against notes that
have no undo. Every write goes through `applescript.py`. `db.connect` opens
`mode=ro` and must stay that way.

## Rewriting a note destroys things

AppleScript replaces a note's *whole* body; there is no partial edit. Two kinds
of content cannot be put back, so `db.rewrite_hazards` names them and the tools
refuse:

- **Attachments** -- images, tables, scanned documents, PDFs. 6.4% of notes on
  the reference archive.
- **Checklists** -- the boxes cannot be recreated, so ticked state is lost.
  7.4% of notes.

Headings are deliberately **not** a hazard. They degrade to bold, which is
presentation rather than information, and Notes styles the first line of nearly
every note as a Title -- so guarding on them would refuse almost every write
and the guard would be switched off within a day. Guard what destroys
information, not what downgrades appearance.

## The AppleScript boundary is the injection surface

Note bodies and folder names are attacker-influenced in the ordinary case:
anyone who can send the operator text they paste into a note has written part
of the input. Every value goes to `osascript` as an **argv element**, never
interpolated into the script source, and the script text is a module-level
constant. `tests/test_applescript.py` pins this against strings that would do
something in a shell or inside an AppleScript literal.

An "escape the quotes and inline it" refactor would look reasonable and would
reopen the hole. Do not.
