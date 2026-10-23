"""lj.listen -- the listening side, on the machine that runs the browser.

The server cannot be reached directly (no inbound port), so THIS process
holds the port instead. It serves the page the browser asks for and accepts
messages pushed to it from the far side.

    python -m lj.listen                  # 0.0.0.0:10000, fresh token
    python -m lj.listen --port 10000
    python -m lj.listen --regen-token   # new token, invalidating the old URL

It prints a one-liner to hand to the sender:

    python -m lj.dialer --target <ip>:<port> --token <token>

SCOPE -- messages only, deliberately
--------------------------------------
This is a message queue with a web page on it. It is NOT a transport for
arbitrary byte streams, and it must not grow into one.

Carrying a shell over it would mean re-implementing what ssh already does
correctly: PTY allocation, windowing and flow control, reconnection
semantics, and end-to-end encryption. Worse, forwarding raw bytes hands the
holder of the token a remote command channel, so the token stops being a
message-passing credential and becomes a shell credential. That is a much
larger blast radius than the one it replaces.

Where a shell on a server with no inbound ports is needed, use ssh's own
mechanism: an sshd on a reachable port here, another sshd on the server
bound to loopback, and `ssh -N -R <port>:localhost:<sshd>` dialled out from
the server. Real SSH, real encryption, and nothing to maintain here.
"""

import argparse
import hmac
import json
import os
import queue
import secrets
import signal
import threading
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 10000
TOKEN_FILE = os.path.expanduser("~/.lj/token")

# Crockford-style alphabet: no I, L, O or U, so a token read aloud or copied
# off a screen cannot be mistyped into a different valid token.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TOKEN_BYTES = 32                      # 256 bits
TOKEN_PREFIX = "lj_"

# Two queues, opposite directions. INBOUND is what the far side pushed to
# us; OUTBOUND is what the browser wants the far side to pick up. They are
# separate on purpose: a message the browser sends must not be echoed back
# to it as if it had arrived from the server.
INBOUND = queue.Queue(maxsize=1000)
OUTBOUND = queue.Queue(maxsize=1000)


def _drain(q: queue.Queue, limit: int = 500) -> list:
    """Take everything queued, up to `limit`, without blocking."""
    out = []
    while len(out) < limit:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ token

def mint_token() -> str:
    raw = secrets.token_bytes(TOKEN_BYTES)
    n = int.from_bytes(raw, "big")
    out = []
    for _ in range(52):
        n, r = divmod(n, 32)
        out.append(ALPHABET[r])
    return TOKEN_PREFIX + "".join(out)


def load_or_create_token(path: str, regen: bool = False) -> tuple:
    """Return (token, note). Persisted so a bookmark keeps working."""
    path = os.path.abspath(os.path.expanduser(path))
    if not regen:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                t = fh.read().strip()
            if t.startswith(TOKEN_PREFIX) and len(t) > len(TOKEN_PREFIX) + 20:
                return t, f"loaded from {path}"
        except OSError:
            pass
    token = mint_token()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    # os.open()'s mode is masked by the process umask, so a 0600 request can
    # still land as 0644. Set it explicitly or the token is world-readable.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass                        # a filesystem without POSIX modes
    return token, ("regenerated into " if regen else "generated into ") + path


def verify(token: str, presented: str) -> bool:
    """Constant-time: a wrong token must not leak its length by timing."""
    return hmac.compare_digest(token.encode("utf-8"),
                               (presented or "").encode("utf-8"))


