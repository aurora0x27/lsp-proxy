"""The proxy itself: two directions, a few threads, and a process to babysit."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .framing import FramingError, decode_json, read_frame, write_all
from .trace import C2S, S2C, Tracer
from .transports import Listener, TransportError, create_listener

STDERR_PREFIX = "[server] "


def _write_fd(fd: int, data: bytes) -> None:
    """Write to a raw fd, bypassing the buffered std streams.

    Daemon threads must never hold the lock of a ``sys.stdin``/``sys.stdout``
    wrapper: if the interpreter finalises while they do, CPython aborts with
    ``_enter_buffered_busy`` instead of exiting.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]


def _stderr(text: str) -> None:
    _write_fd(2, text.encode("utf-8", "replace"))


def _raw_std_stream(fd: int, mode: str) -> BinaryIO | None:
    try:
        return os.fdopen(fd, mode, buffering=0, closefd=False)
    except OSError:
        return None


class ProxyStartupError(Exception):
    """Raised when the proxy cannot start at all."""


class StreamClient:
    """The editor, when we speak to it over our own stdin/stdout (pipe mode)."""

    def __init__(self, stream: BinaryIO, name: str = "editor") -> None:
        self.stream = stream
        self.name = name
        self.alive = True
        self._lock = threading.Lock()

    def send(self, data: bytes) -> bool:
        with self._lock:
            if not self.alive:
                return False
            try:
                write_all(self.stream, data)
                return True
            except (BrokenPipeError, OSError, ValueError):
                self.alive = False
                return False

    def close(self) -> None:
        self.alive = False


class SocketClient:
    """The editor, when it connects to our listening socket (socket mode)."""

    def __init__(self, sock: socket.socket, address: object) -> None:
        self.sock = sock
        self.reader = sock.makefile("rb")
        self.name = str(address) if address else "client"
        self.alive = True
        self._lock = threading.Lock()

    def send(self, data: bytes) -> bool:
        with self._lock:
            if not self.alive:
                return False
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                self.alive = False
                return False

    def close(self) -> None:
        self.alive = False
        # shutdown() first: closing the fd from another thread does not
        # necessarily wake a reader blocked in recv() on Linux.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        for closable in (self.reader, self.sock):
            with contextlib.suppress(OSError):
                closable.close()


