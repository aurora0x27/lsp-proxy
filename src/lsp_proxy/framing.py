"""Frame encoding and decoding for the LSP base protocol.

Every LSP message travels as a set of ASCII headers terminated by a blank line,
followed by exactly ``Content-Length`` bytes of UTF-8 JSON::

    Content-Length: 42\\r\\n
    \\r\\n
    {"jsonrpc":"2.0",...}

The length is counted in **bytes**, not characters, so a body containing
multi-byte characters must be measured after encoding.

Everything here is a pure function over a ``BinaryIO``, which is what makes the
tricky part -- partial reads -- testable without spawning real processes.
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO

HEADER_TERMINATOR = b"\r\n\r\n"

# A header block is a handful of short ASCII lines.  Anything larger means the
# peer is not speaking LSP, and we prefer a clear error to unbounded buffering.
MAX_HEADER_BYTES = 64 * 1024

# Bodies are read in bounded blocks so a bogus Content-Length cannot make us
# allocate an enormous buffer up front.
BODY_CHUNK_BYTES = 1 << 20

# A single frame larger than this is almost certainly a corrupted header.
MAX_BODY_BYTES = 256 * 1024 * 1024


class FramingError(Exception):
    """The peer sent bytes that cannot be interpreted as an LSP frame."""


def read_exact(stream: BinaryIO, count: int) -> bytes:
    """Read exactly ``count`` bytes, looping over short reads.

    A truncated body is fatal: once the stream is out of sync there is no way
    to recover a frame boundary, so the caller must tear the connection down.
    """
    if count <= 0:
        return b""

    buffer = bytearray()
    remaining = count
    while remaining > 0:
        chunk = stream.read(min(remaining, BODY_CHUNK_BYTES))
        if not chunk:
            raise FramingError(f"stream closed with {remaining} of {count} byte(s) still expected")
        buffer += chunk
        remaining -= len(chunk)
    return bytes(buffer)


def read_header(stream: BinaryIO) -> bytes | None:
    """Read one header block, returning ``None`` at a clean end of stream."""
    header = bytearray()
    while True:
        chunk = stream.read(1)
        if not chunk:
            if not header:
                return None
            raise FramingError(f"stream closed inside a header block: {bytes(header)!r}")

        header += chunk
        if header.endswith(HEADER_TERMINATOR):
            return bytes(header)
        if len(header) > MAX_HEADER_BYTES:
            raise FramingError(f"header block exceeded {MAX_HEADER_BYTES} bytes without a terminator")


def content_length(header: bytes) -> int:
    """Extract ``Content-Length`` from a header block."""
    for line in header.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if not separator or name.strip().lower() != b"content-length":
            continue
        try:
            length = int(value.strip())
        except ValueError:
            raise FramingError(f"invalid Content-Length header: {line!r}") from None
        if length < 0:
            raise FramingError(f"negative Content-Length header: {line!r}")
        if length > MAX_BODY_BYTES:
            raise FramingError(f"Content-Length {length} exceeds {MAX_BODY_BYTES} bytes")
        return length

    raise FramingError(f"header block has no Content-Length: {header!r}")


def read_frame(stream: BinaryIO) -> tuple[bytes, bytes] | None:
    """Read one full frame as ``(header, body)``, or ``None`` at end of stream."""
    header = read_header(stream)
    if header is None:
        return None
    return header, read_exact(stream, content_length(header))


def encode_frame(body: bytes) -> bytes:
    """Wrap a JSON body in the base-protocol headers."""
    return b"Content-Length: " + str(len(body)).encode("ascii") + HEADER_TERMINATOR + body


def write_all(stream: BinaryIO, data: bytes) -> None:
    """Write a whole frame and flush it.

    Buffered writers guarantee a full write; the flush is what makes the frame
    observable on the other side instead of sitting in our process.
    """
    stream.write(data)
    stream.flush()


def decode_json(body: bytes) -> dict[str, Any] | None:
    """Best-effort JSON decoding for routing and tracing.

    Returns ``None`` when the body is not a JSON object.  A malformed body is
    still forwarded verbatim, because a proxy should not turn someone's syntax
    error into a transport failure.
    """
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None
