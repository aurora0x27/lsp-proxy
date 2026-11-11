"""Shared plumbing for the end-to-end tests."""

from __future__ import annotations

import contextlib
import json
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from lsp_proxy.framing import encode_frame, read_frame, write_all

FAKE_SERVER = Path(__file__).with_name("fake_server.py")


class ProxyUnderTest:
    """Run the real ``python -m lsp_proxy`` against ``fake_server.py``."""

    def __init__(self, args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lsp_proxy", *args, "--", sys.executable, str(FAKE_SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # raw fds, so select() below sees every byte
        )
        self.stderr_lines: list[str] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.decode("utf-8", "replace").rstrip())

    # -- stdio side --------------------------------------------------------

    def write(self, message: dict) -> None:
        assert self.proc.stdin is not None
        write_all(self.proc.stdin, encode_frame(json.dumps(message).encode()))

    def read(self, timeout: float = 15.0) -> dict:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout.fileno()], [], [], timeout)
        if not ready:
            raise TimeoutError(f"no frame within {timeout}s; stderr:\n{self.stderr_text()}")
        frame = read_frame(self.proc.stdout)
        assert frame is not None, "proxy closed stdout"
        return json.loads(frame[1])

    # -- process side ------------------------------------------------------

    def wait(self, timeout: float = 10.0) -> int:
        return self.proc.wait(timeout=timeout)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def stderr_text(self) -> str:
        return "\n".join(self.stderr_lines)

    def wait_for_stderr(self, needle: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in line for line in self.stderr_lines):
                return
            time.sleep(0.02)
        raise TimeoutError(f"{needle!r} never appeared in stderr:\n{self.stderr_text()}")


class Client:
    """An editor stand-in that speaks the protocol over a socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = sock.makefile("rb", buffering=0)
        self._next_id = 0

    def send(self, message: dict) -> None:
        self.sock.sendall(encode_frame(json.dumps(message).encode()))

    def request(self, method: str, params: dict | None = None) -> int:
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
        return self._next_id

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def recv(self, timeout: float = 15.0) -> dict:
        self.sock.settimeout(timeout)
        frame = read_frame(self.reader)
        assert frame is not None, "server closed the connection"
        return json.loads(frame[1])

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self.request(method, params)
        return self.recv(timeout)

    def close(self) -> None:
        for closable in (self.reader, self.sock):
            with contextlib.suppress(OSError):
                closable.close()


def wait_for_unix_socket(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"{path} never appeared")


def connect_unix(path: str | Path, timeout: float = 10.0) -> Client:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(path))
        except OSError as error:
            sock.close()
            last_error = error
            time.sleep(0.02)
            continue
        return Client(sock)
    raise TimeoutError(f"cannot connect to {path}: {last_error}")


def connect_tcp(address: str, timeout: float = 10.0) -> Client:
    host, _, port = address.partition(":")
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host, int(port)))
        except OSError as error:
            sock.close()
            last_error = error
            time.sleep(0.02)
            continue
        return Client(sock)
    raise TimeoutError(f"cannot connect to {address}: {last_error}")
