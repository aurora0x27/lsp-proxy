"""Command line interface for lsp-proxy."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence

from . import __version__

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19198

# Options that consume the following token when written without ``=``.
# ``split_argv`` needs this to tell ``--trace-file foo`` from ``--trace-file``
# followed by the server command.
VALUE_OPTIONS = frozenset(
    {
        "--mode",
        "--listen",
        "--port",
        "--unix-socket",
        "--port-file",
        "--trace-file",
        "--trace-format",
        "--trace-bodies",
        "--max-body-bytes",
        "--only",
        "--dir",
        "--grace",
    }
)

EPILOG = """\
examples:
  # proxy owns the server, traffic is printed to this terminal
  lsp-proxy -- pyright-langserver --stdio

  # same, over a unix socket, with a log file for `tail -f`
  lsp-proxy --unix-socket /tmp/pyright.sock --trace-file /tmp/pyright.jsonl \\
            -- pyright-langserver --stdio

  # started by the editor as its `cmd` (stdio on both sides)
  lsp-proxy --stdio -- pyright-langserver --stdio
"""


class CommandLineError(Exception):
    """Raised for argument combinations we refuse to run."""


def split_argv(
    argv: Sequence[str],
    value_options: frozenset[str] = VALUE_OPTIONS,
) -> tuple[list[str], list[str]]:
    """Split ``argv`` into (lsp-proxy options, language server command).

    ``--`` is the explicit separator.  A bare ``-`` is accepted for backwards
    compatibility with the original script.  As a convenience the first token
    that does not look like an option also starts the command, so that
    ``lsp-proxy pyright-langserver --stdio`` keeps working.
    """
    index = 0
    while index < len(argv):
        token = argv[index]

        if token in ("--", "-"):
            return list(argv[:index]), list(argv[index + 1 :])

        if token.startswith("-"):
            name = token.split("=", 1)[0]
            takes_separate_value = "=" not in token and name in value_options
            index += 2 if takes_separate_value else 1
            continue

        return list(argv[:index]), list(argv[index:])

    return list(argv), []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lsp-proxy",
        description="Dump the JSON-RPC traffic between an editor and a language server.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    transport = parser.add_argument_group("transport")
    transport.add_argument(
        "--mode",
        choices=("socket", "pipe"),
        default="socket",
        help="socket: listen and let the editor connect; pipe: speak stdio to the editor (default: socket)",
    )
    transport.add_argument(
        "--stdio",
        dest="mode",
        action="store_const",
        const="pipe",
        help="alias for --mode pipe",
    )
    transport.add_argument(
        "--listen",
        metavar="ADDR",
        help=(
            f"listen address, HOST:PORT or unix:/path (default: {DEFAULT_HOST}:{DEFAULT_PORT} in socket mode)"
        ),
    )
    transport.add_argument("--port", type=int, metavar="N", help="TCP port to listen on")
    transport.add_argument("--unix-socket", metavar="PATH", help="unix domain socket to listen on")
    transport.add_argument(
        "--port-file", metavar="PATH", help="write the bound address to PATH once listening"
    )

    trace = parser.add_argument_group("trace output")
    trace.add_argument(
        "--trace-file",
        metavar="PATH",
        help="write the trace here; '-' means standard output (not allowed in pipe mode)",
    )
    trace.add_argument(
        "--trace-format",
        choices=("auto", "pretty", "jsonl"),
        default="auto",
        help="auto chooses pretty on a terminal and jsonl otherwise (default: auto)",
    )
    trace.add_argument(
        "--trace-bodies",
        choices=("pretty", "raw", "none"),
        default="pretty",
        help="how much of each message body to render (default: pretty)",
    )
    trace.add_argument(
        "--max-body-bytes",
        type=int,
        default=8192,
        metavar="N",
        help="truncate traced bodies longer than N bytes, 0 for no limit (default: 8192)",
    )
    trace.add_argument(
        "--trace-append", action="store_true", help="append to the trace file instead of truncating"
    )
    trace.add_argument("--only", metavar="REGEX", help="only trace messages whose method matches REGEX")
    trace.add_argument(
        "--dir",
        choices=("both", "c2s", "s2c"),
        default="both",
        help="only trace one direction: c2s (client to server) or s2c (server to client)",
    )
    trace.add_argument("--no-color", action="store_true", help="never use ANSI colors")

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument(
        "--keep-alive",
        action="store_true",
        help="answer shutdown locally and drop exit, so the server outlives the editor (socket mode only)",
    )
    runtime.add_argument("--exit-on-client", action="store_true", help="exit when the editor disconnects")
    runtime.add_argument(
        "--grace",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="how long to wait for the server to exit before terminating it (default: 2)",
    )
    runtime.add_argument("-v", "--verbose", action="store_true", help="log proxy internals to standard error")
    return parser


def _resolve_listen(args: argparse.Namespace) -> None:
    """Fill in ``args.listen`` from the three equivalent spellings."""
    explicit = [name for name in ("listen", "port", "unix_socket") if getattr(args, name) is not None]
    if len(explicit) > 1:
        raise CommandLineError("use only one of --listen, --port and --unix-socket")

    if args.listen is not None:
        return
    if args.unix_socket is not None:
        args.listen = f"unix:{args.unix_socket}"
    elif args.port is not None:
        args.listen = f"{DEFAULT_HOST}:{args.port}"
    else:
        args.listen = f"{DEFAULT_HOST}:{DEFAULT_PORT}"


def _validate(args: argparse.Namespace) -> None:
    if not args.command:
        raise CommandLineError("no language server command given (use '--' before it)")

    if args.mode == "pipe" and args.trace_file == "-":
        # In pipe mode our stdout *is* the LSP channel.  Writing trace bytes
        # into it would corrupt the protocol stream in a way that is very hard
        # to diagnose from the editor side, so refuse instead of being clever.
        raise CommandLineError("--trace-file - is not allowed in pipe mode: stdout is the LSP channel")

    if args.keep_alive and args.mode == "pipe":
        raise CommandLineError(
            "--keep-alive is only meaningful in socket mode: in pipe mode the editor owns the server process"
        )

    if args.only is not None:
        try:
            args.only_pattern = re.compile(args.only)
        except re.error as error:
            raise CommandLineError(f"--only is not a valid regular expression: {error}") from None
    else:
        args.only_pattern = None

    if args.max_body_bytes < 0:
        raise CommandLineError("--max-body-bytes must be zero or positive")

    if args.grace < 0:
        raise CommandLineError("--grace must be zero or positive")

    if args.mode == "socket":
        _resolve_listen(args)
    else:
        for name in ("listen", "port", "unix_socket", "port_file"):
            if getattr(args, name) is not None:
                raise CommandLineError(f"--{name.replace('_', '-')} is only meaningful in socket mode")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    options, command = split_argv(argv)
    args = build_parser().parse_args(options)
    args.command = command
    _validate(args)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(raw)
    except CommandLineError as error:
        print(f"lsp-proxy: {error}", file=sys.stderr)
        return 2

    from .proxy import run

    return run(args)
