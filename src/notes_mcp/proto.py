"""Just enough protobuf to read a note body.

Apple ships no ``.proto`` for this and the shape is small and stable, so a
dependency on ``protobuf`` -- with a generated module to keep in sync -- buys
nothing a hundred lines of wire-format parsing does not.

Only what the note format actually uses is implemented. Wire types 3 and 4
(the deprecated groups) are not, and 5 and 1 are skipped rather than decoded,
because no field this server reads is a fixed-width number.

The parser is deliberately forgiving about *unknown* fields and strict about
*malformed* ones: an Apple update that adds a field must not break decoding,
but a truncated blob has to raise rather than return half a note.
"""

from __future__ import annotations

# Wire types, from the protobuf encoding spec.
VARINT = 0
FIXED64 = 1
LENGTH_DELIMITED = 2
FIXED32 = 5


class ProtoError(ValueError):
    """A blob is not valid protobuf, or ended in the middle of a field."""


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint at ``pos``; return ``(value, next_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        # 10 bytes is the most a 64-bit varint can occupy. Without this a blob
        # of 0xFF bytes spins building an unbounded integer.
        if shift > 63:
            raise ProtoError("varint too long")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def fields(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Parse one message into ``(field_number, wire_type, value)`` triples.

    Repeated fields appear once per occurrence, in order, which is what the
    attribute-run list depends on. Varints come back as ints and
    length-delimited fields as bytes; fixed-width fields are skipped and
    reported with their raw bytes, since nothing here reads one.
    """
    out: list[tuple[int, int, int | bytes]] = []
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ProtoError("field number 0 is not valid")
        if wire == VARINT:
            value, pos = _varint(buf, pos)
            out.append((number, wire, value))
        elif wire == LENGTH_DELIMITED:
            length, pos = _varint(buf, pos)
            end = pos + length
            if end > len(buf):
                raise ProtoError("length-delimited field runs past the end")
            out.append((number, wire, buf[pos:end]))
            pos = end
        elif wire == FIXED64:
            if pos + 8 > len(buf):
                raise ProtoError("truncated fixed64")
            out.append((number, wire, buf[pos : pos + 8]))
            pos += 8
        elif wire == FIXED32:
            if pos + 4 > len(buf):
                raise ProtoError("truncated fixed32")
            out.append((number, wire, buf[pos : pos + 4]))
            pos += 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")
    return out


def first(buf: bytes, number: int) -> int | bytes | None:
    """The first value of field ``number``, or None if it is absent.

    Scans without materializing the whole field list, because the hot path --
    pulling the text out of every note in the archive to search it -- wants
    three nested lookups and nothing else.
    """
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        num, wire = key >> 3, key & 7
        if num == 0:
            raise ProtoError("field number 0 is not valid")
        if wire == VARINT:
            value, pos = _varint(buf, pos)
            if num == number:
                return value
        elif wire == LENGTH_DELIMITED:
            length, pos = _varint(buf, pos)
            end = pos + length
            if end > len(buf):
                raise ProtoError("length-delimited field runs past the end")
            if num == number:
                return buf[pos:end]
            pos = end
        elif wire == FIXED64:
            pos += 8
        elif wire == FIXED32:
            pos += 4
        else:
            raise ProtoError(f"unsupported wire type {wire}")
        if pos > len(buf):
            raise ProtoError("field runs past the end")
    return None
