"""Argument handling, including the two mistakes that are easy to ship."""

from __future__ import annotations

import pytest

from lsp_proxy.cli import CommandLineError, main, parse_args, split_argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--", "pyright-langserver", "--stdio"], ([], ["pyright-langserver", "--stdio"])),
        (["pyright-langserver", "--stdio"], ([], ["pyright-langserver", "--stdio"])),
        (
            ["--trace-file", "out.jsonl", "--", "srv", "--stdio"],
            (["--trace-file", "out.jsonl"], ["srv", "--stdio"]),
        ),
        (["--trace-file=out.jsonl", "srv"], (["--trace-file=out.jsonl"], ["srv"])),
        (["-v", "srv", "--x"], (["-v"], ["srv", "--x"])),
        (["-", "srv"], ([], ["srv"])),
        (["--mode", "pipe"], (["--mode", "pipe"], [])),
    ],
)
def test_split_argv(argv: list[str], expected: tuple[list[str], list[str]]) -> None:
    assert split_argv(argv) == expected


def test_option_value_is_not_mistaken_for_the_command() -> None:
    options, command = split_argv(["--only", "hover|completion", "--", "srv"])
    assert options == ["--only", "hover|completion"]
    assert command == ["srv"]


def test_socket_mode_defaults_to_localhost() -> None:
    args = parse_args(["--", "srv"])
    assert args.mode == "socket"
    assert args.listen == "127.0.0.1:19198"
    assert args.command == ["srv"]


def test_unix_socket_spelling_normalises() -> None:
    assert parse_args(["--unix-socket", "/tmp/x.sock", "--", "srv"]).listen == "unix:/tmp/x.sock"
    assert parse_args(["--listen", "unix:/tmp/y.sock", "--", "srv"]).listen == "unix:/tmp/y.sock"
    assert parse_args(["--port", "4000", "--", "srv"]).listen == "127.0.0.1:4000"


def test_listen_spellings_are_mutually_exclusive() -> None:
    with pytest.raises(CommandLineError, match="only one of"):
        parse_args(["--port", "4000", "--unix-socket", "/tmp/x", "--", "srv"])


def test_stdio_alias_sets_pipe_mode() -> None:
    assert parse_args(["--stdio", "--", "srv"]).mode == "pipe"


def test_missing_command_is_rejected() -> None:
    with pytest.raises(CommandLineError, match="no language server command"):
        parse_args(["--mode", "pipe"])


def test_trace_to_stdout_is_refused_in_pipe_mode() -> None:
    """In pipe mode stdout is the LSP channel; mixing trace bytes into it would
    desynchronise the editor with no obvious cause."""
    with pytest.raises(CommandLineError, match="stdout is the LSP channel"):
        parse_args(["--stdio", "--trace-file", "-", "--", "srv"])


def test_trace_to_stdout_is_allowed_in_socket_mode() -> None:
    assert parse_args(["--trace-file", "-", "--", "srv"]).trace_file == "-"


def test_listen_options_are_refused_in_pipe_mode() -> None:
    with pytest.raises(CommandLineError, match="only meaningful in socket mode"):
        parse_args(["--stdio", "--port", "4000", "--", "srv"])


def test_negative_numbers_are_refused() -> None:
    with pytest.raises(CommandLineError, match="--max-body-bytes"):
        parse_args(["--max-body-bytes", "-1", "--", "srv"])
    with pytest.raises(CommandLineError, match="--grace"):
        parse_args(["--grace", "-1", "--", "srv"])


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--version"])
    assert exit_info.value.code == 0
    assert "lsp-proxy" in capsys.readouterr().out


def test_main_reports_cli_errors_without_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--stdio", "--trace-file", "-", "--", "srv"]) == 2
    assert "stdout is the LSP channel" in capsys.readouterr().err
