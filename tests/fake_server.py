#!/usr/bin/env python3
"""A tiny LSP server used by the end-to-end tests.

Self-contained on purpose: it must behave identically whether or not the
package is importable from the child interpreter.
"""

from __future__ import annotations

import json
import os
import sys

INITIALIZED = False


def read_exact(stream, count):
    buffer = b""
    while len(buffer) < count:
        chunk = stream.read(count - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer


def read_frame(stream):
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        chunk = stream.read(1)
        if not chunk:
            return None
        header += chunk
    length = 0
    for line in header.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    return read_exact(stream, length)


def write(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def result(request_id, value):
    write({"jsonrpc": "2.0", "id": request_id, "result": value})


def error(request_id, code, message):
    write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def main():
    global INITIALIZED

    stdin = sys.stdin.buffer
    print("fake-server: starting", file=sys.stderr, flush=True)

    while True:
        body = read_frame(stdin)
        if body is None:
            return 0

        try:
            message = json.loads(body)
        except ValueError:
            # Stay alive so the test can prove the proxy forwarded the bytes
            # verbatim instead of swallowing or re-encoding them.
            write(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error", "data": {"length": len(body)}},
                }
            )
            continue

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            if INITIALIZED:
                error(request_id, -32600, "already initialized")
                continue
            INITIALIZED = True
            result(
                request_id,
                {
                    "capabilities": {"hoverProvider": True},
                    "serverInfo": {"name": "fake-server", "pid": os.getpid()},
                },
            )
        elif method == "shutdown":
            result(request_id, None)
        elif method == "exit":
            return 0
        elif method == "textDocument/hover":
            result(request_id, {"contents": "hover from fake server"})
        elif method == "test/huge":
            result(request_id, {"payload": "x" * int(params["size"])})
        elif method == "test/echo":
            result(request_id, params)
        elif method == "test/stderr":
            print("fake-server: stderr ping", file=sys.stderr, flush=True)
            result(request_id, None)
        elif method == "test/pid":
            result(request_id, {"pid": os.getpid()})
        elif method == "test/crash":
            return 3
        elif request_id is not None:
            error(request_id, -32601, f"unknown method {method}")
        # notifications are ignored


if __name__ == "__main__":
    sys.exit(main())
