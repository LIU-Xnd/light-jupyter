"""lj.listen -- the listening side, on the machine that runs the browser.

The server cannot be reached directly (no inbound port), so THIS process
holds the port instead. It serves the page the browser asks for and accepts
messages pushed to it from the far side.

    python -m lj.listen                  # 0.0.0.0:10000, fresh token
    python -m lj.listen --port 10000
    python -m lj.listen --regen-token   # new token, invalidating the old URL

It prints a one-liner to hand to the sender:

    python -m lj.dialer --target <ip>:<port> --token <token>

SCOPE -- a file manager and a command runner
---------------------------------------------
The far side can be asked to list a directory, read a file, write one,
move it, copy it, delete it, and to run a command and return its output.

WHAT THIS IS NOT
----------------
There is no PTY and no byte stream. A command runs to completion and
its output comes back whole. That is deliberate: a real interactive
terminal needs a PTY, a resize channel, a signal channel and flow
control -- and a browser that emulates one properly is xterm.js, which
is a large dependency to carry for a tool whose point is being small.
Every operation here is a request that comes back whole, so a timeout
costs one operation rather than a session that has to be detected and
rebuilt.

THE TOKEN IS NOW A SHELL CREDENTIAL
-----------------------------------
This is the part that matters most. Before, holding the token let you
pass messages. Now it lets you read, write and delete files on the far
side and run commands there. The token is still 256 bits of CSPRNG
compared in constant time, and it is still the only thing standing
between a stranger and that machine -- but it is a PASSWORD for the far
side now, not a messaging credential.

So use a network you control. And keep the far side's account to what
you would be willing to lose.
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
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .page import PAGE
from .wire import WIRE as W
from .wire import decode as _decode

DEFAULT_PORT = 10000
TOKEN_FILE = os.path.expanduser("~/.lj/token")

# The largest request body the listener will read. A command that
# writes a file needs room for its text; anything bigger than this is
# not something the browser sends.
MAX_BODY = 8 * 1024 * 1024

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

# --------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lj_listen/2.0"
    sys_version = ""

    token = ""                      # set by main()

    def _read_body(self) -> bytes:
        """The request body, read exactly once.

        The length is capped: an unbounded read is a way to make the
        listener allocate whatever the sender says it is sending.
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        n = min(n, MAX_BODY)
        return self.rfile.read(n)

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
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
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
        if path == "/cmd":
            # the far side collecting the work the browser asked for
            if not self._token_ok():
                self._send(b'{"error":"bad token"}', "application/json", 401)
                return
            self._send(json.dumps({"cmds": W.take_commands()}).encode("utf-8"),
                       "application/json")
            return
        if path == "/res":
            # the browser collecting what the far side answered
            if not self._token_ok():
                self._send(b'{"error":"bad token"}', "application/json", 401)
                return
            after = 0
            for part in query.split("&"):
                k, _, v = part.partition("=")
                if k == "after":
                    try:
                        after = int(v)
                    except ValueError:
                        after = 0
            self._send(json.dumps({"results": W.take_results(after)}).encode("utf-8"),
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
        elif path == "/cmd":
            # the browser asking for something to be done
            if not self._token_ok():
                self._send(b'{"error":"bad token"}', "application/json", 401)
                return
            raw = self._read_body()
            req = _decode(raw)
            op = req.get("op", "")
            args = req.get("args") or {}
            seq = W.put_command(op, args)
            self._send(json.dumps({"seq": seq}).encode("utf-8"),
                       "application/json")
            return
        elif path == "/res":
            # the far side answering
            if not self._token_ok():
                self._send(b'{"error":"bad token"}', "application/json", 401)
                return
            raw = self._read_body()
            result = _decode(raw)
            if result:
                W.put_result(result)
            self._send(b"", "text/plain", 204)
            return
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
    """This machine's own IPv4 -- the first one that is not loopback.

    It is the address a browser on another machine can reach, so it is
    the one that goes in the URL.

    The addresses are read from the machine, never written down: a box
    can have several (a LAN one, a VPN one, a container one), and the
    first non-loopback one is the one on the path out.

    The socket trick is the fallback: connecting a UDP socket picks the
    route the kernel would use, and no packet leaves -- it is a
    connectionless protocol, so nothing is sent. It is tried first
    because it is one syscall and does not depend on a tool being
    installed.
    """
    # The routing answer first: connecting a UDP socket picks the
    # address the kernel would use to reach the outside, which is the
    # one the far side can reach. No packet leaves -- UDP is
    # connectionless, so connecting only sets the route.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 9))          # discard port; no packets leave
        addr = s.getsockname()[0]
        if addr and not addr.startswith("127."):
            return addr
    except OSError:
        pass
    finally:
        s.close()

    # The interfaces are the fallback: the first non-loopback one.
    for addr in _interface_ipv4s():
        return addr
    return "127.0.0.1"


def _interface_ipv4s() -> list:
    """The IPv4 addresses on the interfaces, in the system's own order.

    Loopback and the link-local ones are left out: neither is an
    address a browser on another machine can reach.
    """
    found = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { "
                 "$_.IPAddress -notlike '127.*' -and "
                 "$_.IPAddress -notlike '169.254.*' } | "
                 "Select-Object -ExpandProperty IPAddress"],
                capture_output=True, text=True, errors="replace").stdout
        else:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                                 capture_output=True, text=True,
                                 errors="replace").stdout
    except (OSError, ValueError):
        return found

    for line in out.splitlines():
        if os.name == "nt":
            addr = line.strip()
        else:
            parts = line.split()
            if "inet" not in parts:
                continue
            addr = parts[parts.index("inet") + 1].split("/")[0]
        if addr and addr.count(".") == 3 and not addr.startswith("127.") \
                and not addr.startswith("169.254."):
            found.append(addr)
    return found


