"""Decode a note body out of ``ZICNOTEDATA.ZDATA``.

The blob is a compressed protobuf. Three nested messages hold everything this
server reads::

    NoteStoreProto { Document document = 2; }
    Document       { Note note = 3; }
    Note           { string text = 2; repeated AttributeRun runs = 5; }

``text`` is the whole note as one flat string, newlines and all. Every scrap of
structure -- what is a bullet, what is bold, which checkbox is ticked -- lives
in ``runs``, each of which claims a character count of that string and carries
the attributes for it. So decoding is: read the text, then walk the runs to
learn what each stretch of it *is*.

Field numbers were not taken from a specification; Apple publishes none. They
were read off a real archive of 1,163 notes by counting which numbers occur and
checking each against what the app displays. The counts are in the comments
where they justify a decision.
"""

from __future__ import annotations

import gzip
import zlib

from . import proto

# NoteStoreProto -> Document -> Note.
_DOCUMENT = 2
_NOTE = 3

# Fields of Note.
_TEXT = 2
_RUN = 5

# Fields of AttributeRun.
_RUN_LENGTH = 1
_RUN_PARAGRAPH = 2
_RUN_FONT_WEIGHT = 5
_RUN_UNDERLINED = 6
_RUN_STRIKETHROUGH = 7
_RUN_LINK = 9
_RUN_ATTACHMENT = 12

# Fields of ParagraphStyle.
_PARA_STYLE_TYPE = 1
_PARA_INDENT = 4
_PARA_CHECKLIST = 5

# Fields of Checklist.
_CHECK_DONE = 2

# Fields of AttachmentInfo.
_ATTACH_UTI = 2

# Paragraph style types, all observed on a real archive.
TITLE = 0
HEADING = 1
SUBHEADING = 2
MONOSPACED = 4
BULLET = 100
DASHED = 101
NUMBERED = 102
CHECKLIST = 103

# Font weight is a bitmask, not an enum: 3 is bold *and* italic.
_BOLD = 1
_ITALIC = 2

# Apple parks one of these in the text wherever an attachment sits. It is the
# standard Unicode object replacement character, not an Apple invention.
OBJECT_REPLACEMENT = "￼"

LIST_STYLES = frozenset({BULLET, DASHED, NUMBERED, CHECKLIST})


class NoteDataError(ValueError):
    """A note body could not be decompressed or parsed."""


def decompress(data: bytes) -> bytes:
    """Inflate a ``ZDATA`` blob.

    Almost every note is gzip. Exactly one of the 1,163 on the reference
    archive is bare zlib (it starts ``78 9c``), and a decoder that only knows
    gzip loses that note with a confusing ``BadGzipFile``. Both are deflate
    underneath, so supporting the second costs one branch.
    """
    if not data:
        raise NoteDataError("note body is empty")
    try:
        if data[:2] == b"\x1f\x8b":
            return gzip.decompress(data)
        return zlib.decompress(data)
    except (OSError, zlib.error) as exc:
        raise NoteDataError(f"could not decompress note body: {exc}") from exc


def _note_message(data: bytes) -> bytes:
    raw = decompress(data)
    document = proto.first(raw, _DOCUMENT)
    if not isinstance(document, bytes):
        raise NoteDataError("note body has no document")
    note = proto.first(document, _NOTE)
    if not isinstance(note, bytes):
        raise NoteDataError("document has no note")
    return note


def note_text(data: bytes) -> str:
    """The note's plain text, with no formatting applied.

    This is the search path, called for every note in the archive on every
    query, so it stops at the text field and never touches the attribute runs.
    Decoding all 1,163 notes this way takes 24ms on the reference machine,
    which is what makes searching note bodies -- rather than just titles --
    affordable.
    """
    text = proto.first(_note_message(data), _TEXT)
    if text is None:
        return ""
    if not isinstance(text, bytes):
        raise NoteDataError("note text is not a string")
    # Apple's own text, so it is valid UTF-8; "replace" is here so one bad
    # byte costs a character rather than the whole note.
    return text.decode("utf-8", "replace")


