"""The wire-format reader.

Malformed input matters here as much as well-formed input: this parser runs
over blobs written by a closed-source app, and it must fail loudly rather than
return half a note.
"""

import pytest

from notes_mcp import proto

from .support.notewriter import field, tag, varint


def test_varints_round_trip():
    for value in (0, 1, 127, 128, 300, 2**31, 2**63 - 1):
        assert proto.fields(field(1, value)) == [(1, proto.VARINT, value)]


def test_length_delimited_fields():
    assert proto.fields(field(3, b"abc")) == [(3, proto.LENGTH_DELIMITED, b"abc")]


def test_strings_are_returned_as_bytes():
    assert proto.fields(field(2, "hé")) == [(2, proto.LENGTH_DELIMITED, "hé".encode())]


def test_repeated_fields_keep_their_order():
    blob = field(5, b"a") + field(5, b"b") + field(5, b"c")
    assert [value for _, _, value in proto.fields(blob)] == [b"a", b"b", b"c"]


def test_first_finds_a_field():
    blob = field(1, 7) + field(2, b"body") + field(3, 9)
    assert proto.first(blob, 2) == b"body"
    assert proto.first(blob, 3) == 9


def test_first_returns_none_for_an_absent_field():
    assert proto.first(field(1, 1), 99) is None


def test_first_returns_the_first_of_a_repeated_field():
    assert proto.first(field(5, b"a") + field(5, b"b"), 5) == b"a"


def test_unknown_fields_are_skipped_not_rejected():
    """An Apple update that adds a field must not break decoding."""
    blob = field(99, b"something new") + field(2, b"wanted")
    assert proto.first(blob, 2) == b"wanted"


def test_fixed_width_fields_are_skipped():
    blob = tag(4, proto.FIXED32) + b"\x00\x01\x02\x03" + field(2, b"wanted")
    assert proto.first(blob, 2) == b"wanted"
    blob = tag(4, proto.FIXED64) + b"\x00" * 8 + field(2, b"wanted")
    assert proto.first(blob, 2) == b"wanted"


def test_a_truncated_varint_raises():
    with pytest.raises(proto.ProtoError):
        proto.fields(b"\x80\x80\x80")


def test_a_varint_that_never_ends_raises_rather_than_spinning():
    with pytest.raises(proto.ProtoError):
        proto.fields(b"\xff" * 20)


def test_a_length_running_past_the_end_raises():
    with pytest.raises(proto.ProtoError):
        proto.fields(tag(1, proto.LENGTH_DELIMITED) + varint(50) + b"short")


def test_first_also_rejects_a_length_running_past_the_end():
    with pytest.raises(proto.ProtoError):
        proto.first(tag(1, proto.LENGTH_DELIMITED) + varint(50) + b"short", 9)


def test_field_number_zero_is_rejected():
    with pytest.raises(proto.ProtoError):
        proto.fields(tag(0, proto.VARINT) + varint(1))


def test_group_wire_types_are_rejected():
    """Wire types 3 and 4 are the deprecated groups; nothing here uses them."""
    with pytest.raises(proto.ProtoError):
        proto.fields(tag(1, 3))


def test_empty_input_is_an_empty_message():
    assert proto.fields(b"") == []
    assert proto.first(b"", 1) is None
