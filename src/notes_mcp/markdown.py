"""Turn Markdown into the HTML Notes accepts as a note body.

AppleScript's ``body`` property takes HTML, so this is the write half of what
``notedata`` reads. It is a small, deliberate subset rather than a Markdown
implementation: only constructs Notes actually honours are emitted, because
anything else silently degrades and a converter that pretends otherwise
produces notes that do not match what was asked for.

What Notes does with each construct was tested against the real app, not
assumed:

======================  ====================================================
Markdown                Result in Notes
======================  ====================================================
``- item``              bullet list
``1. item``             numbered list
```` ``` ````           monospaced block
``**bold**``            bold
``*italic*``            italic
``~~struck~~``          strikethrough
``[text](url)``         hyperlink
``# Heading``           **bold text, not a heading** -- see below
``- [ ] item``          **plain bullet, not a checklist** -- see below
======================  ====================================================

Two of those are Apple's limits and cannot be worked around from here:

*Headings.* ``<h1>``, ``<h2>`` and ``<h3>`` all arrive as plain bold text;
Notes' own Title/Heading/Subheading styles are not reachable through the
scripting interface. Bold is what the app itself renders closest to, so ``#``
becomes bold rather than being dropped or left as a literal ``#``.

*Checklists.* Five different HTML spellings were tried -- ``class="checklist"``,
``class="Apple-checklist"``, ``<input type="checkbox">``, and two nesting
variants -- and every one came back as an ordinary bullet. A note's existing
checklists therefore cannot survive a rewrite either, which is why
``server`` refuses to rewrite a note that has them.
"""

from __future__ import annotations

import html
import re

# Bullet, dash and plus all mean the same thing in Markdown, and Notes has one
# kind of bulleted list to put them in.
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*```")

# A leading checkbox on a list item. Matched only to strip it: the box cannot
# be recreated, and leaving "[ ]" in the text would look like a checklist that
# does not work.
_CHECKBOX = re.compile(r"^\[([ xX])\]\s+(.*)$")

_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD_ITALIC = re.compile(r"\*\*\*(\S(?:.*?\S)?)\*\*\*")
_BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*|__(\S(?:.*?\S)?)__")
_ITALIC = re.compile(r"(?<![*\w])\*(\S(?:.*?\S)?)\*(?!\*)|(?<![_\w])_(\S(?:.*?\S)?)_(?!\w)")
_STRIKE = re.compile(r"~~(\S(?:.*?\S)?)~~")
_CODE = re.compile(r"`([^`]+)`")

# Where a parked code span went, so it can be put back last.
_PARKED = re.compile(r"\0(\d+)\0")

# How many spaces of Markdown indentation count as one nesting level. Two is
# the tighter of the two conventions and the one a model writing a nested list
# is most likely to use; four still rounds to level one at worst.
_INDENT_WIDTH = 2


def _inline(text: str) -> str:
    """Render one line's inline markup.

    HTML-escaping happens first so that a note containing ``<`` or ``&`` is not
    read as markup, and every tag added afterwards is deliberate. None of the
    Markdown markers are touched by escaping, so the order is safe.
    """
    # NULs cannot occur in a note and are used below to fence placeholders, so
    # dropping them first stops crafted input from forging one.
    out = html.escape(text.replace("\0", ""), quote=False)

    # A backtick span is literal, so the markers inside it must survive every
    # substitution that follows. Emitting <tt> here is not enough -- the bold
    # and italic passes would still see through the tags and format the code --
    # so its contents are parked and restored once everything else has run.
    parked: list[str] = []

    def park(match: re.Match[str]) -> str:
        parked.append(match.group(1))
        return f"\0{len(parked) - 1}\0"

    out = _CODE.sub(park, out)
    # The URL has already been through html.escape above, so only the quote
    # character is still left to handle -- escaping it again here would turn
    # a query string's "&" into "&amp;amp;" and break the link.
    out = _LINK.sub(
        lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">'
        f"{m.group(1) or m.group(2)}</a>",
        out,
    )
    out = _BOLD_ITALIC.sub(lambda m: f"<b><i>{m.group(1)}</i></b>", out)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", out)
    out = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", out)
    return _PARKED.sub(lambda m: f"<tt>{parked[int(m.group(1))]}</tt>", out)


class _Lists:
    """The open ``<ul>``/``<ol>`` stack, so nesting by indentation works."""

    def __init__(self, out: list[str]):
        self._out = out
        self._open: list[tuple[int, str]] = []

    def item(self, level: int, tag: str, body: str) -> None:
        # Close any list deeper than this item, and any at the same depth that
        # is the wrong kind -- switching from bullets to numbers at one level
        # is a new list, not a continuation.
        while self._open and (
            self._open[-1][0] > level
            or (self._open[-1][0] == level and self._open[-1][1] != tag)
        ):
            self._out.append(f"</{self._open.pop()[1]}>")
        while not self._open or self._open[-1][0] < level:
            depth = self._open[-1][0] + 1 if self._open else 0
            self._open.append((depth, tag))
            self._out.append(f"<{tag}>")
            if depth >= level:
                break
        self._out.append(f"<li>{body}</li>")

    def close(self) -> None:
        while self._open:
            self._out.append(f"</{self._open.pop()[1]}>")


def to_html(text: str) -> str:
    """Convert Markdown to a Notes-compatible HTML body.

    The first line becomes the note's title, because that is how Notes decides
    what a note is called -- there is no separate title to set.
    """
    out: list[str] = []
    lists = _Lists(out)
    in_code = False

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _FENCE.match(raw):
            if not in_code:
                lists.close()
            in_code = not in_code
            continue

        if in_code:
            # Inside a fence the text is literal, so it is escaped and never
            # scanned for markers.
            out.append(f"<div><tt>{html.escape(raw, quote=False) or '<br>'}</tt></div>")
            continue

        if not raw.strip():
            lists.close()
            out.append("<div><br></div>")
            continue

        heading = _HEADING.match(raw)
        if heading:
            lists.close()
            out.append(f"<div><b>{_inline(heading.group(2))}</b></div>")
            continue

        bullet = _BULLET.match(raw)
        numbered = None if bullet else _NUMBERED.match(raw)
        if bullet or numbered:
            match = bullet or numbered
            assert match is not None
            level = len(match.group(1).expandtabs(_INDENT_WIDTH)) // _INDENT_WIDTH
            body = match.group(2)
            # "- [ ] thing" loses its box; see the module docstring.
            checkbox = _CHECKBOX.match(body)
            if checkbox:
                body = checkbox.group(2)
            lists.item(level, "ul" if bullet else "ol", _inline(body))
            continue

        lists.close()
        out.append(f"<div>{_inline(raw)}</div>")

    lists.close()
    return "".join(out)


def has_checklist_syntax(text: str) -> bool:
    """Whether the Markdown asks for checkboxes this converter cannot deliver.

    The tools report this back rather than failing, so a model that wrote
    ``- [ ]`` learns the note came out with plain bullets instead of assuming
    it got what it asked for.
    """
    for raw in text.split("\n"):
        match = _BULLET.match(raw) or _NUMBERED.match(raw)
        if match and _CHECKBOX.match(match.group(2)):
            return True
    return False
