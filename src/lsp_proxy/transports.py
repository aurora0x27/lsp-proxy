"""Socket listeners for socket mode."""

from __future__ import annotations

import contextlib
import os
import socket
import stat
from dataclasses import dataclass

AF_UNIX_AVAILABLE = hasattr(socket, "AF_UNIX")


class TransportError(Exception):
    """Raised when the requested listen address cannot be served."""


@dataclass
class Listener:
    sock: socket.socket
    address: str
    unix_path: str | None = None

    def accept(self) -> tuple[socket.socket, object]:
        return self.sock.accept()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()
        if self.unix_path:
            with contextlib.suppress(OSError):
                os.unlink(self.unix_path)


def parse_listen(spec: str) -> tuple[str, str, int]:
    """Parse ``HOST:PORT`` or ``unix:/path`` into ``(kind, host_or_path, port)``."""
    if spec.startswith("unix:"):
        path = spec[len("unix:") :]
        if not path:
            raise TransportError("unix listen address needs a path, e.g. unix:/tmp/lsp.sock")
        if not AF_UNIX_AVAILABLE:
            raise TransportError("unix sockets are not available on this platform")
        return "unix", path, 0

    host, separator, port_text = spec.rpartition(":")
    if not separator:
        raise TransportError(f"listen address must be HOST:PORT or unix:/path, got {spec!r}")
    try:
        port = int(port_text)
    except ValueError:
        raise TransportError(f"invalid port in listen address {spec!r}") from None
    if not 0 <= port <= 65535:
        raise TransportError(f"port out of range in listen address {spec!r}")
    return "tcp", host or "127.0.0.1", port


def _claim_unix_path(path: str) -> None:
    """Remove a stale socket file, but never steal a live one."""
    if not os.path.exists(path):
        return

    if not stat.S_ISSOCK(os.stat(path).st_mode):
        # A regular file at this path is someone's data, not our leftover.
        raise TransportError(f"{path} exists and is not a socket")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except OSError:
        os.unlink(path)
    else:
        raise TransportError(f"{path} is already being served by another process")
    finally:
        probe.close()


def create_listener(spec: str) -> Listener:
    kind, host_or_path, port = parse_listen(spec)

    if kind == "unix":
        _claim_unix_path(host_or_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(host_or_path)
        sock.listen(1)
        # Report the path, not the address: an abstract unix socket has none.
        return Listener(sock, f"unix:{host_or_path}", unix_path=host_or_path)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host_or_path, port))
    except OSError as error:
        sock.close()
        raise TransportError(f"cannot bind {host_or_path}:{port}: {error}") from error
    sock.listen(1)

    bound_host, bound_port = sock.getsockname()[:2]
    return Listener(sock, f"{bound_host}:{bound_port}")
