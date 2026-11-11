"""Framing is where all the sharp edges are, so it gets the most tests."""

from __future__ import annotations

import io
import json

import pytest

from lsp_proxy.framing import (
    FramingError,
    content_length,
    decode_json,
    encode_frame,
    read_exact,
    read_frame,
)


class Trickle(io.RawIOBase):
    """A stream that hands out at most ``chunk`` bytes per read.

    Real pipes, sockets and TLS layers all return short reads.  A stream like
    this is the cheapest way to prove we loop instead of trusting ``read(n)``.
    """

    def __init__(self, data: bytes, chunk: int) -> None:
        self._data = data
        self._pos = 0
        self._chunk = chunk

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[no-untyped-def]
        size = min(len(buffer), self._chunk, len(self._data) - self._pos)
        if size <= 0:
            return 0
        buffer[:size] = self._data[self._pos : self._pos + size]
        self._pos += size
        return size


def frame(body: bytes, extra_headers: bytes = b"") -> bytes:
    return b"Content-Length: " + str(len(body)).encode() + b"\r\n" + extra_headers + b"\r\n" + body


def test_encode_frame_measures_characters_as_bytes() -> None:
    body = json.dumps({"text": "中文 🎉"}, ensure_ascii=False).encode()
    encoded = encode_frame(body)

    assert encoded.startswith(b"Content-Length: " + str(len(body)).encode())
    header, decoded = read_frame(io.BytesIO(encoded))
    assert decoded == body
    # The header must count bytes, not characters: these differ here.
    assert content_length(header) == len(body) != len(body.decode())


def test_read_frame_roundtrip() -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    header, decoded = read_frame(io.BytesIO(encode_frame(body)))

    assert header == b"Content-Length: %d\r\n\r\n" % len(body)
    assert decode_json(decoded)["method"] == "initialize"


def test_read_frame_returns_none_at_clean_eof() -> None:
    assert read_frame(io.BytesIO(b"")) is None


def test_header_split_across_reads() -> None:
    body = b'{"jsonrpc":"2.0","method":"x"}'
    stream = Trickle(frame(body), chunk=1)

    header, decoded = read_frame(stream)
    assert decoded == body
    assert header.endswith(b"\r\n\r\n")


def test_body_split_across_reads() -> None:
    payload = json.dumps({"payload": "x" * 5000}).encode()
    stream = Trickle(frame(payload), chunk=97)

    _, decoded = read_frame(stream)
    assert decoded == payload


def test_large_body_survives_many_short_reads() -> None:
    payload = b'{"payload":"' + b"x" * (5 * 1024 * 1024) + b'"}'
    stream = Trickle(frame(payload), chunk=65536)

    _, decoded = read_frame(stream)
    assert decoded == payload
    assert len(decoded) == 5 * 1024 * 1024 + len(b'{"payload":""}')


def test_multiple_frames_share_one_stream() -> None:
    stream = io.BytesIO(encode_frame(b'{"a":1}') + encode_frame(b'{"b":2}'))

    assert read_frame(stream)[1] == b'{"a":1}'
    assert read_frame(stream)[1] == b'{"b":2}'
    assert read_frame(stream) is None


def test_extra_headers_are_preserved() -> None:
    body = b'{"a":1}'
    raw = b"Content-Length: 7\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n" + body

    header, decoded = read_frame(io.BytesIO(raw))
    assert b"Content-Type" in header
    assert decoded == body and content_length(header) == 7


def test_header_is_case_insensitive() -> None:
    assert content_length(b"content-length: 12\r\n\r\n") == 12


def test_missing_content_length_is_an_error() -> None:
    with pytest.raises(FramingError, match="no Content-Length"):
        read_frame(io.BytesIO(b"Content-Type: application/json\r\n\r\n{}"))


def test_invalid_content_length_is_an_error() -> None:
    with pytest.raises(FramingError, match="invalid Content-Length"):
        read_frame(io.BytesIO(b"Content-Length: banana\r\n\r\n"))


def test_negative_content_length_is_an_error() -> None:
    with pytest.raises(FramingError, match="negative Content-Length"):
        read_frame(io.BytesIO(b"Content-Length: -1\r\n\r\n"))


def test_truncated_body_is_an_error() -> None:
    stream = io.BytesIO(b"Content-Length: 100\r\n\r\nonly-a-few-bytes")

    with pytest.raises(FramingError, match="stream closed with"):
        read_frame(stream)


def test_truncated_header_is_an_error() -> None:
    with pytest.raises(FramingError, match="inside a header block"):
        read_frame(io.BytesIO(b"Content-Length: 5\r\n"))


def test_read_exact_zero_bytes() -> None:
    assert read_exact(io.BytesIO(b"abc"), 0) == b""


def test_decode_json_rejects_non_objects() -> None:
    assert decode_json(b"[1,2,3]") is None
    assert decode_json(b"not json") is None
    assert decode_json(b'{"a":1}') == {"a": 1}
