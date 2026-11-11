"""Trace rendering: pairing, filtering, truncation and the two formats."""

from __future__ import annotations

import io
import json
import re

import pytest

from lsp_proxy.trace import C2S, JSONL, PRETTY, S2C, Tracer


def run_tracer(*, fmt=JSONL, bodies="none", max_body_bytes=8192, only=None, directions=None, color=False):
    """Build a tracer, drive it through a request/response pair, return output."""
    stream = io.StringIO()
    tracer = Tracer(
        stream,
        fmt=fmt,
        color=color,
        bodies=bodies,
        max_body_bytes=max_body_bytes,
        only=re.compile(only) if only else None,
        directions=frozenset(directions or {C2S, S2C}),
    )
    tracer.start()
    return tracer, stream


def body(**fields) -> bytes:
    return json.dumps(fields).encode()


def test_jsonl_records_are_one_object_per_line() -> None:
    tracer, stream = run_tracer()
    tracer.client_to_server(body(jsonrpc="2.0", id=1, method="initialize", params={}))
    tracer.server_to_client(body(jsonrpc="2.0", id=1, result={"capabilities": {}}))
    tracer.close()

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["dir"] for line in lines] == [C2S, S2C]
    assert [line["kind"] for line in lines] == ["request", "response"]
    assert lines[0]["method"] == "initialize"
    assert lines[1]["body"] == {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}


def test_response_inherits_the_request_method_and_elapsed_time() -> None:
    tracer, stream = run_tracer()
    tracer.client_to_server(body(jsonrpc="2.0", id=7, method="textDocument/hover"))
    tracer.server_to_client(body(jsonrpc="2.0", id=7, result={"contents": "hi"}))
    tracer.close()

    response = json.loads(stream.getvalue().splitlines()[1])
    assert response["method"] == "textDocument/hover"
    assert response["elapsed_ms"] >= 0


def test_notifications_are_classified_without_an_id() -> None:
    tracer, stream = run_tracer()
    tracer.server_to_client(body(jsonrpc="2.0", method="window/logMessage", params={"message": "x"}))
    tracer.close()

    record = json.loads(stream.getvalue())
    assert record["kind"] == "notification"
    assert record["method"] == "window/logMessage"
    assert "id" not in record


def test_errors_are_classified_as_errors() -> None:
    tracer, stream = run_tracer()
    tracer.client_to_server(body(jsonrpc="2.0", id=3, method="shutdown"))
    tracer.server_to_client(body(jsonrpc="2.0", id=3, error={"code": -32601, "message": "no"}))
    tracer.close()

    assert json.loads(stream.getvalue().splitlines()[1])["kind"] == "error"


def test_only_filters_by_method_but_keeps_events() -> None:
    tracer, stream = run_tracer(only="hover")
    tracer.event("editor attached")
    tracer.client_to_server(body(jsonrpc="2.0", id=1, method="textDocument/hover"))
    tracer.client_to_server(body(jsonrpc="2.0", id=2, method="textDocument/completion"))
    tracer.close()

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record.get("method") for record in records] == [None, "textDocument/hover"]
    assert records[0]["kind"] == "event"


def test_dir_filter_keeps_events_too() -> None:
    tracer, stream = run_tracer(directions={S2C})
    tracer.client_to_server(body(jsonrpc="2.0", id=1, method="initialize"))
    tracer.server_to_client(body(jsonrpc="2.0", id=1, result={}))
    tracer.event("something")
    tracer.close()

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["dir"] for record in records] == [S2C, "proxy"]


def test_body_truncation_is_explicit() -> None:
    tracer, stream = run_tracer(max_body_bytes=32)
    tracer.client_to_server(body(jsonrpc="2.0", id=1, method="initialize", params={"big": "x" * 500}))
    tracer.close()

    record = json.loads(stream.getvalue())
    assert record["truncated"] is True
    assert record["bytes"] > 500
    assert len(record["preview"]) == 32


def test_undelivered_messages_are_marked() -> None:
    tracer, stream = run_tracer()
    tracer.server_to_client(body(jsonrpc="2.0", id=1, method="window/logMessage"), delivered=False)
    tracer.close()

    assert json.loads(stream.getvalue())["delivered"] is False


def test_pretty_renders_arrows_method_and_timing() -> None:
    tracer, stream = run_tracer(fmt=PRETTY, bodies="none")
    tracer.client_to_server(body(jsonrpc="2.0", id=7, method="textDocument/hover"))
    tracer.server_to_client(body(jsonrpc="2.0", id=7, result={"contents": "hi"}))
    tracer.close()

    text = stream.getvalue()
    assert "→" in text and "←" in text
    assert "textDocument/hover" in text
    assert "ms" in text


def test_pretty_bodies_are_indented_json() -> None:
    tracer, stream = run_tracer(fmt=PRETTY, bodies="pretty")
    tracer.server_to_client(body(jsonrpc="2.0", id=1, result={"contents": "hi"}))
    tracer.close()

    text = stream.getvalue()
    assert '    "result"' in text


def test_pretty_bodies_can_be_disabled() -> None:
    tracer, stream = run_tracer(fmt=PRETTY, bodies="none")
    tracer.server_to_client(body(jsonrpc="2.0", id=1, result={"contents": "hi"}))
    tracer.close()

    assert "contents" not in stream.getvalue()


def test_color_is_only_used_when_asked() -> None:
    plain, plain_stream = run_tracer(fmt=PRETTY, bodies="none")
    plain.client_to_server(body(jsonrpc="2.0", id=1, method="initialize"))
    plain.close()

    colored, colored_stream = run_tracer(fmt=PRETTY, bodies="none", color=True)
    colored.client_to_server(body(jsonrpc="2.0", id=1, method="initialize"))
    colored.close()

    assert "\x1b[" not in plain_stream.getvalue()
    assert "\x1b[32m" in colored_stream.getvalue()


def test_server_logs_and_proxy_events_share_the_stream() -> None:
    tracer, stream = run_tracer()
    tracer.server_log("fake-server: starting")
    tracer.event("editor attached")
    tracer.close()

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["dir"] for record in records] == ["server", "proxy"]
    assert records[0]["text"] == "fake-server: starting"


@pytest.mark.parametrize("fmt", [PRETTY, JSONL])
def test_non_json_bodies_are_recorded_verbatim(fmt) -> None:
    tracer, stream = run_tracer(fmt=fmt, bodies="raw")
    tracer.client_to_server(b"not json at all")
    tracer.close()

    assert "not json at all" in stream.getvalue()
