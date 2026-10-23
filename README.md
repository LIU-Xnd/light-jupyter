# light_jupyter 2.0

A minimal, egress-only channel. One side holds a port, the other dials out
to it. No inbound port is needed on the machine that cannot be reached.

```
machine A  (no inbound port)            machine B  (has the port)
  lj_dialer  ---- POST --------------->  lj_listen  :10000
                                          ^
                                          | GET /            browser
                                          v GET /msg --------  polls
```

Two stdlib-only modules. No dependencies, no install step.

## Use

**On the machine with the browser** - it holds the port:

```bash
python -m lj_listen
```

```
  lj_listen  (light_jupyter 2.0)
    listening : 0.0.0.0:10000
    reachable : 192.168.77.160:10000
    token     : lj_FE3FQK8Z...           (generated into ~/.lj/token)

  browser : http://127.0.0.1:10000/

  hand this to the sending side:

    python -m lj_dialer --target 192.168.77.160:10000 --token lj_FE3FQK8Z...
```

Open `http://127.0.0.1:10000/` - the page polls and renders whatever arrives.

**On the machine that cannot be reached** - it dials out:

```bash
python -m lj_dialer --target 192.168.77.160:10000 --token lj_FE3FQK8Z... -m "hello"
echo "from a pipe" | python -m lj_dialer --target ... --token ...
python -m lj_dialer --target ... --token ... --repeat 10 --interval 4
```

```
  [13:50:55] 1/3 -> 204 in 0.01s
  [13:50:56] 2/3 -> 204 in 0.01s
  [13:50:57] 3/3 -> 204 in 0.00s
```

## Versions

| | |
|---|---|
| **v2.0.0** | one-way: `lj_dialer` -> `lj_listen`. The listener holds the port, the dialer pushes messages, the page renders them. |
| **v2.1.0** | two-way: the browser can send back too. |

v1.x was a full web IDE (terminal, file pane, editor) carried over a custom
frame-multiplexed tunnel. v2 drops that protocol for plain HTTP - measured
end to end, it was the only thing standing between the two machines, and
plain HTTP is easier to debug, proxy and extend. v1 is still in
`light_jupyter_server/` and `light_jupyter_client/` if you want the IDE.

## Commands

### `lj_listen`

| flag | |
|---|---|
| `--port N` | listen port (default `10000`) |
| `--host ADDR` | bind address (default `0.0.0.0` - must be reachable by the sender) |
| `--token-file PATH` | where the token lives (default `~/.lj/token`) |
| `--regen-token` | mint a new token, discarding the old one |
| `--print-only` | print the banner and exit |

### `lj_dialer`

| flag | |
|---|---|
| `--target IP[:PORT]` | required; defaults to port `10000` |
| `--token TOKEN` | required |
| `-m, --message TEXT` | what to send (default: stdin) |
| `-f, --file PATH` | send a file |
| `--repeat N`, `--interval SEC` | send repeatedly |
| `--show` | read the queue back after sending |

## The token

- 256 bits from `secrets`, rendered in a Crockford-style alphabet (no `I`,
  `L`, `O`, `U`, so a token read off a screen cannot be mistyped into a
  different valid token)
- persisted at `~/.lj/token`, mode `0600`
- compared with `hmac.compare_digest`, so a wrong token cannot be
  brute-forced by timing
- sent as the `X-LJ-Token` header; the browser passes it as the `lj_token`
  cookie because a navigation cannot set headers

Regenerate with `python -m lj_listen --regen-token`. The old URL stops
working immediately.

## Test

```bash
python selftest_v2.py
```

34 checks: token properties, target parsing, the HTTP surface, that a wrong
token is rejected on every path, and the dialer's CLI including its failure
modes. The `0600` assertion only runs where POSIX permission bits mean
something (not on Windows/NTFS).

## Install

None needed - both files are stdlib only:

```bash
git clone https://github.com/LIU-Xnd/light-jupyter.git
cd light-jupyter
python -m lj_listen
```

If `github.com:443` is blocked on your network, `git clone
git@github.com:LIU-Xnd/light-jupyter.git` works the same.

## Security

- 256-bit token compared in constant time, stored `0600`
- a wrong token is rejected on every path (`GET /msg`, `POST /msg`)
- the page itself is unauthenticated (a static shell); all data requires
  the token

## Limitations

This is the transport, not the IDE yet:

- one-way until v2.1.0 (the browser cannot send back)
- messages are queued in memory and dropped on restart
- no shell, no file access, no editor yet
- the port on the listening machine must be reachable by the dialer

## License

[MIT](LICENSE)
