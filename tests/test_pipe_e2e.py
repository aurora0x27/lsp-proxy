"""End-to-end tests for pipe mode: the editor talks to us over stdio."""

from __future__ import annotations

import subprocess
import sys

from helpers import ProxyUnderTest

from lsp_proxy.framing import encode_frame


def start(tmp_path, *extra: str) -> ProxyUnderTest:
    return ProxyUnderTest(["--stdio", "--trace-file", str(tmp_path / "trace.jsonl"), *extra])


def initialize(proxy: ProxyUnderTest) -> dict:
    proxy.write({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    return proxy.read()


def test_large_message_survives_the_round_trip(tmp_path) -> None:
    """A 5 MB body exercises every short-read and short-write path at once.

    The old implementation read a body with a single ``recv(content_length)``
    and never looped, so anything above the socket buffer was silently cut in
    half.  Keep this test large enough to matter.
    """
    proxy = start(tmp_path)
    try:
        initialize(proxy)

        size = 5 * 1024 * 1024
        proxy.write({"jsonrpc": "2.0", "id": 2, "method": "test/huge", "params": {"size": size}})
        response = proxy.read()

        assert len(response["result"]["payload"]) == size
    finally:
        proxy.kill()


def test_frames_stay_ordered_and_parseable(tmp_path) -> None:
    proxy = start(tmp_path)
    try:
        initialize(proxy)

        for index in range(20):
            proxy.write({"jsonrpc": "2.0", "id": index + 2, "method": "test/echo", "params": {"n": index}})
        responses = [proxy.read() for _ in range(20)]

        assert [response["id"] for response in responses] == list(range(2, 22))
        assert [response["result"]["n"] for response in responses] == list(range(20))
    finally:
        proxy.kill()


def test_clean_shutdown_returns_zero(tmp_path) -> None:
    proxy = start(tmp_path)
    initialize(proxy)
    proxy.write({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    assert proxy.read()["result"] is None
    proxy.write({"jsonrpc": "2.0", "method": "exit"})

    assert proxy.wait() == 0


def test_editor_eof_shuts_the_proxy_down(tmp_path) -> None:
    """Closing stdin is how an editor says goodbye; the proxy must not linger."""
    proxy = start(tmp_path, "--grace", "1")
    initialize(proxy)

    assert proxy.proc.stdin is not None
    proxy.proc.stdin.close()

    assert proxy.wait() == 0


def test_server_crash_code_is_propagated(tmp_path) -> None:
    proxy = start(tmp_path, "--grace", "1")
    initialize(proxy)
    proxy.write({"jsonrpc": "2.0", "id": 2, "method": "test/crash"})

    assert proxy.wait() == 3


def test_server_stderr_reaches_our_stderr(tmp_path) -> None:
    proxy = start(tmp_path)
    try:
        initialize(proxy)
        proxy.write({"jsonrpc": "2.0", "id": 2, "method": "test/stderr"})
        proxy.read()

        proxy.wait_for_stderr("[server] fake-server: stderr ping")
    finally:
        proxy.kill()


def test_trace_file_records_both_directions(tmp_path) -> None:
    proxy = start(tmp_path)
    try:
        initialize(proxy)
        proxy.write({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
        proxy.read()

        trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        assert "c2s" in trace and "s2c" in trace
        assert "tracing to" in proxy.stderr_text()
    finally:
        proxy.kill()


def test_non_json_body_is_forwarded_untouched(tmp_path) -> None:
    """A proxy must not turn the server's parse error into a transport error."""
    proxy = start(tmp_path)
    try:
        initialize(proxy)

        garbage = b'{"jsonrpc": "2.0", "id": 2, "method": '
        assert proxy.proc.stdin is not None
        proxy.proc.stdin.write(encode_frame(garbage))
        proxy.proc.stdin.flush()

        response = proxy.read()
        assert response["error"]["code"] == -32700
        # The server saw exactly the bytes we sent, so nothing was re-encoded.
        assert response["error"]["data"]["length"] == len(garbage)
    finally:
        proxy.kill()


def test_trace_to_stdout_is_refused_in_pipe_mode() -> None:
    """I1, checked at the process boundary rather than only in argparse."""
    completed = subprocess.run(
        [sys.executable, "-m", "lsp_proxy", "--stdio", "--trace-file", "-", "--", "true"],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 2
    assert "stdout is the LSP channel" in completed.stderr
