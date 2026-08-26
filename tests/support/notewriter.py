"""Write note bodies in the shape Notes produces.

``notedata`` only reads. The fixtures need blobs that go through the real
decode path -- gzip, then nested protobuf, then attribute runs measured in
characters -- because a hand-stubbed dict would let a decoding bug pass the
suite and fail on the first real note.

Kept deliberately small and independent of ``notes_mcp.proto``: a writer built
on the reader under test would agree with it about a mistake.
"""

from __future__ import annotations

import gzip
import zlib


def varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varints here are never negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(number: int, wire: int) -> bytes:
    return varint((number << 3) | wire)


def field(number: int, value: int | bytes | str) -> bytes:
    """One protobuf field. Ints are varints, bytes and str length-delimited."""
    if isinstance(value, int):
        return tag(number, 0) + varint(value)
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return tag(number, 2) + varint(len(payload)) + payload


def checklist(done: bool, uuid: bytes = b"\x00" * 16) -> bytes:
    """A Checklist message: a uuid and whether the box is ticked."""
    return field(1, uuid) + field(2, 1 if done else 0)


def paragraph(
    *,
    style: int | None = None,
    indent: int = 0,
    checked: bool | None = None,
) -> bytes:
    """A ParagraphStyle message."""
    out = b""
    if style is not None:
        out += field(1, style)
    if indent:
        out += field(4, indent)
    if checked is not None:
        out += field(5, checklist(checked))
    return out


def run(
    length: int,
    *,
    style: int | None = None,
    indent: int = 0,
    checked: bool | None = None,
    bold: bool = False,
    italic: bool = False,
    strikethrough: bool = False,
    underlined: bool = False,
    link: str | None = None,
    attachment: str | None = None,
) -> bytes:
    """An AttributeRun covering ``length`` characters of the note's text."""
    out = field(1, length)
    style_blob = paragraph(style=style, indent=indent, checked=checked)
    if style_blob:
        out += field(2, style_blob)
    weight = (1 if bold else 0) | (2 if italic else 0)
    if weight:
        out += field(5, weight)
    if underlined:
        out += field(6, 1)
    if strikethrough:
        out += field(7, 1)
    if link is not None:
        out += field(9, link)
    if attachment is not None:
        # AttachmentInfo carries an identifier in 1 and the type UTI in 2.
        out += field(12, field(1, "SYNTHETIC-ATTACHMENT") + field(2, attachment))
    return out


def note_body(text: str, runs: list[bytes] | None = None, *, use_zlib: bool = False) -> bytes:
    """Assemble and compress a complete note body.

    ``use_zlib`` produces the bare-zlib form that exactly one note on the
    reference archive uses, so the fallback in ``notedata.decompress`` is
    exercised rather than assumed.
    """
    note = field(2, text)
    for blob in runs or []:
        note += field(5, blob)
    document = field(1, 0) + field(2, 0) + field(3, note)
    store = field(1, 0) + field(2, document)
    if use_zlib:
        return zlib.compress(store)
    return gzip.compress(store)


def plain(text: str) -> bytes:
    """A note with one run covering all of it, which is what plain text looks like."""
    return note_body(text, [run(len(text))])
