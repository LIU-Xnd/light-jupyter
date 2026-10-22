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
| `--trust-proxy` | honour `X-Forwarded-For` when rate limiting |
| `--throttle-threshold` | failed token attempts before backoff (default 5) |
| `--print-only` | print the URL and exit |
| `--enjoy` | also start a **managed sshd** that lives and dies with light_jupyter, and print copy-paste setup for Windows/Linux/macOS |
| `--enjoy-port` | port to advertise for that sshd (default: auto-picked) |

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

`--port` defaults to auto: scan **1024–32767** from a random starting point
and take the first free port. Privileged and well-known ports (`22`, `25`,
`53`, `445`, …) are skipped.

A port taken in between still reports cleanly:

```
cannot bind 0.0.0.0:20410 (Address already in use).  Another light_jupyter
may already be running; stop it or leave --port empty for auto.
```

## --enjoy: sshd that lives and dies with light_jupyter

Two ways in:

  * `python -m light_jupyter --enjoy` — set up at startup, blocks printed to
    the terminal.
  * the **⚠ ssh** button in the web UI — click it and the server brings up
    the same managed sshd on the spot, then the page walks you through the
    local side: download the private key, copy the one-liner for your OS
    (auto-detected), run it, and `ssh light_jupyter` just works. The host in
    the generated config is the address your browser is actually using, so
    it is right by construction. Both paths share one sshd instance, and it
    stops when light_jupyter stops (ctrl-c / SIGTERM / atexit).

The system sshd is never touched: no `/etc/ssh/sshd_config` edits, no
reloads. The managed instance is a child process with its own port, host key
and config. Everything it creates carries a `_light_jupyter` suffix so it
cannot collide with existing files:

| file | purpose |
|---|---|
| `~/.ssh/id_light_jupyter` (+`.pub`) | the login keypair (ed25519) |
| `~/.ssh/authorized_keys_light_jupyter` | the public key; the standard `authorized_keys` is never read or written |
| `~/.light_jupyter/sshd_config_light_jupyter` | the managed sshd's own config |
| `~/.light_jupyter/id_light_jupyter_host_ed25519` | its host key |
| `~/.light_jupyter/sshd_light_jupyter.pid` / `.log` | pid and log of the instance |

Key-only logins (`PasswordAuthentication no`). A leftover instance from a
crashed run is detected via the pidfile and cleaned up on the next start.
`--enjoy-port` overrides the advertised port; by default it is auto-picked
the same way as `--port`.

### Stop enjoying

The modal's **■ stop enjoying** button undoes everything: the managed sshd
is killed (the port closes) and every server-side file it created is
deleted — idempotent, and the web token in `~/.light_jupyter/token` is
untouched. Browsers cannot delete files on your machine, so the page then
offers a local-undo one-liner (removes `~/.ssh/id_light_jupyter` and strips
the delimited config block). Set up again any time with ⚠ ssh — it mints a
fresh key, so redo the local step.

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