# light_jupyter_client

The browser-side end of an **egress-only** reverse tunnel for
[light_jupyter_server](../light_jupyter_server).

Assumption this package is built around: **the server has no inbound port
open — outbound only.** So the server cannot be reached by a browser, and
nothing here ever connects to it. The server dials *us*.

```
browser  ->  127.0.0.1:48999          (this process)
        ->  the socket the server opened from the outside
        ->  stream  ->  light_jupyter on the server
```

Only `127.0.0.1` is exposed to the browser, so nothing on the network can
reach it. Use `--browser-host 0.0.0.0` if you want other machines on the LAN
to use it as well.

## Install

No dependencies — standard library only, so it runs on Windows, macOS and
Linux alike:

```bash
python3 -m pip install -e .
# or just run it in place
python -m light_jupyter_client
```

## Use

**1. On this machine (where the browser is):**

```bash
python -m light_jupyter_client
```

It prints your address and the exact command for the server:

```
  light_jupyter client  (egress-only tunnel)
    this machine : 192.168.77.160
    server dials : 192.168.77.160:45901
    browser at   : http://127.0.0.1:48999

  ON THE SERVER, run:
    python -m light_jupyter.gateway --client 192.168.77.160 45901 --lj-port 8890
```

**2. Copy the shared secret to this machine** (one-time, the first run on
the server creates it):

```bash
scp <server>:~/.light_jupyter/secret_light_jupyter .
```

**3. Open <http://127.0.0.1:48999/#token=...> in your browser** — put the
token from the server's banner in the fragment.

## How the tunnel works

Because the server can only afford a single outbound socket, every browser
connection is multiplexed over it. Each frame is:

```
4-byte length | 1-byte type | 4-byte stream id | payload
```

| type | direction | meaning |
|---|---|---|
| `0x01 OPEN` | client → server | please open a stream |
| `0x02 DATA` | both | stream payload |
| `0x03 CLOSE` | both | stream finished |
| `0x04 ACK` | server → client | the stream is attached to lj |

The dial-in is authenticated before a single byte is forwarded:
`LJGATE` + 4-byte timestamp + 16-byte truncated HMAC-SHA256 over the
timestamp. A replayed handshake is rejected (the timestamp must be fresh),
and without the secret file no one can attach.

## License

MIT