"""Trace output.

A dedicated writer thread owns the sink so that formatting a message never
delays forwarding it.  What we write here is deliberately one line per record:
it has to stay useful under ``tail -f``.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

C2S = "c2s"
S2C = "s2c"
SERVER = "server"
PROXY = "proxy"

DIR_LABELS = {C2S: "c2s", S2C: "s2c", SERVER: "srv", PROXY: "proxy"}

# Bodies are truncated for humans and recordings alike; a single 5 MB
# didChange should not be able to make the trace unusable.
DEFAULT_MAX_BODY_BYTES = 8192


def default_trace_path(command: list[str]) -> str:
    """``$XDG_STATE_HOME/lsp-proxy/<server>-<pid>.jsonl``.

    The pid keeps concurrent proxy runs (one per language server) apart, and
    ``tail -f`` works against a path that the tool prints on startup.
    """
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    server = Path(command[0]).name if command else "lsp"
    return str(Path(state_home) / "lsp-proxy" / f"{server}-{os.getpid()}.jsonl")


def open_trace_sink(args) -> tuple[TextIO, bool]:
    """Open the trace destination, returning ``(stream, we_own_it)``."""
    if args.trace_file == "-":
        return sys.stdout, False

    path = args.trace_file or default_trace_path(args.command)
    directory = Path(path).parent
    if directory != Path(""):
        directory.mkdir(parents=True, exist_ok=True)

    mode = "a" if args.trace_append else "w"
    # Held open for the lifetime of the process on purpose.
    stream = open(path, mode, encoding="utf-8", buffering=1)  # noqa: SIM115
    print(f"lsp-proxy: tracing to {path}", file=sys.stderr, flush=True)
    return stream, True


class Tracer:
    """Serialises trace records onto one stream, off the forwarding path."""

    def __init__(
        self,
        stream: TextIO,
        *,
        owns_stream: bool = False,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self._stream = stream
        self._owns_stream = owns_stream
        self._max_body_bytes = max_body_bytes
        self._queue: queue.SimpleQueue[tuple[float, str, Any] | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="trace", daemon=False)
        self._broken = False

    def start(self) -> None:
        self._thread.start()

    def client_to_server(self, body: bytes) -> None:
        self._record(C2S, body)

    def server_to_client(self, body: bytes) -> None:
        self._record(S2C, body)

    def server_log(self, text: str) -> None:
        self._record(SERVER, text)

    def event(self, text: str) -> None:
        self._record(PROXY, text)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2)
        if self._owns_stream and not self._broken:
            with contextlib.suppress(OSError):
                self._stream.close()

    def _record(self, direction: str, payload: Any) -> None:
        if self._broken:
            return
        self._queue.put((time.time(), direction, payload))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                self._stream.write(self._format(*item) + "\n")
                self._stream.flush()
            except (BrokenPipeError, ValueError, OSError):
                # The reader went away (`| head`, or a closed terminal).  Stop
                # tracing but keep proxying: the editor still needs us.
                self._broken = True
                break

    def _format(self, timestamp: float, direction: str, payload: Any) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(timestamp))
        millis = int((timestamp % 1) * 1000)
        label = DIR_LABELS[direction]

        if isinstance(payload, bytes):
            text = payload.decode("utf-8", "replace")
            if self._max_body_bytes and len(text) > self._max_body_bytes:
                omitted = len(text) - self._max_body_bytes
                text = text[: self._max_body_bytes] + f" ... (+{omitted} bytes)"
            return f"{stamp}.{millis:03d} {label:5} {text}"

        return f"{stamp}.{millis:03d} {label:5} {payload}"


def summarise(body: bytes) -> dict[str, Any]:
    """Extract the fields worth putting on a summary line."""
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return message if isinstance(message, dict) else {}
