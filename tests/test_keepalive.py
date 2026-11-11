"""Keep-alive level 1: the server outlives the editor session.

Level 2 (replaying ``initialize`` so a reattaching editor gets a working
session) is deliberately not implemented yet; the last test here pins down
that limitation so it cannot be forgotten.
"""

from __future__ import annotations

import pytest

from helpers import ProxyUnderTest, connect_unix, wait_for_unix_socket


def start(tmp_path, *extra: str) -> tuple[ProxyUnderTest, object]:
    sock_path = tmp_path / "lsp.sock"
    proxy = ProxyUnderTest(
        [
            "--unix-socket",
            str(sock_path),
            "--keep-alive",
            "--trace-file",
            str(tmp_path / "trace.jsonl"),
            *extra,
        ]
    )
    wait_for_unix_socket(sock_path)
    return proxy, sock_path


def test_shutdown_is_answered_by_the_proxy_not_the_server(tmp_path) -> None:
    proxy, sock_path = start(tmp_path)
    try:
        client = connect_unix(sock_path)
        client.call("initialize")
        pid = client.call("test/pid")["result"]["pid"]

        assert client.call("shutdown")["result"] is None

        # Same server process, still answering: the shutdown never got there.
        assert client.call("test/pid")["result"]["pid"] == pid
        proxy.wait_for_stderr("suppressed shutdown")
        client.close()
    finally:
        proxy.stop()


def test_exit_is_dropped(tmp_path) -> None:
    proxy, sock_path = start(tmp_path)
    try:
        client = connect_unix(sock_path)
        client.call("initialize")
        pid = client.call("test/pid")["result"]["pid"]

        client.notify("exit")

        assert client.call("test/pid")["result"]["pid"] == pid
        proxy.wait_for_stderr("suppressed exit")
        client.close()
    finally:
        proxy.stop()


def test_server_survives_a_quitting_editor(tmp_path) -> None:
    proxy, sock_path = start(tmp_path)
    try:
        first = connect_unix(sock_path)
        first.call("initialize")
        pid = first.call("test/pid")["result"]["pid"]
        first.call("shutdown")
        first.notify("exit")
        first.close()
        proxy.wait_for_stderr("editor detached")

        second = connect_unix(sock_path)
        assert second.call("test/pid")["result"]["pid"] == pid
        second.close()
    finally:
        proxy.stop()


def test_reinitialize_is_rejected_this_is_the_known_limitation(tmp_path) -> None:
    """L1 keeps the process alive but not the session: LSP allows only one
    ``initialize``, so a reattaching editor is refused until L2 lands."""
    proxy, sock_path = start(tmp_path)
    try:
        first = connect_unix(sock_path)
        first.call("initialize")
        first.call("shutdown")
        first.notify("exit")
        first.close()
        proxy.wait_for_stderr("editor detached")

        second = connect_unix(sock_path)
        response = second.call("initialize")

        assert response["error"]["message"] == "already initialized"
        second.close()
    finally:
        proxy.stop()


def test_without_keep_alive_shutdown_is_forwarded(tmp_path) -> None:
    sock_path = tmp_path / "lsp.sock"
    proxy = ProxyUnderTest(["--unix-socket", str(sock_path), "--trace-file", str(tmp_path / "trace.jsonl")])
    try:
        wait_for_unix_socket(sock_path)
        client = connect_unix(sock_path)
        client.call("initialize")
        assert client.call("shutdown")["result"] is None

        # Give the log a moment; there should be nothing to suppress.
        client.call("textDocument/hover")
        assert "suppressed" not in proxy.stderr_text()
        client.close()
    finally:
        proxy.stop()


def test_keep_alive_is_refused_in_pipe_mode() -> None:
    from lsp_proxy.cli import CommandLineError, parse_args

    with pytest.raises(CommandLineError, match="only meaningful in socket mode"):
        parse_args(["--stdio", "--keep-alive", "--", "srv"])
