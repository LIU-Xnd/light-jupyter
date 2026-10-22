# light_jupyter

A lightweight, token-gated web IDE that runs **on a remote Linux box** and gives
you, from any browser:

- a real interactive terminal (PTY, full TTY semantics)
- an SFTP-style file browser (upload, download, mkdir, rename, delete)
- an editor pane for text files — read-only until you press **edit**

All over **one port**. No Jupyter kernel, no notebook server, no X11.

## Run

```bash
pip install tornado
python -m light_jupyter
```

That is the whole thing. It picks a free port, serves your home directory,
mints (or reuses) a token, and prints the URL. The host part of the URL is
the first non-loopback IPv4 on the machine, read from `ip addr` — nothing is
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
| `--print-only` | print the URL and exit |

### Token

Tokens are 256-bit CSPRNG values in Crockford Base32 (`secrets.choice`,
constant-time comparison), grouped for transcription:

```
lj_9YSXZWG5AEQXF-1BGZX2YSS7A70-P6696FF6Z46NG-E8V8DGFK16STC
```

The alphabet excludes `I`, `L`, `O` and `U`, so a token read off a screen
cannot be mistyped into a *different valid* token. The token is persisted to
`~/.light_jupyter/token` with mode 0600 and reused across restarts, so
bookmarks keep working — otherwise a 256-bit secret that regenerates every
boot is not a credential.

Repeated failures from one IP are throttled with exponential backoff
(default 5 attempts, 0.5 s doubling to 30 s); the response is `429` with
`Retry-After`. A successful request clears the counter.

### Default root

`--root` defaults to `/home/<user>`, **not** `os.path.expanduser("~")`. `$HOME`
is unset or wrong whenever the process starts detached (`setsid`, cron,
systemd), and some accounts keep their home outside `/home` — those fall back
to the passwd entry via `pwd.getpwnam()`.

### Port auto-selection

`--port` defaults to auto: take the first free port among the ones this
network actually admits (currently **8890**, then **9999**).

Why not scan or let the kernel assign? Measured from the client segment on
2026-10-22, the lab firewall drops inbound TCP for almost every port (1024,
3000, 5000, 7000, 8000, 8080, 8500, 8800, 9000, 10000+, 15000, 20410, 25000,
30000, 32000 and the whole ephemeral range all time out); only specific
administratively opened ports (8890, 9999) admit connections. A randomly
picked port yields a healthy server that no browser can reach. Pass `--port`
explicitly to override.

A port taken in between still reports cleanly:

```
cannot bind 0.0.0.0:20410 (Address already in use).  Another light_jupyter
may already be running; stop it or leave --port empty for auto.
```

## Self-test

```bash
python -m light_jupyter.selftest
```

Exercises the PTY session, the file API, the token gate (401 on missing /
wrong token, 200 on valid), and a full websocket command round-trip.

## Architecture

```
light_jupyter/
  __main__.py    CLI entry point
  ports.py       port parsing / free-port scanning / socket binding
  tokens.py      256-bit token minting, persistence, auth throttle
  server.py      Tornado HTTP + WebSocket, token gate
  backend.py     Session (PTY shell) and FileSystem (path-confined file ops)
  selftest.py    no-framework self test
  static/        index.html, app.css, app.js
```

Two deliberate choices worth knowing:

- **The shell goes through `pty.openpty()` + `subprocess`, read on the event
  loop via `run_in_executor`.** `pty.fork()` does not survive threads; this
  keeps Tornado responsive and makes kill/disconnect clean (`setsid` gives
  each session its own process group, so we can `killpg`).
- **Every path is confined to `--root`.** `FileSystem._safe()` rejects anything
  resolving outside the root, so `../` from the browser cannot escape.

## Why one port

Terminal, files, and editor are the same origin, so no CORS, no second port,
no separate SFTP daemon. The only requirement is that the port be reachable
from your machine.