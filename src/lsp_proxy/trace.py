"""Trace output: what the editor and the server actually said to each other.

Design constraints that came out of real use:

* One record per line, flushed immediately, so ``tail -f`` on a file and a
  terminal panel behave the same.
* Formatting happens on a writer thread, never on the forwarding path.  A
  proxy that adds latency to ``textDocument/hover`` is a bad proxy.
* Requests and responses are paired by id, so a response line carries the
  method and how long the server took.  That is most of what makes an
  inspector readable.
* ``jsonl`` for machines, ``pretty`` for eyes, chosen by ``isatty`` unless
  the user says otherwise.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

C2S = "c2s"
S2C = "s2c"
SERVER = "server"
PROXY = "proxy"

PRETTY = "pretty"
JSONL = "jsonl"

RESET = "\x1b[0m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
CYAN = "\x1b[36m"

DEFAULT_MAX_BODY_BYTES = 8192


# ---------------------------------------------------------------------------
# Where the trace goes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceTarget:
    kind: str  # "stdout" | "file"
    path: str | None
    format: str  # PRETTY | JSONL
    color: bool
    file_mode: str = "w"


def default_trace_path(command: list[str]) -> str:
    """``$XDG_STATE_HOME/lsp-proxy/<server>-<pid>.jsonl``.

    The pid keeps concurrent runs (one per language server) apart, and the
    server name makes the file identifiable without opening it.
    """
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    server = Path(command[0]).name if command else "lsp"
    return str(Path(state_home) / "lsp-proxy" / f"{server}-{os.getpid()}.jsonl")


def _want_color(fmt: str, is_tty: bool, no_color: bool) -> bool:
    if not is_tty or no_color or fmt != PRETTY:
        return False
    return not os.environ.get("NO_COLOR")


def resolve_trace_target(args, *, mode: str, stdout_is_tty: bool) -> TraceTarget:
    """Decide sink and format from the flags and the environment.

    Kept pure so the whole matrix can be tested without opening a terminal.
    """
    explicit_format = None if args.trace_format == "auto" else args.trace_format
    file_mode = "a" if args.trace_append else "w"

    if args.trace_file == "-":
        if mode == "pipe":
            # I1: in pipe mode stdout carries LSP frames.
            raise ValueError("--trace-file - is not allowed in pipe mode: stdout is the LSP channel")
        fmt = explicit_format or (PRETTY if stdout_is_tty else JSONL)
        return TraceTarget("stdout", None, fmt, _want_color(fmt, stdout_is_tty, args.no_color), file_mode)

    if args.trace_file:
        fmt = explicit_format or JSONL
        return TraceTarget("file", args.trace_file, fmt, False, file_mode)

    if mode == "socket" and stdout_is_tty:
        fmt = explicit_format or PRETTY
        return TraceTarget("stdout", None, fmt, _want_color(fmt, True, args.no_color), file_mode)

    return TraceTarget("file", default_trace_path(args.command), explicit_format or JSONL, False, file_mode)


def open_target(target: TraceTarget) -> tuple[TextIO, bool]:
    """Open the sink, returning ``(stream, we_own_it)``."""
    if target.kind == "stdout":
        return sys.stdout, False

    assert target.path is not None
    path = Path(target.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Held open for the lifetime of the process on purpose.
    stream = open(path, target.file_mode, encoding="utf-8", buffering=1)  # noqa: SIM115
    print(f"lsp-proxy: tracing to {path}", file=sys.stderr, flush=True)
    return stream, True


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Record:
    ts: float
    direction: str
    kind: str  # request | response | notification | error | log | event
    method: str | None = None
    ident: int | str | None = None
    elapsed_ms: float | None = None
    size: int | None = None
    raw: bytes | None = None
    text: str | None = None
    parsed: Any = None
    delivered: bool = True
    seq: int = field(default=0)


def classify(message: dict[str, Any] | None, body: bytes) -> tuple[str, str | None, Any]:
    """Return ``(kind, method, parsed)`` for one protocol body."""
    if message is None:
        return "unknown", None, None

    method = message.get("method")
    has_id = "id" in message

    if method is not None and has_id:
        return "request", method, message
    if method is not None:
        return "notification", method, message
    if has_id and ("result" in message or "error" in message):
        return ("error" if "error" in message else "response"), None, message
    return "unknown", None, message


class Tracer:
    """Serialises records onto one sink from a dedicated thread."""

    def __init__(
        self,
        stream: TextIO,
        *,
        fmt: str = PRETTY,
        color: bool = False,
        owns_stream: bool = False,
        bodies: str = "pretty",
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        only: re.Pattern[str] | None = None,
        directions: frozenset[str] = frozenset({C2S, S2C}),
    ) -> None:
        self._stream = stream
        self._fmt = fmt
        self._color = color
        self._owns_stream = owns_stream
        self._bodies = bodies
        self._max_body_bytes = max_body_bytes
        self._only = only
        self._directions = directions

        self._queue: queue.SimpleQueue[Record | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="trace", daemon=False)
        self._started = False
        self._broken = False
        self._seq = 0
        self._client_pending: dict[int | str, tuple[str, float]] = {}
        self._server_pending: dict[int | str, tuple[str, float]] = {}
        self._pending_lock = threading.Lock()

        self._write = self._write_pretty if fmt == PRETTY else self._write_jsonl

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def close(self) -> None:
        self._queue.put(None)
        if self._started:
            self._thread.join(timeout=2)
        if self._owns_stream and not self._broken:
            with contextlib.suppress(OSError):
                self._stream.close()

    # -- producer side -----------------------------------------------------

    def client_to_server(self, body: bytes, *, delivered: bool = True) -> None:
        self._protocol(C2S, body, delivered)

    def server_to_client(self, body: bytes, *, delivered: bool = True) -> None:
        self._protocol(S2C, body, delivered)

    def server_log(self, text: str) -> None:
        self._submit(Record(time.time(), SERVER, "log", text=text))

    def event(self, text: str) -> None:
        self._submit(Record(time.time(), PROXY, "event", text=text))

    def _protocol(self, direction: str, body: bytes, delivered: bool) -> None:
        message = _parse(body)
        kind, method, parsed = classify(message, body)
        now = time.time()

        record = Record(
            ts=now,
            direction=direction,
            kind=kind,
            method=method,
            size=len(body),
            raw=body,
            parsed=parsed,
            delivered=delivered,
        )

        with self._pending_lock:
            if kind == "request":
                record.ident = message.get("id")
                # Server-initiated requests have their own id namespace.
                own = self._server_pending if direction == S2C else self._client_pending
                own[record.ident] = (method or "?", now)
            elif kind in ("response", "error"):
                record.ident = parsed.get("id")
                # A response travels the opposite way from its request.
                origin = self._client_pending if direction == S2C else self._server_pending
                started = origin.pop(record.ident, None)
                if started is not None:
                    record.method, began = started
                    record.elapsed_ms = (now - began) * 1000

        self._submit(record)

    def _submit(self, record: Record) -> None:
        if record.direction in (C2S, S2C) and record.direction not in self._directions:
            return
        if self._only is not None and record.method is not None and not self._only.search(record.method):
            return
        if self._broken:
            return
        self._seq += 1
        record.seq = self._seq
        self._queue.put(record)

    # -- consumer side -----------------------------------------------------

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                return
            try:
                self._stream.write(self._write(record) + "\n")
                self._stream.flush()
            except (BrokenPipeError, ValueError, OSError):
                # `head` closed the pipe, or the terminal went away.  Stop
                # tracing, keep proxying: the editor still needs us.
                self._broken = True
                return

    # -- formatting --------------------------------------------------------

    def _paint(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self._color else text

    def _stamp(self, ts: float) -> str:
        base = time.strftime("%H:%M:%S", time.localtime(ts))
        return f"{base}.{int((ts % 1) * 1000):03d}"

    def _write_pretty(self, record: Record) -> str:
        arrow = {C2S: "→", S2C: "←"}.get(record.direction, "·")
        kind = {"request": "req", "response": "res", "notification": "note", "error": "err"}.get(
            record.kind, record.kind
        )
        ident = f"#{record.ident}" if record.ident is not None else ""

        extra = []
        if record.elapsed_ms is not None:
            extra.append(f"{record.elapsed_ms:.1f}ms")
        if record.size is not None:
            extra.append(_human_bytes(record.size))
        if not record.delivered:
            extra.append("(not delivered)")

        if record.method:
            subject = record.method
        elif record.text is not None:
            subject = record.text
        else:
            subject = ""

        if record.direction == C2S:
            arrow_text = self._paint(GREEN, arrow)
        elif record.direction == S2C:
            arrow_text = self._paint(CYAN, arrow)
        else:
            arrow_text = self._paint(DIM, arrow)
        kind_text = self._paint(RED, kind) if record.kind == "error" else kind
        stamp = self._paint(DIM, self._stamp(record.ts))

        head = f"{stamp} {arrow_text} {kind_text:<5} {ident:<5} {subject}"
        if extra:
            head += "  " + " ".join(extra)

        body = self._render_body(record)
        return head.rstrip() + ("\n" + body if body else "")

    def _render_body(self, record: Record) -> str:
        if self._bodies == "none" or record.raw is None:
            return ""

        clipped, omitted = _clip(record.raw, self._max_body_bytes)
        if omitted and record.parsed is None:
            text = clipped.decode("utf-8", "replace")
            return _indent(text + f"\n... (+{omitted} bytes)")

        if self._bodies == "raw" or record.parsed is None:
            text = clipped.decode("utf-8", "replace")
            if omitted:
                text += f" ... (+{omitted} bytes)"
            return _indent(text)

        rendered = json.dumps(record.parsed, indent=4, ensure_ascii=False)
        if omitted:
            rendered = rendered[: self._max_body_bytes] + f"\n... (+{omitted} bytes)"
        return _indent(rendered)

    def _write_jsonl(self, record: Record) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.ts).astimezone().isoformat(timespec="milliseconds"),
            "seq": record.seq,
            "dir": record.direction,
            "kind": record.kind,
        }
        if record.ident is not None:
            payload["id"] = record.ident
        if record.method:
            payload["method"] = record.method
        if record.elapsed_ms is not None:
            payload["elapsed_ms"] = round(record.elapsed_ms, 3)
        if record.size is not None:
            payload["bytes"] = record.size
        if not record.delivered:
            payload["delivered"] = False

        if record.raw is not None:
            clipped, omitted = _clip(record.raw, self._max_body_bytes)
            if record.parsed is not None and not omitted:
                payload["body"] = record.parsed
            elif omitted:
                payload["truncated"] = True
                payload["preview"] = clipped.decode("utf-8", "replace")
            else:
                payload["body"] = clipped.decode("utf-8", "replace")
        elif record.text is not None:
            payload["text"] = record.text

        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(body: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None


def _clip(raw: bytes, limit: int) -> tuple[bytes, int]:
    if limit <= 0 or len(raw) <= limit:
        return raw, 0
    return raw[:limit], len(raw) - limit


def _human_bytes(count: int) -> str:
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f}KB"
    return f"{count / (1024 * 1024):.1f}MB"


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())