class Proxy:
    def __init__(self, args, tracer: Tracer, *, echo_events: bool = True) -> None:
        self.args = args
        self.tracer = tracer
        # When the trace already goes to a terminal, echoing the same events on
        # stderr would print every line twice, out of order.
        self.echo_events = echo_events
        self.child: subprocess.Popen[bytes] | None = None
        self.listener: Listener | None = None
        self.client: StreamClient | SocketClient | None = None
        self.exit_code: int | None = None

        self.stop_event = threading.Event()
        self.editor_eof = threading.Event()
        self._client_lock = threading.Lock()
        self._child_write_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        self.tracer.start()
        try:
            self._start_child()
            self._install_signal_handlers()
            self._start_server_pumps()
            if self.args.mode == "pipe":
                self._start_pipe_client()
            else:
                self._start_listener()
        except ProxyStartupError as error:
            self._log(str(error))
            self.tracer.close()
            return 1

        self._wait()
        return self._shutdown()

    def _start_child(self) -> None:
        try:
            self.child = subprocess.Popen(
                self.args.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Own session: Ctrl-C reaches us (not the server) and we can
                # reap the server's whole process group.  pyright and tsserver
                # both spawn grandchildren that would otherwise be orphaned.
                start_new_session=True,
            )
        except OSError as error:
            raise ProxyStartupError(f"cannot start {self.args.command[0]}: {error}") from error

        self._log(f"server started (pid {self.child.pid}): {' '.join(self.args.command)}")

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: object) -> None:
            self._log(f"received {signal.Signals(signum).name}, shutting down")
            self.stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            # Not the main thread, or an unsupported signal: nothing to do.
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signum, handle)

    def _start_server_pumps(self) -> None:
        threading.Thread(target=self._pump_server_output, name="server-out", daemon=True).start()
        threading.Thread(target=self._pump_server_stderr, name="server-err", daemon=True).start()

    def _start_pipe_client(self) -> None:
        # Raw fds rather than sys.stdin.buffer / sys.stdout.buffer: see _write_fd.
        reader = _raw_std_stream(0, "rb") or sys.stdin.buffer
        writer = _raw_std_stream(1, "wb") or sys.stdout.buffer

        client = StreamClient(writer)
        with self._client_lock:
            self.client = client

        def pump() -> None:
            try:
                self._read_client_frames(reader)
            finally:
                self._log("editor closed stdin")
                self.editor_eof.set()
                self._close_child_stdin()

        threading.Thread(target=pump, name="editor-in", daemon=True).start()

    def _start_listener(self) -> None:
        try:
            self.listener = create_listener(self.args.listen)
        except (TransportError, OSError) as error:
            raise ProxyStartupError(f"cannot listen on {self.args.listen}: {error}") from error

        self._log(f"listening on {self.listener.address}")

        if self.args.port_file:
            try:
                Path(self.args.port_file).write_text(self.listener.address + "\n", encoding="utf-8")
            except OSError as error:
                raise ProxyStartupError(f"cannot write {self.args.port_file}: {error}") from error

        threading.Thread(target=self._accept_loop, name="accept", daemon=True).start()

    def _wait(self) -> None:
        deadline: float | None = None
        while not self.stop_event.is_set():
            returncode = self.child.poll()
            if returncode is not None:
                self.exit_code = returncode
                self._log(f"server exited with code {returncode}")
                return

            if self.args.mode == "pipe" and self.editor_eof.is_set() and deadline is None:
                deadline = time.monotonic() + self.args.grace

            if deadline is not None and time.monotonic() >= deadline:
                self._log("grace period expired, terminating the server")
                return

            self.stop_event.wait(0.05)

    def _shutdown(self) -> int:
        self.stop_event.set()
        self._close_child_stdin()

        if self.child is not None and self.child.poll() is None:
            self._terminate_child()

        if self.listener is not None:
            self.listener.close()
            self.listener = None

        if self.args.port_file:
            with contextlib.suppress(OSError):
                Path(self.args.port_file).unlink()

        with self._client_lock:
            client, self.client = self.client, None
        if client is not None:
            client.close()

        self.tracer.close()

        # A server we terminated ourselves is not a failure.
        return 0 if self.exit_code is None else self.exit_code

    def _terminate_child(self) -> None:
        assert self.child is not None
        pid = self.child.pid

        self._signal_group(pid, signal.SIGTERM)
        try:
            self.child.wait(timeout=self.args.grace)
            return
        except subprocess.TimeoutExpired:
            pass

        self._log("server ignored SIGTERM, sending SIGKILL")
        self._signal_group(pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.child.wait(timeout=2)

    def _signal_group(self, pid: int, signum: int) -> None:
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            try:
                if signum == signal.SIGKILL:
                    self.child.kill()  # type: ignore[union-attr]
                else:
                    self.child.terminate()  # type: ignore[union-attr]
            except OSError:
                pass

    # -- forwarding --------------------------------------------------------

    def _read_client_frames(self, reader: BinaryIO) -> None:
        while not self.stop_event.is_set():
            try:
                frame = read_frame(reader)
            except FramingError as error:
                self._log(f"framing error from editor: {error}")
                return

            if frame is None:
                return

            header, body = frame
            delivered = self._forward_to_server(header + body)
            self.tracer.client_to_server(body, delivered=delivered)

    def _pump_server_output(self) -> None:
        assert self.child is not None and self.child.stdout is not None
        stream = self.child.stdout

        while not self.stop_event.is_set():
            try:
                frame = read_frame(stream)
            except FramingError as error:
                self._log(f"framing error from server: {error}")
                break

            if frame is None:
                self._log("server closed its stdout")
                break

            header, body = frame

            client = self._current_client()
            delivered = False
            if client is not None:
                delivered = client.send(header + body)
                if not delivered:
                    self._log(f"cannot write to {client.name}, detaching")
                    self._drop_client(client)

            self.tracer.server_to_client(body, delivered=delivered)

            if client is None:
                self._note_unroutable(body)

    def _pump_server_stderr(self) -> None:
        assert self.child is not None and self.child.stderr is not None

        while True:
            line = self.child.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            self.tracer.server_log(text)
            # Forward it too: in pipe mode this is how the language server's
            # own diagnostics reach the editor's LSP log, and in socket mode it
            # shows up in the terminal running the proxy.
            if self.echo_events:
                _stderr(STDERR_PREFIX + text + "\n")

    def _forward_to_server(self, data: bytes) -> bool:
        assert self.child is not None and self.child.stdin is not None
        with self._child_write_lock:
            try:
                write_all(self.child.stdin, data)
                return True
            except (BrokenPipeError, OSError, ValueError):
                self._log("cannot write to the server, it is probably gone")
                return False

    def _close_child_stdin(self) -> None:
        if self.child is None or self.child.stdin is None:
            return
        with self._child_write_lock, contextlib.suppress(BrokenPipeError, OSError, ValueError):
            self.child.stdin.close()

    # -- clients -----------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self.listener is not None

        while not self.stop_event.is_set():
            try:
                sock, address = self.listener.accept()
            except OSError:
                return  # listener closed during shutdown

            client = SocketClient(sock, address)

            with self._client_lock:
                current = self.client
                # LSP is one editor per server.  Two clients would both start
                # numbering requests at id 1, and responses would be routed to
                # the wrong one with no visible error.
                if current is not None and current.alive:
                    self._log(f"rejecting {client.name}: an editor is already attached")
                    client.close()
                    continue
                self.client = client

            self._log(f"editor attached: {client.name}")
            threading.Thread(target=self._serve_client, args=(client,), name="editor", daemon=True).start()

    def _serve_client(self, client: SocketClient) -> None:
        try:
            self._read_client_frames(client.reader)
        finally:
            self._drop_client(client)
            self._log(f"editor detached: {client.name}")
            if self.args.exit_on_client:
                self.stop_event.set()

    def _current_client(self) -> StreamClient | SocketClient | None:
        with self._client_lock:
            client = self.client
        return client if client is not None and client.alive else None

    def _drop_client(self, client: StreamClient | SocketClient) -> None:
        with self._client_lock:
            if self.client is client:
                self.client = None
        client.close()

    def _note_unroutable(self, body: bytes) -> None:
        message = decode_json(body)
        if message is not None and "method" in message and "id" in message:
            # A server-initiated request nobody will answer: the server waits.
            self._log(
                f"dropped server request {message['method']!r}: no editor attached "
                "(the server may now be waiting for a reply)"
            )
        else:
            self._debug("dropped a server message: no editor attached")

    # -- logging -----------------------------------------------------------

    def _log(self, message: str) -> None:
        self.tracer.event(message)
        if self.echo_events:
            _stderr(f"lsp-proxy: {message}\n")

    def _debug(self, message: str) -> None:
        if self.args.verbose:
            self._log(message)


def run(args) -> int:
    from .trace import open_target, resolve_trace_target

    try:
        target = resolve_trace_target(args, mode=args.mode, stdout_is_tty=sys.stdout.isatty())
    except ValueError as error:
        _stderr(f"lsp-proxy: {error}\n")
        return 2

    stream, owns = open_target(target)
    directions = frozenset({C2S, S2C}) if args.dir == "both" else frozenset({args.dir})

    tracer = Tracer(
        stream,
        fmt=target.format,
        color=target.color,
        owns_stream=owns,
        bodies=args.trace_bodies,
        max_body_bytes=args.max_body_bytes,
        only=args.only_pattern,
        directions=directions,
    )
    return Proxy(args, tracer, echo_events=target.kind != "stdout").run()
