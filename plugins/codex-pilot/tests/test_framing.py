"""Wire framing: 4-byte little-endian length prefix + UTF-8 JSON body.

Decoded from Codex Desktop's app.asar main bundle (`src-DlBR1tzg.js`), functions
`g9` (writer) and `m9` (reader). The reader rejects a frame length of 0 or one
above 268435456, and plain newline-delimited JSON is silently dropped.
"""

import json
import struct

import pytest

from codex_pilot.framing import MAX_FRAME_BYTES, FrameError, FrameReader, encode_frame


def test_encode_frame_is_le_length_prefix_plus_utf8_json():
    frame = encode_frame({"type": "request", "method": "initialize"})
    length = struct.unpack("<I", frame[:4])[0]
    assert length == len(frame) - 4
    assert json.loads(frame[4:].decode()) == {"type": "request", "method": "initialize"}


def test_encode_frame_measures_utf8_bytes_not_characters():
    # "é" is one character but two UTF-8 bytes; a char-based length would desync the stream.
    frame = encode_frame({"t": "é"})
    assert struct.unpack("<I", frame[:4])[0] == len(frame) - 4


def test_round_trip_single_message():
    reader = FrameReader()
    msgs = list(reader.feed(encode_frame({"a": 1})))
    assert msgs == [{"a": 1}]


def test_round_trip_multiple_messages_in_one_chunk():
    reader = FrameReader()
    payload = encode_frame({"a": 1}) + encode_frame({"b": 2})
    assert list(reader.feed(payload)) == [{"a": 1}, {"b": 2}]


def test_message_split_across_chunks_is_reassembled():
    reader = FrameReader()
    frame = encode_frame({"hello": "world" * 50})
    assert list(reader.feed(frame[:7])) == []
    assert list(reader.feed(frame[7:])) == [{"hello": "world" * 50}]


def test_length_prefix_split_across_chunks():
    reader = FrameReader()
    frame = encode_frame({"a": 1})
    assert list(reader.feed(frame[:2])) == []
    assert list(reader.feed(frame[2:])) == [{"a": 1}]


def test_zero_length_frame_is_rejected():
    reader = FrameReader()
    with pytest.raises(FrameError):
        list(reader.feed(struct.pack("<I", 0)))


def test_oversized_frame_is_rejected():
    reader = FrameReader()
    with pytest.raises(FrameError):
        list(reader.feed(struct.pack("<I", MAX_FRAME_BYTES + 1)))


def test_max_frame_bytes_matches_bundle_constant():
    assert MAX_FRAME_BYTES == 268435456


def test_empty_chunk_is_a_noop():
    reader = FrameReader()
    assert list(reader.feed(b"")) == []