class Run:
    """One stretch of note text and the attributes that apply to it."""

    __slots__ = (
        "length",
        "style",
        "indent",
        "checked",
        "bold",
        "italic",
        "strikethrough",
        "link",
        "attachment",
    )

    def __init__(self, blob: bytes):
        self.length = 0
        self.style: int | None = None
        self.indent = 0
        self.checked: bool | None = None
        self.bold = False
        self.italic = False
        self.strikethrough = False
        self.link: str | None = None
        self.attachment: str | None = None

        for number, _wire, value in proto.fields(blob):
            if number == _RUN_LENGTH and isinstance(value, int):
                self.length = value
            elif number == _RUN_PARAGRAPH and isinstance(value, bytes):
                self._read_paragraph(value)
            elif number == _RUN_FONT_WEIGHT and isinstance(value, int):
                self.bold = bool(value & _BOLD)
                self.italic = bool(value & _ITALIC)
            elif number == _RUN_STRIKETHROUGH and isinstance(value, int):
                self.strikethrough = bool(value)
            elif number == _RUN_LINK and isinstance(value, bytes):
                self.link = value.decode("utf-8", "replace")
            elif number == _RUN_ATTACHMENT and isinstance(value, bytes):
                uti = proto.first(value, _ATTACH_UTI)
                self.attachment = (
                    uti.decode("utf-8", "replace")
                    if isinstance(uti, bytes)
                    else "unknown"
                )
        # _RUN_UNDERLINED is named above but deliberately never read. Notes
        # underlines every hyperlink, so rendering it would decorate all 299
        # links on the reference archive with markup that says nothing about
        # them, and Markdown has no underline anyway.

    def _read_paragraph(self, blob: bytes) -> None:
        for number, _wire, value in proto.fields(blob):
            if number == _PARA_STYLE_TYPE and isinstance(value, int):
                self.style = value
            elif number == _PARA_INDENT and isinstance(value, int):
                self.indent = value
            elif number == _PARA_CHECKLIST and isinstance(value, bytes):
                done = proto.first(value, _CHECK_DONE)
                self.checked = bool(done)


def parse(data: bytes) -> tuple[str, list[Run]]:
    """Decode a note body into its text and its attribute runs."""
    note = _note_message(data)
    text = ""
    runs = []
    for number, _wire, value in proto.fields(note):
        if number == _TEXT and isinstance(value, bytes) and not text:
            text = value.decode("utf-8", "replace")
        elif number == _RUN and isinstance(value, bytes):
            runs.append(Run(value))
    return text, runs


class _Span:
    """A run clipped to the part of one line it covers."""

    __slots__ = ("text", "run")

    def __init__(self, text: str, run: Run):
        self.text = text
        self.run = run


def _lines(text: str, runs: list[Run]) -> list[list[_Span]]:
    """Cut the flat text into lines, each carrying the runs that cover it.

    A run does not respect line boundaries -- one run held ``"bullet
    one\\nbullet two\\n"`` on the reference archive -- and a line is routinely
    covered by several runs, one per formatting change. So both have to be
    walked together rather than either driving the other.
    """
    out: list[list[_Span]] = []
    current: list[_Span] = []
    pos = 0

    for run in runs:
        chunk = text[pos : pos + run.length]
        pos += run.length
        while chunk:
            head, sep, chunk = chunk.partition("\n")
            if head:
                current.append(_Span(head, run))
            if sep:
                out.append(current)
                current = []
            elif not chunk:
                break

    # Text past the last run still belongs to the note. Runs should cover it
    # all, but a note written by a version that formats differently should
    # lose formatting rather than lose words.
    if pos < len(text):
        tail = text[pos:]
        blank = Run(b"")
        while tail:
            head, sep, tail = tail.partition("\n")
            if head:
                current.append(_Span(head, blank))
            if sep:
                out.append(current)
                current = []
            elif not tail:
                break

    if current:
        out.append(current)
    return out


def _same_format(a: Run, b: Run) -> bool:
    """Whether two runs would render with identical inline markup."""
    return (
        a.bold == b.bold
        and a.italic == b.italic
        and a.strikethrough == b.strikethrough
        and a.link == b.link
        and not a.attachment
        and not b.attachment
    )


