"""End-to-end tests for socket mode: the proxy owns the server, the editor
attaches (and can re-attach) whenever it likes."""

from __future__ import annotations

import os
import re
import socket
import time

import pytest

from helpers import ProxyUnderTest, connect_tcp, connect_unix, wait_for_unix_socket


def start_unix(tmp_path, *extra: str) -> tuple[ProxyUnderTest, object]:
    sock_path = tmp_path / "lsp.sock"
    proxy = ProxyUnderTest(
        ["--unix-socket", str(sock_path), "--trace-file", str(tmp_path / "trace.jsonl"), *extra]
    )
    wait_for_unix_socket(sock_path)
    return proxy, sock_path


def test_unix_socket_round_trip(tmp_path) -> None:
    proxy, sock_path = start_unix(tmp_path)
    try:
        client = connect_unix(sock_path)
        response = client.call("initialize")
        assert response["result"]["capabilities"]["hoverProvider"] is True

        response = client.call("textDocument/hover")
        assert response["result"]["contents"] == "hover from fake server"
        client.close()
    finally:
        proxy.stop()


def test_tcp_port_zero_reports_the_real_port(tmp_path) -> None:
    port_file = tmp_path / "port"
    proxy = ProxyUnderTest(
        ["--port", "0", "--port-file", str(port_file), "--trace-file", str(tmp_path / "trace.jsonl")]
    )
    try:
        deadline = time.monotonic() + 10
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

        client = connect_tcp(port_file.read_text(encoding="utf-8").strip())
        assert client.call("initialize")["result"]["serverInfo"]["name"] == "fake-server"
        client.close()
    finally:
        proxy.stop()


def test_editor_can_reattach_after_disconnecting(tmp_path) -> None:
    """The server outlives the editor: that is the whole point of socket mode."""
    proxy, sock_path = start_unix(tmp_path)
    try:
        first = connect_unix(sock_path)
        pid = first.call("test/pid")["result"]["pid"]
        first.close()
        proxy.wait_for_stderr("editor detached")

        second = connect_unix(sock_path)
        assert second.call("test/pid")["result"]["pid"] == pid
        second.close()
    finally:
        proxy.stop()


def test_a_second_concurrent_editor_is_rejected(tmp_path) -> None:
    """Two clients would both number requests from id 1 and responses would be
    routed to the wrong one, silently.  Refuse instead."""
    proxy, sock_path = start_unix(tmp_path)
    try:
        first = connect_unix(sock_path)
        first.call("initialize")

        second = connect_unix(sock_path)
        second.sock.settimeout(10)
        assert second.sock.recv(1) == b""

        # The first client is unaffected.
        assert first.call("textDocument/hover")["result"]["contents"]
        first.close()
        second.close()
    finally:
        proxy.stop()


def test_exit_on_client_ends_the_proxy(tmp_path) -> None:
    proxy, sock_path = start_unix(tmp_path, "--exit-on-client")
    client = connect_unix(sock_path)
    client.call("initialize")
    client.close()

    assert proxy.wait() == 0


def test_unix_socket_file_is_removed_on_shutdown(tmp_path) -> None:
    proxy, sock_path = start_unix(tmp_path)
    assert sock_path.exists()

    proxy.stop()

    deadline = time.monotonic() + 5
    while sock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not sock_path.exists()


def test_disconnecting_does_not_stop_the_proxy(tmp_path) -> None:
    proxy, sock_path = start_unix(tmp_path)
    try:
        client = connect_unix(sock_path)
        client.call("initialize")
        client.close()
        proxy.wait_for_stderr("editor detached")

        assert proxy.proc.poll() is None
    finally:
        proxy.stop()


def test_abruptly_killed_editor_does_not_take_the_server_down(tmp_path) -> None:
    proxy, sock_path = start_unix(tmp_path)
    try:
        client = connect_unix(sock_path)
        client.call("initialize")
        # No shutdown/exit exchange, just a close, like a crashed editor.
        client.close()
        proxy.wait_for_stderr("editor detached")

        replacement = connect_unix(sock_path)
        assert replacement.call("test/pid")["result"]["pid"] > 0
        replacement.close()
    finally:
        proxy.stop()


def test_framing_error_from_the_editor_drops_only_that_client(tmp_path) -> None:
    proxy, sock_path = start_unix(tmp_path)
    try:
        client = connect_unix(sock_path)
        client.call("initialize")
        client.sock.sendall(b"Content-Length: 100\r\n\r\nshort")
        # Half-close so the proxy's body read hits EOF instead of waiting.
        client.sock.shutdown(socket.SHUT_WR)
        proxy.wait_for_stderr("framing error from editor")

        replacement = connect_unix(sock_path)
        assert replacement.call("textDocument/hover")["result"]["contents"]
        replacement.close()
    finally:
        proxy.stop()


def test_listening_address_is_announced(tmp_path) -> None:
    proxy, _ = start_unix(tmp_path)
    try:
        proxy.wait_for_stderr("listening on unix:")
        assert isinstance(proxy.proc.pid, int)
    finally:
        proxy.stop()


def test_reused_unix_socket_path_is_claimed_when_stale(tmp_path) -> None:
    sock_path = tmp_path / "lsp.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()  # leaves a socket file behind, which is the stale case

    proxy = ProxyUnderTest(["--unix-socket", str(sock_path), "--trace-file", str(tmp_path / "trace.jsonl")])
    try:
        wait_for_unix_socket(sock_path)
        client = connect_unix(sock_path)
        assert client.call("initialize")["result"]["serverInfo"]["name"] == "fake-server"
        client.close()
    finally:
        proxy.stop()


def test_port_in_use_fails_cleanly_and_reaps_the_server(tmp_path) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]

    proxy = ProxyUnderTest(["--port", str(port), "--trace-file", str(tmp_path / "trace.jsonl")])
    try:
        assert proxy.wait() == 1
        assert "cannot listen" in proxy.stderr_text()

        # The server was already running when the listener failed; it must not
        # be left behind.
        pid = int(re.search(r"server started \(pid (\d+)\)", proxy.stderr_text()).group(1))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        blocker.close()
        proxy.kill()


def test_a_regular_file_is_never_mistaken_for_a_stale_socket(tmp_path) -> None:
    path = tmp_path / "not-a-socket"
    path.write_text("someone's data", encoding="utf-8")

    proxy = ProxyUnderTest(["--unix-socket", str(path), "--trace-file", str(tmp_path / "trace.jsonl")])
    try:
        assert proxy.wait() == 1
        assert "is not a socket" in proxy.stderr_text()
        assert path.read_text(encoding="utf-8") == "someone's data"
    finally:
        proxy.kill()
