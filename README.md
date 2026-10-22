# light_jupyter

A token-gated web IDE that runs on a remote Linux box and serves, over a
single port, from any browser:

- a real interactive terminal (PTY, full TTY semantics)
- an SFTP-style file browser (upload, download, mkdir, rename, delete)
- an editor pane that is read-only until you press **edit**

With `--enjoy` — or one click on the **⚠ ssh** button in the web UI — it also
runs an sshd instance whose lifetime is bound to the server's, so plain
`ssh light_jupyter` works alongside the browser session.

No Jupyter kernel, no notebook server, no X11.

## Install

From GitHub, editable (a clone lands in `src/`, so `git pull` inside it
updates the install):

```bash
python3 -m pip install -e "git+https://github.com/LIU-Xnd/light-jupyter.git#subdirectory=light_jupyter"
```

tornado is pulled in automatically. Or clone first and install from the
checkout — same result:

```bash
git clone https://github.com/LIU-Xnd/light-jupyter.git
python3 -m pip install -e ./light-jupyter/light_jupyter
```

> Linux only (real PTY + sshd); the CLI will not start on Windows/macOS.

## Run

```bash
python3 -m light_jupyter        # or just: light-jupyter
```

That is the whole thing. It picks a free port, serves your home directory,
mints (or reuses) a token, and prints the URL. The host part of the URL is the
first non-loopback IPv4 on the machine, read from `ip addr` — nothing is
hardcoded:

```
  light_jupyter
    url   : http://<first-ipv4>:<port>/#token=lj_9YSXZWG5AEQXF-...
    port  : 20410  (auto)
    root  : /home/<user>
    user  : <user>
    token : lj_9YSXZWG5AEQXF-...
```

Open that URL in a browser. The fragment (`#token=...`) never reaches the
server — the page reads it, puts it in a cookie, and hands it to the
websocket. A bad token shows an explicit error, not a blank page.

## Flags

Every flag is optional; they exist for when you need to override a default.

| flag | meaning |
|---|---|
| `--port` | listen port; empty / `auto` / `0` picks a free one |
| `--host` | bind address (default `0.0.0.0`) |
| `--root` | directory the file pane shows (default `/home/<user>`) |
| `--token` | pin the token instead of generating one |
| `--token-file` | where the token is persisted (default `~/.light_jupyter/token`) |
| `--regen-token` | discard the stored token and mint a new one |
| `--show-token` | print the token and exit |
| `--user` | shell user (default: current user) |
| `--trust-proxy` | honour `X-Forwarded-For` when rate limiting |
| `--throttle-threshold` | failed token attempts before backoff (default 5) |
| `--print-only` | print the URL and exit |

## Security

Tokens are 256-bit CSPRNG values in Crockford Base32, compared in constant
time and persisted with mode 0600:

```
lj_9YSXZWG5AEQXF-1BGZX2YSS7A70-P6696FF6Z46NG-E8V8DGFK16STC
```

The alphabet excludes `I`, `L`, `O` and `U`, so a token read off a screen
cannot be mistyped into a *different valid* token. Repeated failures from one
IP are throttled with exponential backoff (`429` + `Retry-After`).

File access is confined to the root: `FileSystem._safe()` rejects anything
resolving outside it, so `../` from the browser cannot escape.

The editor is read-only by default; a file is only writable after you press
**edit**, and `save` stays disabled until then.

## Self-test

```bash
python -m light_jupyter.selftest
```

93 checks: PTY round-trip, file API, path traversal, token gate (401 / 429 /
200), websocket command round-trip, port selection, token minting and
persistence, address discovery.

## Design notes

See [light_jupyter/README.md](light_jupyter/README.md) for the full reference:
the rule of every flag, why the shell uses `pty.openpty()` instead of
`pty.fork()`, why the terminal had to be xterm.js rather than a `<pre>` full
of escape bytes, and how `--port` auto-scans below the ephemeral range
(with a caveat for networks whose ACLs drop most inbound ports).

```
light_jupyter/
  __main__.py    CLI entry point
  ports.py       port parsing / free-port scanning / socket binding
  tokens.py      256-bit token minting, persistence, auth throttle
  server.py      Tornado HTTP + WebSocket, token gate
  backend.py     Session (PTY shell) and FileSystem (path-confined file ops)
  selftest.py    no-framework self test
  static/        index.html, app.css, app.js, vendor/xterm.js
```

## License

[MIT](LICENSE)