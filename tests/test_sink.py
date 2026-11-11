"""Where the trace goes, and in what format.

This matrix is the part users notice first, so it is a pure function with no
terminal or filesystem in the way.
"""

from __future__ import annotations

import os
from argparse import Namespace

import pytest

from lsp_proxy.trace import JSONL, PRETTY, default_trace_path, resolve_trace_target


def make_args(**overrides) -> Namespace:
    defaults = {
        "trace_file": None,
        "trace_format": "auto",
        "trace_append": False,
        "no_color": False,
        "command": ["pyright-langserver", "--stdio"],
        "max_body_bytes": 8192,
        "trace_bodies": "pretty",
        "dir": "both",
        "only": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_pipe_mode_never_writes_trace_to_stdout(monkeypatch, tmp_path) -> None:
    """I1: in pipe mode our stdout carries LSP frames."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    target = resolve_trace_target(make_args(), mode="pipe", stdout_is_tty=True)

    assert target.kind == "file"
    assert target.path is not None and target.path.startswith(str(tmp_path))
    assert target.format == JSONL


def test_pipe_mode_refuses_stdout_even_if_made_explicit() -> None:
    with pytest.raises(ValueError, match="stdout is the LSP channel"):
        resolve_trace_target(make_args(trace_file="-"), mode="pipe", stdout_is_tty=True)


def test_default_file_name_carries_the_server_name_and_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    path = default_trace_path(["/usr/bin/pyright-langserver", "--stdio"])

    assert os.path.basename(path) == f"pyright-langserver-{os.getpid()}.jsonl"
    assert os.path.dirname(path) == str(tmp_path / "lsp-proxy")


def test_socket_mode_on_a_terminal_streams_pretty_output(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    target = resolve_trace_target(make_args(), mode="socket", stdout_is_tty=True)

    assert (target.kind, target.format, target.color) == ("stdout", PRETTY, True)


def test_socket_mode_without_a_terminal_records_to_a_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    target = resolve_trace_target(make_args(), mode="socket", stdout_is_tty=False)

    assert target.kind == "file"
    assert target.format == JSONL


def test_dash_means_stdout_in_socket_mode() -> None:
    target = resolve_trace_target(make_args(trace_file="-"), mode="socket", stdout_is_tty=False)

    assert target.kind == "stdout"
    assert target.format == JSONL and target.color is False


def test_explicit_file_defaults_to_jsonl_and_no_color() -> None:
    target = resolve_trace_target(make_args(trace_file="/tmp/t.log"), mode="socket", stdout_is_tty=True)

    assert (target.kind, target.path, target.format, target.color) == ("file", "/tmp/t.log", JSONL, False)


def test_append_mode_is_honoured() -> None:
    target = resolve_trace_target(
        make_args(trace_file="/tmp/t.log", trace_append=True), mode="socket", stdout_is_tty=False
    )

    assert target.file_mode == "a"


@pytest.mark.parametrize(
    ("overrides", "stdout_is_tty", "env", "expected"),
    [
        ({}, True, {}, True),
        ({"no_color": True}, True, {}, False),
        ({}, True, {"NO_COLOR": "1"}, False),
        ({"trace_format": "jsonl"}, True, {}, False),
        ({}, False, {}, False),
        ({"trace_format": "pretty"}, False, {}, False),
    ],
)
def test_color_decision(monkeypatch, overrides, stdout_is_tty, env, expected) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    target = resolve_trace_target(make_args(**overrides), mode="socket", stdout_is_tty=stdout_is_tty)

    assert target.color is expected


def test_explicit_pretty_to_a_file_is_allowed_but_never_colored() -> None:
    target = resolve_trace_target(
        make_args(trace_file="/tmp/t.log", trace_format="pretty"), mode="socket", stdout_is_tty=True
    )

    assert target.format == PRETTY
    assert target.color is False
