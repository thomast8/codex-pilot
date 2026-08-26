"""Length-prefixed JSON framing for Codex Desktop's IPC socket.

Wire format, decoded from the app.asar main bundle (`src-DlBR1tzg.js`):

    [4 bytes: uint32 little-endian body length][body: UTF-8 JSON]

Writer is `g9`::

    let t = Buffer.byteLength(e, 'utf8'), n = Buffer.alloc(4 + t);
    n.writeUInt32LE(t, 0); n.write(e, 4, 'utf8');

Reader is `m9`, which destroys the socket on a frame length of 0 or one above
268435456. Newline-delimited JSON is not a valid framing here -- the reader
treats the first four bytes as a length and gives up.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from typing import Any

FRAME_HEADER_BYTES = 4
MAX_FRAME_BYTES = 268435456


class FrameError(Exception):
    """A frame violated the wire format and the stream can no longer be trusted."""


def encode_frame(message: Any) -> bytes:
    """Serialise one message to a length-prefixed frame."""
    body = json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(body)) + body


class FrameReader:
    """Incremental reader turning a byte stream into messages.

    Feed it whatever arrives from the socket; it yields each complete message.
    Frames split across reads, and several frames inside one read, both work.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> Iterator[dict[str, Any]]:
        if not chunk:
            return
        self._buf.extend(chunk)
        while True:
            if len(self._buf) < FRAME_HEADER_BYTES:
                return
            length = struct.unpack("<I", self._buf[:FRAME_HEADER_BYTES])[0]
            if length == 0 or length > MAX_FRAME_BYTES:
                raise FrameError(f"invalid frame length ({length} bytes)")
            end = FRAME_HEADER_BYTES + length
            if len(self._buf) < end:
                return
            body = bytes(self._buf[FRAME_HEADER_BYTES:end])
            del self._buf[:end]
            try:
                yield json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FrameError(f"undecodable frame body: {exc}") from exc