def _bind_or_explain(host: str, port: int):
    """Bind, or say clearly which port is taken."""
    from http.server import ThreadingHTTPServer
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno in (98, 48, 10048, 13):     # EADDRINUSE, Linux/mac/Win, or denied
            raise SystemExit(
                f"cannot bind {host}:{port} -- already in use.")
        raise
    return srv


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
        # Nothing answered. That is not the same as something else
        # answering: a socket in TIME_WAIT from a previous run refuses
        # new connections for a moment, and the server may not have
        # finished starting yet. Either way, this is not proof that
        # another process holds the port, so it must not be reported as
        # one.
        return False
    # our page has a marker another server would not
    return "lj.listen" in body or "lj dialer" in body or 'id="tree"' in body


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    token, note = load_or_create_token(args.token_file, args.regen_token)
    Handler.token = token

    me = local_addr()
    banner = [
        "",
        "  lj.listen  (light_jupyter 4.0.0)",
        f"    listening : {args.host}:{args.port}",
        f"    reachable : {me}:{args.port}",
        f"    token     : {token}",
        f"                ({note})",
        "",
        # The token rides in the fragment, so this whole line is
        # copy-pasteable: open it as-is and the page authenticates
        # itself.
        #
        # The address is the machine's own IPv4 -- the one on the path
        # out -- not 127.0.0.1. A loopback URL only works on this
        # machine, and the browser line is the one a person pastes into
        # a browser that is somewhere else. (On this machine 127.0.0.1
        # would work too, but the address that works everywhere is the
        # one to print.)
        f"  browser : http://{me}:{args.port}/#token={token}",
        "",
        "  hand this to the sending side:",
        "",
        # The token is IN the target now, so the far side needs nothing
        # added to it -- the line is copy-pasteable as it stands. A
        # separate --token is a thing to mistype, and a mistyped token
        # reads as "the two sides disagree" rather than as a typo.
        f"    lj dialer --worker {me}:{args.port}/#token={token}",
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