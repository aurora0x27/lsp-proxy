# lsp-proxy

A tiny proxy that sits between your editor and a language server and prints the
JSON-RPC traffic going both ways.

It exists because `vim.lsp.set_log_level('debug')` gives you a log file full of
nvim's own scheduling noise, and `$/setTrace` depends on the server cooperating.
A proxy is transparent to both sides: any server, any language, no cooperation
required.

No runtime dependencies, one process, one file to read if something goes wrong.

## Install

```bash
uv tool install --from git+https://github.com/aurora0x27/lsp-proxy lsp-proxy
uv tool upgrade lsp-proxy     # later
uv tool uninstall lsp-proxy
```

Try it without installing:

```bash
uvx --from git+https://github.com/aurora0x27/lsp-proxy lsp-proxy --help
```

Requires Python 3.11+ and Linux or macOS (it uses `killpg` and unix sockets).
Not published to PyPI; releases are GitHub tags with the wheel and sdist
attached.

## The three ways to use it

### 1. The proxy owns the server, the trace is your terminal

Start the proxy in one panel, attach the editor from another. Because the
proxy — not the editor — owns the server process, restarting the editor does
not restart the server.

```bash
lsp-proxy --unix-socket /tmp/pyright.sock -- pyright-langserver --stdio
```

Terminal output (colour is used only when stdout is a terminal):

```
14:03:22.418 · event       server started (pid 4210): pyright-langserver --stdio
14:03:22.419 · event       listening on unix:/tmp/pyright.sock
14:03:31.002 · event       editor attached: client
14:03:31.004 → req   #1    initialize                                 312B
14:03:31.281 ← res   #1    initialize                         277.4ms   1.9KB
14:03:33.910 → req   #2    textDocument/hover                      118B
14:03:33.912 ← res   #2    textDocument/hover                   2.1ms   189B
```

### 2. Same thing, but the log is a file and `tail -f` is the panel

```bash
lsp-proxy --unix-socket /tmp/pyright.sock \
          --trace-file /tmp/pyright.jsonl \
          -- pyright-langserver --stdio

# elsewhere
tail -f /tmp/pyright.jsonl
```

Without `--trace-file` the trace goes to
`$XDG_STATE_HOME/lsp-proxy/<server>-<pid>.jsonl` and that path is printed on
stderr.

### 3. The editor owns the proxy (pipe mode)

Useful when you do not want a second process to manage. The trace cannot go to
the terminal here — stdout is the LSP channel — so it goes to a file, and nvim
will show you the path in `:LspLog`.

```lua
vim.lsp.config('pyright', {
  cmd = { 'lsp-proxy', '--stdio', '--', 'pyright-langserver', '--stdio' },
})
```

## Attaching from Neovim

**TCP** (no extra tools):

```lua
vim.lsp.config('pyright', {
  cmd = function()
    return vim.lsp.rpc.connect('127.0.0.1', 19198)
  end,
})
```

**Unix socket** — `vim.lsp.rpc.connect` only speaks TCP, so use `socat`:

```lua
vim.lsp.config('pyright', {
  cmd = { 'socat', '-', 'UNIX-CONNECT:/tmp/pyright.sock' },
})
```

Unix sockets are worth the extra process: no port collisions, one path per
language server, and nothing else on the machine can connect by accident.

## Watching it live

The default is too much output once `initialize` and `$/progress` start flowing.
Narrow it:

```bash
# only the things you care about
lsp-proxy --only 'hover|completion|diagnostic' -- pyright-langserver --stdio

# one direction only
lsp-proxy --dir c2s -- pyright-langserver --stdio

# full bodies for a specific method, truncated elsewhere
lsp-proxy --only 'workspace/configuration' --max-body-bytes 0 -- pyright-langserver --stdio
```

`jsonl` output is one object per line, so the file is queryable:

```bash
jq 'select(.elapsed_ms > 50) | {method, elapsed_ms}' /tmp/pyright.jsonl
jq -r 'select(.kind == "request") | .method' /tmp/pyright.jsonl | sort | uniq -c
```

## Keeping the server alive across editor restarts

```bash
lsp-proxy --keep-alive --unix-socket /tmp/pyright.sock -- pyright-langserver --stdio
```

`shutdown` is answered by the proxy and `exit` is dropped, so the language
server process — with its warm index — survives the editor quitting.

**Known limitation:** LSP allows exactly one `initialize` per process, so a
reattaching editor is refused by the server (`already initialized`). This mode
keeps the *process* alive, not the *session*. Replaying `initialize` from a
cache is planned for a later release and will be opt-in.

## Where the trace goes

| Situation | Destination | Format |
| --- | --- | --- |
| `--trace-file PATH` | that file | `jsonl` unless `--trace-format` says otherwise |
| `--trace-file -` | stdout | `pretty` on a terminal, `jsonl` otherwise |
| socket mode, no flag, stdout is a terminal | stdout | `pretty`, coloured |
| socket mode, no flag, stdout is not a terminal | XDG state dir, `<server>-<pid>.jsonl` | `jsonl` |
| pipe mode, no flag | XDG state dir, `<server>-<pid>.jsonl` | `jsonl` |

`NO_COLOR=1` and `--no-color` turn colour off. `--trace-file -` is refused in
pipe mode: stdout carries LSP frames there, and mixing trace bytes into it
desynchronises the editor in a way that is very hard to diagnose.

## Options

```
--mode {socket,pipe}        socket (default) or pipe; --stdio is an alias
--listen ADDR               HOST:PORT or unix:/path
--port N                    TCP port (default 19198)
--unix-socket PATH          unix domain socket path
--port-file PATH            write the bound address to PATH (use with --port 0)
--trace-file PATH|-         trace destination, '-' means stdout
--trace-format {auto,pretty,jsonl}
--trace-bodies {pretty,raw,none}
--max-body-bytes N          truncate bodies, 0 for unlimited (default 8192)
--trace-append              append instead of truncating
--only REGEX                keep only messages whose method matches
--dir {both,c2s,s2c}        keep one direction
--no-color
--keep-alive                answer shutdown and drop exit (socket mode only)
--exit-on-client            exit when the editor disconnects
--grace SECONDS             how long to wait before terminating the server
-v, --verbose
```

Anything after `--` is the language server command. As a convenience the first
token that is not an option also starts the command, so
`lsp-proxy pyright-langserver --stdio` works too.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | clean shutdown, including `SIGINT`/`SIGTERM` |
| 2 | bad command line |
| 1 | could not start the server or the listener |
| other | the language server exited with that code |

The server is started in its own session and killed with the whole process
group, so the `node` processes that pyright and tsserver spawn do not outlive
the proxy.

## How it differs from `lsp-devtools`

`lsp-devtools` is a good project and considers the same problem. It is an agent
plus a recorder plus a Textual inspector, installed with `pipx`. This one is a
single small process with no dependencies, and it is designed around the way
you actually work in nvim: one panel running the proxy, one editor attached to
its socket. If you want a UI, use `lsp-devtools`; if you want `tail -f`, use
this.

## Development

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
```

The end-to-end tests run a real proxy against `tests/fake_server.py`, including
a 5 MB round trip that covers the short-read and short-write paths.
