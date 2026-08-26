"""Decoding a note body.

Every case here goes through the real gzip-and-protobuf path via
``notewriter``, not a stubbed structure, because the decoder is the part most
likely to be wrong about a format Apple does not document.
"""

import gzip

import pytest

from notes_mcp import notedata
from notes_mcp.notedata import to_markdown

from .support.notewriter import note_body, plain, run


def test_plain_text_round_trips():
    assert to_markdown(plain("Hello\nworld\n")) == "Hello\nworld"


def test_zlib_bodies_decode_too():
    """One note in 1,163 on the reference archive is zlib rather than gzip."""
    body = note_body("Old import\n", [run(11)], use_zlib=True)
    assert to_markdown(body) == "Old import"


def test_trailing_blank_lines_are_dropped():
    """Notes stores a trailing newline on nearly every note."""
    assert to_markdown(plain("One\n\n\n")) == "One"


def test_note_text_ignores_formatting():
    body = note_body("bold text\n", [run(4, bold=True), run(6)])
    assert notedata.note_text(body) == "bold text\n"


@pytest.mark.parametrize(
    "style,expected",
    [
        (notedata.TITLE, "# Heading"),
        (notedata.HEADING, "## Heading"),
        (notedata.SUBHEADING, "### Heading"),
        (notedata.BULLET, "- Heading"),
        (notedata.DASHED, "- Heading"),
    ],
)
def test_paragraph_styles(style, expected):
    assert to_markdown(note_body("Heading\n", [run(8, style=style)])) == expected


def test_numbered_lists_count_up():
    body = note_body(
        "one\ntwo\nthree\n",
        [run(4, style=102), run(4, style=102), run(6, style=102)],
    )
    assert to_markdown(body) == "1. one\n2. two\n3. three"


def test_numbering_restarts_after_a_break():
    """Two lists separated by a paragraph are two lists, not one."""
    body = note_body(
        "one\nbreak\none\n",
        [run(4, style=102), run(6), run(4, style=102)],
    )
    assert to_markdown(body) == "1. one\nbreak\n1. one"


def test_nested_numbering_is_per_level():
    body = note_body(
        "top\nsub\nsub\ntop\n",
        [
            run(4, style=102),
            run(4, style=102, indent=1),
            run(4, style=102, indent=1),
            run(4, style=102),
        ],
    )
    assert to_markdown(body) == "1. top\n  1. sub\n  2. sub\n2. top"


def test_checklists_carry_their_ticked_state():
    body = note_body(
        "done\ntodo\n",
        [run(5, style=103, checked=True), run(5, style=103, checked=False)],
    )
    assert to_markdown(body) == "- [x] done\n- [ ] todo"


def test_indent_nests_bullets():
    body = note_body(
        "top\ndeep\n",
        [run(4, style=100), run(5, style=100, indent=2)],
    )
    assert to_markdown(body) == "- top\n    - deep"


def test_monospaced_lines_become_one_fenced_block():
    body = note_body(
        "x = 1\ny = 2\nprose\n",
        [run(6, style=4), run(6, style=4), run(6)],
    )
    assert to_markdown(body) == "```\nx = 1\ny = 2\n```\nprose"


def test_unterminated_code_block_is_closed():
    body = note_body("x = 1\n", [run(6, style=4)])
    assert to_markdown(body) == "```\nx = 1\n```"


def test_inline_attributes():
    body = note_body(
        "bold and italic and struck\n",
        [
            run(4, bold=True),
            run(5),
            run(6, italic=True),
            run(5),
            run(6, strikethrough=True),
            run(1),
        ],
    )
    assert to_markdown(body) == "**bold** and *italic* and ~~struck~~"


def test_bold_and_italic_together():
    """Font weight is a bitmask, so 3 is both rather than a third style."""
    body = note_body("loud\n", [run(5, bold=True, italic=True)])
    assert to_markdown(body) == "***loud***"


def test_underline_is_dropped():
    """Notes underlines every link, so rendering it would decorate them all."""
    body = note_body("plain\n", [run(6, underlined=True)])
    assert to_markdown(body) == "plain"


def test_links_render_as_markdown():
    body = note_body("docs\n", [run(4, link="https://example.com/x"), run(1)])
    assert to_markdown(body) == "[docs](https://example.com/x)"


def test_a_bare_url_is_not_printed_twice():
    """Apple stores the URL as its own link text, and normalizes a trailing slash."""
    body = note_body(
        "https://example.com\n",
        [run(19, link="https://example.com/"), run(1)],
    )
    assert to_markdown(body) == "https://example.com/"


def test_adjacent_runs_with_one_style_are_merged():
    """Notes splits styled text as it is edited; rendering each separately
    emits ``**My po****sition**``, which reads as literal asterisks."""
    body = note_body("My position\n", [run(5, bold=True), run(6, bold=True), run(1)])
    assert to_markdown(body) == "**My position**"


def test_whitespace_is_kept_outside_the_markers():
    """"**bold **next" is not bold in any Markdown parser."""
    body = note_body("bold next\n", [run(5, bold=True), run(5)])
    assert to_markdown(body) == "**bold** next"


def test_attachments_become_placeholders():
    body = note_body("￼\n", [run(1, attachment="public.png"), run(1)])
    assert to_markdown(body) == "[attachment: public.png]"


def test_has_attachments():
    assert notedata.has_attachments(note_body("￼\n", [run(1, attachment="public.jpeg")]))
    assert not notedata.has_attachments(plain("nothing here\n"))


def test_text_past_the_last_run_is_not_lost():
    """Runs should cover the whole note; if they do not, keep the words."""
    body = note_body("covered\nuncovered\n", [run(8)])
    assert to_markdown(body) == "covered\nuncovered"


def test_a_body_that_will_not_decompress_raises():
    with pytest.raises(notedata.NoteDataError):
        to_markdown(b"not compressed at all")


def test_an_empty_body_raises():
    with pytest.raises(notedata.NoteDataError):
        to_markdown(b"")


def test_a_body_with_no_document_raises():
    with pytest.raises(notedata.NoteDataError):
        to_markdown(gzip.compress(b""))