# ------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>light_jupyter</title>
<style>
  body { font: 14px/1.6 ui-monospace, Menlo, Consolas, monospace;
         margin: 2rem; color: #222; }
  h1 { font-size: 1.1rem; font-weight: 600; }
  #log { border: 1px solid #d0d0d0; border-radius: 4px; padding: 1rem;
         min-height: 60vh; max-height: 70vh; overflow: auto;
         white-space: pre-wrap; background: #fafafa; }
  #bar { color: #666; font-size: 12px; margin: .6rem 0; }
</style>
<h1>light_jupyter</h1>
<div id="bar">connecting&hellip;</div>
<div id="log"></div>
<form id="form" style="margin-top:1rem">
  <input id="text" placeholder="type here, enter to send"
         style="font:inherit;padding:.4rem;width:22rem;border:1px solid #ccc;border-radius:4px">
  <button type="submit" style="font:inherit;padding:.4rem 1rem">send</button>
</form>
<script>
const logEl = document.getElementById('log');
const bar   = document.getElementById('bar');
const form  = document.getElementById('form');
const input = document.getElementById('text');
let token = '';
let seen  = 0;

// The token rides in the fragment, which browsers never put on the wire.
// Move it into a cookie so the <form> navigation can carry it too.
function grabToken() {
  const h = location.hash || '';
  const m = /(?:^#|[?&])token=([^&]+)/.exec(h);
  if (m) {
    token = decodeURIComponent(m[1]);
    document.cookie = 'lj_token=' + token + '; path=/; SameSite=Strict';
    history.replaceState(null, '', location.pathname);   // keep it out of history
    return true;
  }
  const c = /(?:^|;)\s*lj_token=([^;]+)/.exec(document.cookie);
  if (c) { token = decodeURIComponent(c[1]); return true; }
  return false;
}

function setState(text, bad) {
  bar.textContent = text;
  bar.style.color = bad ? '#b00' : '#666';
}

function render(msgs) {
  for (const m of msgs) {
    const line = document.createElement('div');
    line.textContent = m;
    logEl.appendChild(line);
  }
  if (msgs.length) {
    logEl.scrollTop = logEl.scrollHeight;
    seen += msgs.length;
    setState(seen + ' message' + (seen === 1 ? '' : 's'), false);
  }
}

async function tick() {
  if (!token) { setState('no token -- use http://127.0.0.1:PORT/#token=...', true); return; }
  try {
    const r = await fetch('msg', {cache: 'no-store',
                                  headers: {'X-LJ-Token': token}});
    if (r.status === 401) { setState('token rejected', true); return; }
    render((await r.json()).msgs);
  } catch (e) {
    setState('waiting for the listener (' + e.message + ')', true);
  }
  setTimeout(tick, 500);
}

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const text = input.value;
  if (!text.trim()) return;
  input.value = '';
  try {
    const r = await fetch('msg', {method: 'POST',
                                  headers: {'X-LJ-Token': token,
                                            'Content-Type': 'text/plain'}});
    if (r.status === 401) { setState('token rejected', true); return; }
    setState('sent', false);
  } catch (e) {
    setState('send failed: ' + e.message, true);
  }
});

if (grabToken()) tick();
else setState('no token -- use http://127.0.0.1:PORT/#token=...', true);
</script>
"""


# --------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lj_listen/2.0"
    sys_version = ""

    token = ""                      # set by main()

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _token_ok(self) -> bool:
        got = self.headers.get("X-LJ-Token", "")
        if not got:
            # a browser navigation cannot set headers, so the page passes the
            # token as a cookie; the sender uses the header
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "lj_token":
                    got = v
        return verify(self.token, got)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/msg":
            if not self._token_ok():
                self._send(b'{"error":"bad token"}', "application/json", 401)
                return
            # what the far side pushed to us, for the page to render
            self._send(json.dumps({"msgs": _drain(INBOUND)}).encode("utf-8"),
                       "application/json")
            return
        if path == "/send":
            if not self._token_ok():
                self._send(b'{"error":"bad token"}', "application/json", 401)
                return
            # what the page typed, for the far side to collect
            self._send(json.dumps({"msgs": _drain(OUTBOUND)}).encode("utf-8"),
                       "application/json")
            return
        self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        # POST /msg is the browser sending out; POST /in is the far side
        # sending in. They cannot share an endpoint: one direction per path.
        if path == "/msg":
            target, source = OUTBOUND, "page"
        elif path == "/in":
            target, source = INBOUND, "far "
        else:
            self._send(b"not found", "text/plain", 404)
            return
        if not self._token_ok():
            self._send(b'{"error":"bad token"}', "application/json", 401)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        text = raw.decode("utf-8", "replace")
        try:
            target.put_nowait(text)
        except queue.Full:
            self._send(b'{"error":"queue full"}', "application/json", 507)
            return
        log(f"  {source} ->  {len(raw):>6} chars  {text[:60]!r}")
        self._send(b"", "text/plain", 204)

    def log_message(self, *_a) -> None:
        pass                      # log what matters, not every poll


# ------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lj listen",
        description="hold the port the far side dials, and serve the page "
                    "the browser polls")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"listen port (default: {DEFAULT_PORT})")
    p.add_argument("--host", default="0.0.0.0",
                   help="bind address; must be reachable by the far side "
                        "(default: 0.0.0.0)")
    p.add_argument("--token-file", default=TOKEN_FILE, metavar="PATH",
                   help=f"where the token is kept (default: {TOKEN_FILE})")
    p.add_argument("--regen-token", action="store_true",
                   help="mint a new token and discard the stored one")
    p.add_argument("--print-only", action="store_true",
                   help="print the banner and exit without listening")
    return p


def local_addr() -> str:
    """Our address on the path out -- any destination works, nothing is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 9))          # discard port; no packets leave
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _bind_or_explain(host: str, port: int):
    """Bind, or say clearly which port is taken."""
    from http.server import ThreadingHTTPServer
    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno in (98, 48, 10048, 13):     # EADDRINUSE, Linux/mac/Win, or denied
            raise SystemExit(
                f"cannot bind {host}:{port} -- already in use.")
        raise


