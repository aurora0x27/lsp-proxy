# Changelog

## 0.1.0

First release as an installable tool rather than a single script.

### Breaking changes

- **The `./lsp-proxy.py` entry point is gone.** Install with `uv tool install`
  and call `lsp-proxy`. There is no compatibility shim on purpose: two entry
  points would drift. Existing nvim configs need their `cmd` updated once.
- **`--mode` keeps its meaning, and `socket` is still the default.** Configs
  that pass `--mode pipe` or `--mode socket` continue to work. `--stdio` is a
  new alias for `--mode pipe`.
- **`--trace-file -` is rejected in pipe mode.** In pipe mode stdout is the LSP
  channel; writing trace bytes there corrupted the protocol stream. It was
  never sensible, but it used to be accepted.
- **The default trace destination moved.** It used to be `./lsp-proxy.trace.log`
  in the current directory, which for an editor-spawned proxy means dropping a
  log file into whatever project happens to be open. It is now
  `$XDG_STATE_HOME/lsp-proxy/<server>-<pid>.jsonl`, and the resolved path is
  printed on stderr.
- **Trace files default to `jsonl`, not indented JSON.** A terminal gets
  coloured, human-readable output instead; `--trace-format pretty` forces the
  old shape.

### Fixed

- Large messages were truncated on the way to and from the editor: the socket
  side read a body with a single `recv()` and wrote it with `send()`.
- Pipe mode never drained the language server's stderr, so a chatty server
  deadlocked once the OS pipe buffer filled. It is now read in both modes and
  forwarded to our stderr, which keeps it in `:LspLog`.
- The forwarding loop busy-waited on two queues, pinning a CPU core.
- Pipe mode never exited: when the editor closed stdin the proxy kept spinning
  forever.
- Mixing `select()` with buffered readers could stall on already-buffered data.

### Added

- socket mode owns the server: the editor can disconnect and re-attach, and a
  second concurrent editor is refused instead of silently cross-wiring ids.
- Unix domain sockets (`--unix-socket`), which nvim cannot reach directly and
  `socat` can.
- `--port 0` with `--port-file`, for scripts that cannot pick a free port.
- Requests and responses are paired by id, so a response line carries the
  method and the elapsed time.
- `--only` and `--dir` to keep live output readable.
- `--keep-alive`: the server survives the editor quitting.
- `--max-body-bytes`, with truncation stated explicitly instead of emitting
  broken JSON.
- The server runs in its own session and is reaped with `killpg`, so the node
  processes spawned by pyright and tsserver do not outlive the proxy.
