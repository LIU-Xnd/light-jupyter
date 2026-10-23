# light_jupyter

A token-gated web IDE (terminal + SFTP-style file browser + read-only editor)
that runs on a remote Linux box and serves, over a single port, from any
browser.

```
v1.0.0 and later   server has NO inbound ports; only outbound is allowed.
                   Both halves run: the client on the browser's machine,
                   the server dials out to it. Any random lj port works.

v0.x and earlier   the browser connected straight to the server, so the
                   port had to be reachable INBOUND. Only usable where the
                   network admits it (no firewall / same open subnet).
```

## Two halves

**`light_jupyter_server/`** — the web IDE. Linux only (real PTY);
depends on tornado. Also provides the `gateway` that dials out to the client.

**`light_jupyter_client/`** — the browser-side end of the egress-only
tunnel. **Windows client** (tested on Windows 11), standard library only, so
nothing needs installing. The browser talks to
`http://127.0.0.1:<port>`; nothing is exposed to the network.

## Install

Server (Linux):

```bash
python3 -m pip install -e "git+https://github.com/LIU-Xnd/light-jupyter.git#subdirectory=light_jupyter_server&egg=light-jupyter"
```

Client (any OS, no dependencies):

```bash
python3 -m pip install -e "git+https://github.com/LIU-Xnd/light-jupyter.git#subdirectory=light_jupyter_client&egg=light-jupyter-client"
# or just run it in place
python -m light_jupyter_client
```

> `&egg=` is required on an editable install: pip cannot infer the project
> name from a bare `#subdirectory` URL. It matches `[project] name`, not the
> import name. If github.com:443 is blocked on your network, swap `https://`
> for `ssh://git@` in the URL.

## Run

On the server:

```bash
python -m light_jupyter_server  # prints its URL and token
```

On the browser machine (new in v1.0.0, needed whenever the server's port is
not reachable inbound):

```bash
python -m light_jupyter_client
```

It prints your address and the gateway command to paste on the server. Then
open `http://127.0.0.1:<browser-port>/#token=...` — put the token from the
server's banner in the fragment. Full walkthrough in
[`light_jupyter_client/README.md`](light_jupyter_client/README.md).

## What v1.0.0 changed

The browser used to connect straight to the server, which meant the port
had to be open inbound. On a network that filters inbound traffic per port —
the lab network this was built on admits 6 of 65,535 — that made an
auto-picked port unreachable no matter what the server did.

Now the direction is reversed: the server dials **out** (never filtered) to
the client on the browser's machine, and every browser connection is
multiplexed over that one outbound socket. The server needs no open port at
all, and any free port it picks becomes reachable.

Discovery by beacon is not possible in that setup — inbound UDP is filtered
too — so the client prints its address and you paste it into the gateway
command once.

## Security

- 256-bit CSPRNG tokens in Crockford Base32, compared in constant time,
  persisted 0600, with per-IP exponential backoff on failure.
- File access confined to the root; `../` cannot escape.
- The editor is read-only until you press **edit**.
- The tunnel's dial-in is authenticated with a fresh-timestamp HMAC; without
  the shared secret file nothing can attach.

## License

[MIT](LICENSE)