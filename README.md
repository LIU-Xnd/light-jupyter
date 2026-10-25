# lj

A minimal, egress-only channel. One side holds a port, the other dials out
to it. No inbound port is needed on the machine that cannot be reached.

```
repo/
  pyproject.toml            <- packaging metadata; the package sits one level
  lj/                          below it, or the install silently fails
    __init__.py
    listen.py                 lj listen
    dialer.py                 lj dialer
    selftest.py               python -m lj.selftest

machine A  (no inbound port)            machine B  (holds the port)
  lj dialer  --POST /in--------------->  lj listen  :10000
      ^                                    ^
      |                          GET  /msg |  page polls: renders arrivals
      +---- GET /send -------------- POST /msg   page posts: text to send
                                          ^
                                          |
                                        browser
```

Two stdlib-only modules. No dependencies, no install step.

## Use

**On the machine with the browser** - it holds the port:

```bash
lj listen
```

```
  lj listen  (light_jupyter 2.7.1)
    listening : 0.0.0.0:10000
    reachable : 192.168.77.160:10000
    token     : lj_FE3FQK8Z...           (loaded from ~/.lj/token)

  browser : http://127.0.0.1:10000/#token=lj_FE3FQK8Z...

  hand this to the sending side:

    lj dialer --target http://192.168.77.160:10000 --token lj_FE3FQK8Z...

  ctrl-c to stop
```

Copy the `browser :` line as it stands - the token is in the fragment, so
the page authenticates itself. The fragment is never sent to the server,
so the token cannot leak into a log or a `Referer` header.

**On the machine that cannot be reached** - it dials out:

```bash
lj dialer --target 192.168.77.160:10000 --token lj_FE3FQK8Z... -m "hello"
echo "from a pipe" | lj dialer --target ... --token ...
lj dialer --target ... --token ... --repeat 10 --interval 4
```

```
  [13:50:55] 1/3 -> 204 in 0.01s
  [13:50:56] 2/3 -> 204 in 0.01s
  [13:50:57] 3/3 -> 204 in 0.00s
```

**With no `-m` and a terminal**, the dialer becomes a two-way REPL: type
to send, and what the page types appears as it arrives.

```
you > hello
  [16:19:55] --> hello
  [16:19:56] <-- message from the page
```

`/help`, `/send TEXT`, `/quit` and ctrl-d. Piped input stays a one-shot
send, so scripts never hang in a prompt.

## Versions

| | |
|---|---|
| **v2.0.0** | one-way: `lj dialer` pushes, the page renders. |
| **v2.1.0** | two-way: type in the page and `lj dialer --listen` receives it. |
| **v2.2.0** | packaged as `lj`, one command with two subcommands; `lj`, `lj -h` and `lj --help` print the top-level usage. |
| **v2.3.0** | `listen` proves the port is really its own before printing a URL, and ctrl-c shuts down and releases it. |
| **v2.4.0** | `lj dialer` is a two-way REPL: type to send, inbound messages appear live with timestamps. |
| **v2.4.1** | the self-test no longer leaves listeners behind; the REPL section survives Linux. |
| **v2.5.0** | the version number, the tag and the installed metadata are checked against each other. |
| **v2.6.0** | the version number, the tag and the installed metadata agree. |
| **v2.7.0** | https: `lj listen --cert` serves TLS, `lj dialer --target https://...` speaks it. |

## Encryption

`lj` speaks plain HTTP by default. To encrypt the traffic, give the
listener a certificate:

```bash
lj listen --gen-cert ~/.lj      # writes cert.pem and key.pem, then exits
lj listen --cert ~/.lj/cert.pem # serves https
```

The banner then prints `https://` URLs, and the other side is told to
trust that certificate:

```bash
lj dialer --target https://192.168.0.1:10000 --token lj_... \
          --ca ~/.lj/cert.pem
```

`--ca` trusts that one certificate. `--insecure` trusts any certificate
at all, which removes the point of using https -- it is for testing on a
network you control, and it says so when it runs.

The certificate is self-signed, so it has to be handed over explicitly;
there is no CA to ask. `ssl` is in the standard library, so this costs no
dependency -- only `openssl` on the machine that generates the
certificate.

## A window instead of a terminal

`lj gui` opens the listener as a window: a start button, the address to
hand to the sender, a log, and a box to type into. It is a view over the
same server -- the HTTP handling, the token and the queues are
`lj.listen`'s, imported and driven, not reimplemented. The command line
still works and nothing here is required to use `lj`.

```bash
lj gui
```

## Endpoints

One path per direction — an endpoint cannot mean both:

| method | path | who | meaning |
|---|---|---|---|
| `GET` | `/` | anyone | the page (a static shell, no data) |
| `POST` | `/in` | far side | push a message to the page |
| `GET` | `/msg` | page | take the messages that arrived |
| `POST` | `/msg` | page | send a message to the far side |
| `GET` | `/send` | far side | take what the browser typed |

All four data endpoints require the token (`X-LJ-Token` header, or the
`lj_token` cookie for the page).

## Commands

### `lj gui`

The listener in a window. No flags -- it is the same thing `lj listen`
does, with buttons.

### `lj listen`

