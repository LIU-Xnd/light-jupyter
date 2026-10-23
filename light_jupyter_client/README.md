# light_jupyter_client

The browser-side end of an **egress-only** reverse tunnel for
[light_jupyter_server](../light_jupyter_server).

> **Windows is the supported client platform.** It is developed and tested on
> Windows 11; it needs no Python packages beyond the standard library, and
> `python` on Windows is enough.

Assumption this package is built around: **the server has no inbound port
open — outbound only.** So the server cannot be reached by a browser, and
nothing here ever connects to it. The server dials *us*.

```
v1.0.0 and later   this client. Works when the server may only make
                   outbound connections — any firewall, any lj port.
v0.x and earlier   no client existed: the browser connected straight to the
                   server, which required the port to be reachable inbound.
```

```
browser  ->  127.0.0.1:48999          (this process)
        ->  the socket the server opened from the outside
        ->  stream  ->  light_jupyter_server on the server
```

Only `127.0.0.1` is exposed to the browser, so nothing on the network can
reach it. Use `--browser-host 0.0.0.0` if you want other machines on the LAN
to use it as well.

## Install

On Windows, from GitHub (editable — a clone lands in `src/`, so `git pull`
inside it updates the install). No dependencies beyond the standard library:

```powershell
python -m pip install -e "git+https://github.com/LIU-Xnd/light-jupyter.git#subdirectory=light_jupyter_client&egg=light_jupyter_client"
```

> The `&egg=` fragment is required: pip cannot infer a project name from a
> bare `#subdirectory` URL, and rejects the `name @ url` form outright when
> `-e` is used.

Or from a clone you already have:

```powershell
git clone https://github.com/LIU-Xnd/light-jupyter.git
cd light-jupyter\light_jupyter_client
python -m pip install -e .
# or just run it in place, no install at all
python -m light_jupyter_client
```

## Use

Run this on **both** machines — the browser's (Windows), and the server's. The
server has no inbound port, so the server dials *you*; all you do is hand it
your address once (a beacon cannot work: inbound UDP is filtered here too).

**1. On the Windows machine** (where the browser is):

```bash
python -m light_jupyter_client
```

It prints your address and one line to copy:

```
  light_jupyter_client  (egress-only tunnel)
    this machine : 192.168.77.160
    server dials : 192.168.77.160:45901
    browser at   : http://127.0.0.1:48999

  copy this line to the server terminal:

    python -m light_jupyter_server.gateway --client 192.168.77.160 45901 --lj-port <LJ-PORT>
```

**2. Move the shared secret to this machine** (one-time; the server prints a
single command on its first run). No `scp`, nothing to type by hand — copy
that one line and run it here:

```powershell
# the server prints something like this on first run:
#   echo aGVsbG8... | base64 -d > secret_light_jupyter && chmod 400 secret_light_jupyter
#
# run it on Windows in this directory:
echo aGVsbG8... | base64 -d > secret_light_jupyter
icacls secret_light_jupyter /inheritance:r /grant:r "%USERNAME%:R"
```

That writes `secret_light_jupyter` read-only. The client reads it from the
current directory, so run it from here (or pass `--secret-file`).

**3. On the server**, paste the line from step 1 and fill in `<LJ-PORT>`
with the port its banner shows (the random one is fine — the tunnel makes it
reachable):

```bash
python -m light_jupyter_server.gateway --client 192.168.77.160 45901 --lj-port 17235
```

It will say `link up -> lj 127.0.0.1:17235`.

**4. Open <http://127.0.0.1:48999/#token=...> in your browser**, using the
token from the server's banner. That is it — no port on the server had to be
opened.

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