def _serves_our_page(host: str, port: int, timeout: float = 3.0) -> bool:
    """True if a request to our own port comes back with our page.

    Connect to whatever address the browser would use, not to 127.0.0.1:
    a server bound to a specific non-loopback address is invisible on
    loopback, so probing loopback would wrongly report a false failure.
    """
    import urllib.request
    import urllib.error

    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{probe}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return False
    # our page has a marker another server would not
    return "lj.listen" in body or "<div id=\"log\">" in body


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    token, note = load_or_create_token(args.token_file, args.regen_token)
    Handler.token = token

    me = local_addr()
    banner = [
        "",
        "  lj.listen  (light_jupyter 2.4)",
        f"    listening : {args.host}:{args.port}",
        f"    reachable : {me}:{args.port}",
        f"    token     : {token}",
        f"                ({note})",
        "",
        # the token rides in the fragment, so this whole line is copy-pasteable:
        # open it as-is and the page authenticates itself
        f"  browser : http://127.0.0.1:{args.port}/#token={token}",
        "",
        "  hand this to the sending side:",
        "",
        f"    lj dialer --target {me}:{args.port} --token {token}",
        "",
    ]
    if args.print_only:
        log("\n".join(banner))
        return 0

    # Bind and prove the port is really ours BEFORE printing a URL the user
    # is going to trust. On Windows two sockets can bind one port and the
    # older process keeps the accept queue, so a bind that "succeeded" can
    # still leave someone else answering -- which looks exactly like a
    # broken lj. Printing first would leave a dead URL on screen after a
    # failed check, and the user would copy it anyway.
    srv = _bind_or_explain(args.host, args.port)
    srv.daemon_threads = True

    # Serving has to start BEFORE the probe: nothing answers on the port
    # until serve_forever() is running, so probing first would fail on
    # every launch. The thread is a daemon and serve_forever() below
    # replaces it, so the one started here is only for the probe window.
    _t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3},
                          daemon=True)
    _t.start()

    if not _serves_our_page(args.host, args.port):
        srv.shutdown()
        srv.server_close()
        raise SystemExit(
            f"bound {args.host}:{args.port} but something else is answering "
            f"on it -- another process holds the port.\n"
            f"  stop it, or start lj listen --port <another>")

    log("\n".join(banner))
    log("  ctrl-c to stop")
    log("")


    def _stop(*_a) -> None:
        """Ctrl-C / SIGTERM: stop accepting, release the port, leave.

        Runs inside the signal handler, so everything is done
        synchronously -- nothing here is deferred to a thread, because a
        deferred shutdown would be racing the interpreter teardown.
        """
        log("\nshutting down")
        try:
            srv.shutdown()          # returns once serve_forever() has stopped
            srv.server_close()      # release the listening socket
        except Exception:
            pass
        try:
            sys.stdout.flush()
        except Exception:
            pass
        # hard exit rather than trusting interpreter teardown: the request
        # handler threads are non-daemon on some paths and can keep the
        # process alive after 'shutting down' already printed, which reads
        # as a hang rather than an exit.
        os._exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # serve_forever() is already running on the daemon thread; the main
    # thread just waits so the signal handlers stay installed and
    # responsive.
    try:
        while _t.is_alive():
            _t.join(0.5)
    except KeyboardInterrupt:
        _stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())