| flag | |
|---|---|
| `--port N` | listen port (default `10000`) |
| `--host ADDR` | bind address (default `0.0.0.0` - must be reachable by the sender) |
| `--token-file PATH` | where the token lives (default `~/.lj/token`) |
| `--regen-token` | mint a new token, discarding the old one |
| `--print-only` | print the banner and exit |
| `--cert PEM` | serve https with this certificate |
| `--key PEM` | the private key for `--cert` (default: `key.pem` beside it) |
| `--gen-cert [DIR]` | generate a self-signed certificate and key (default `~/.lj`) and exit |

### `lj dialer`

| flag | |
|---|---|
| `--target [https://]IP[:PORT]` | required; defaults to port `10000` |
| `--token TOKEN` | required |
| `-m, --message TEXT` | what to send (default: stdin) |
| `-f, --file PATH` | send a file |
| `--repeat N`, `--interval SEC` | send repeatedly |
| `--timeout SEC` | per-request timeout (default `10`) |
| `--show` | read what the browser queued after sending |
| `--listen` | receive what the browser sends instead of sending |
| `--once` | with `--listen`, poll once and exit |
| `-i, --interactive` | two-way REPL (the default when stdin is a terminal and no message was given) |
| `--poll SEC` | how often the REPL checks for new messages |
| `--ca PEM` | trust this certificate (for an https target) |
| `--insecure` | accept any certificate -- not protected against anyone watching |

## The token

- 256 bits from `secrets`, rendered in a Crockford-style alphabet (no `I`,
  `L`, `O`, `U`, so a token read off a screen cannot be mistyped into a
  different valid token)
- persisted at `~/.lj/token`, mode `0600`
- compared with `hmac.compare_digest`, so a wrong token cannot be
  brute-forced by timing
- sent as the `X-LJ-Token` header; the browser passes it as the `lj_token`
  cookie because a navigation cannot set headers

Regenerate with `lj listen --regen-token`. The old URL stops
working immediately.

## Test

```bash
python -m lj.selftest
```

120 checks: token properties, target parsing, the HTTP surface, that a wrong
token is rejected on every path, that the two directions do not bleed into
each other, that the page can authenticate and send, that the dialer's CLI
handles its failure modes, that https really rejects an untrusted
certificate, and that no byte-stream/terminal surface has crept in (see
Scope). The `0600` assertion only runs where POSIX permission bits mean
something (not on Windows/NTFS).

The suite leaves nothing running: a normal exit reaps every child, and a
run that is killed leaves its pids in a file that the next run cleans up.

## Install

Standard library only, so there is nothing to resolve. Either run it from a
clone:

```bash
git clone https://github.com/LIU-Xnd/light-jupyter.git
cd light-jupyter
lj listen
```

or install it, which puts `lj` on PATH everywhere:

```bash
python -m pip install -e .          # editable: git pull updates the install
lj listen
```

`#egg=` is required on an editable install of a subdirectory URL - pip cannot
infer the project name from `#subdirectory` alone:

```bash
python -m pip install -e "git+https://github.com/LIU-Xnd/light-jupyter.git#subdirectory=lj&egg=lj"
```

If `github.com:443` is blocked on your network, `git clone
git@github.com:LIU-Xnd/light-jupyter.git` works the same.

### As an MSI

There is also an installer, with the window and a Start Menu shortcut:

```bash
pyappdist build            # -> appdist/windows/dist/lj-<version>.msi
```

It bundles a Python runtime, so it installs on a machine with nothing on
it -- no Python, no pip, no conda. The build needs
[WiX](https://wixtoolset.org) (v6 or later) and `pyappdist`; point
`PYAPPDIST_WIN_WIX` at `wix.exe` if it is not on PATH.

The MSI is unsigned, so Windows will warn about an unknown publisher. That
is the price of not having a signing certificate.

## Security

- 256-bit token compared in constant time, stored `0600`
- a wrong token is rejected on every path (`GET /msg`, `POST /msg`)
- the page itself is unauthenticated (a static shell); all data requires
  the token

## Scope: messages only, deliberately

This is a message queue with a web page on it. It is **not** a transport
for arbitrary byte streams, and there is no raw/stream mode.

Carrying a shell over it would mean re-implementing what ssh already does
correctly — PTY allocation, windowing, flow control, reconnection, and
end-to-end encryption. Worse, forwarding raw bytes would turn the token
from a message-passing credential into a remote shell credential, which is
a much larger blast radius than the one it replaces.

For a shell on a server with no inbound ports, use ssh's own mechanism: an
sshd on a reachable port on the browser's machine, another sshd on the
server bound to loopback, and `ssh -N -R <port>:localhost:<sshd>` dialled
out from the server. Real SSH, real encryption, nothing to maintain here.

`lj/selftest.py` asserts this boundary — it fails if a `/raw`, `/stream`,
`/ws`, `/shell` or `/exec` endpoint or a `--raw`/`--pty` flag ever appears.

## Limitations

- messages are queued in memory and dropped on restart; nothing is
  persisted and there is no history on reload
- one shared token, so anything holding it can both send and receive
- no delivery guarantee: a message queued while nobody is polling waits
  until someone reads it
- no shell, no file access, no editor
- the port on the listening machine must be reachable by the dialer

## License

[MIT](LICENSE)