def _coalesce(spans: list[_Span]) -> list[_Span]:
    """Merge neighbouring spans that carry the same formatting.

    Notes splits a styled stretch across several runs as it is edited -- one
    real note held "My position" as a bold run of "My po" followed by a bold
    run of "sition". Rendering those separately emits ``**My po****sition**``,
    which no Markdown parser reads as one bold phrase, so the words come out
    wrapped in visible asterisks.
    """
    merged: list[_Span] = []
    for span in spans:
        if merged and _same_format(merged[-1].run, span.run):
            merged[-1] = _Span(merged[-1].text + span.text, merged[-1].run)
        else:
            merged.append(span)
    return merged


def _inline(spans: list[_Span]) -> str:
    """Render one line's spans, applying the character-level attributes."""
    parts = []
    for span in _coalesce(spans):
        run = span.run
        if run.attachment and span.text.strip(OBJECT_REPLACEMENT) == "":
            parts.append(f"[attachment: {run.attachment}]")
            continue

        text = span.text.replace(OBJECT_REPLACEMENT, "")
        if not text:
            continue

        # Whitespace is pulled outside the markers. "**bold **next" does not
        # render as bold in any Markdown parser, and Notes produces runs with
        # trailing spaces constantly.
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :]
        core = text.strip()
        if not core:
            parts.append(text)
            continue

        if run.strikethrough:
            core = f"~~{core}~~"
        if run.bold and run.italic:
            core = f"***{core}***"
        elif run.bold:
            core = f"**{core}**"
        elif run.italic:
            core = f"*{core}*"
        if run.link:
            # Notes stores the URL as the link text for anything typed as a
            # bare URL, and "[https://x](https://x)" prints it twice for no
            # gain. Apple normalizes "https://example.com" to a trailing
            # slash, so compare with that allowed for.
            if core == run.link or f"{core}/" == run.link:
                core = run.link
            else:
                core = f"[{core}]({run.link})"
        parts.append(f"{lead}{core}{trail}")
    return "".join(parts)


def to_markdown(data: bytes) -> str:
    """Render a note body as Markdown.

    Lossy in two directions that are worth naming, because both are Apple's
    limits rather than choices made here:

    - Notes' dotted and dashed lists both become ``-``. Markdown has one
      bullet, and writes always produce the dotted kind, which is Notes'
      default and 99% of what the reference archive contains.
    - Underline is dropped; see ``Run.__init__``.
    """
    text, runs = parse(data)
    lines = _lines(text, runs)

    out: list[str] = []
    numbering: dict[int, int] = {}
    in_code = False

    for spans in lines:
        run = spans[0].run if spans else None
        style = run.style if run else None
        indent = run.indent if run else 0
        body = _inline(spans)

        if style == MONOSPACED:
            if not in_code:
                out.append("```")
                in_code = True
            out.append(body)
            continue
        if in_code:
            out.append("```")
            in_code = False

        if style != NUMBERED:
            numbering.clear()

        pad = "  " * indent
        if style == TITLE:
            out.append(f"# {body}")
        elif style == HEADING:
            out.append(f"## {body}")
        elif style == SUBHEADING:
            out.append(f"### {body}")
        elif style == CHECKLIST:
            box = "x" if run and run.checked else " "
            out.append(f"{pad}- [{box}] {body}")
        elif style in (BULLET, DASHED):
            out.append(f"{pad}- {body}")
        elif style == NUMBERED:
            # Numbering restarts per indent level, and a deeper level resets
            # when the list comes back out to it.
            numbering[indent] = numbering.get(indent, 0) + 1
            for deeper in [k for k in numbering if k > indent]:
                del numbering[deeper]
            out.append(f"{pad}{numbering[indent]}. {body}")
        else:
            out.append(body)

    if in_code:
        out.append("```")

    # Notes stores a trailing newline on almost every note, which becomes a
    # final empty line here and is noise in every result.
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def has_attachments(data: bytes) -> bool:
    """Whether the body carries an inline attachment.

    A second opinion on the question ``db.attachment_count`` answers from the
    ``ICAttachment`` rows. Tables in particular show up here reliably, and a
    write that rewrites the body destroys them either way.
    """
    _text, runs = parse(data)
    return any(run.attachment for run in runs)
