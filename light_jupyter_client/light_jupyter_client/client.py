"""Reverse-tunnel client — runs on YOUR machine, next to the browser.

Assumes the server has **no inbound port at all** (egress only). So the
server dials *us*; nothing ever connects to it:

    browser -> 127.0.0.1:<browser-port>        (this process)
            -> the socket the server opened from outside
            -> lj on the server

Two listeners:

* ``--listen-port``  the port the server dials; the HMAC handshake runs here
  and a session that fails it is dropped before any byte is forwarded.
* ``--browser-port`` where your browser connects, bound to loopback by
  default (``--browser-host 0.0.0.0`` to reach it from the LAN).

Because the server can only afford one dial-out, every browser connection is
multiplexed over that single socket:

    4-byte length | 1-byte type | 4-byte stream id | payload

    0x01 OPEN   client -> server   please open a stream
    0x02 DATA   both               stream payload
    0x03 CLOSE  both               stream finished
    0x04 ACK    server -> client   the stream is attached to lj
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import socket
import struct
import sys
import threading
import time
from typing import Dict, Optional

HELLO = b"LJGATE"
MAC_LEN = 16
STAMP_LEN = 4
HELLO_WINDOW = 120.0

OPEN, DATA, CLOSE, ACK = 1, 2, 3, 4
HDR = struct.Struct("!IBI")
MAX_FRAME = 1 << 20

DEFAULT_SECRET = os.path.expanduser("~/.light_jupyter/secret_light_jupyter")


def log(msg: str) -> None:
    print(msg, flush=True)


def load_secret(path: str) -> bytes:
    """The shared secret; must match the server's byte for byte."""
    for cand in (path, os.path.join(os.getcwd(), "secret_light_jupyter")):
        try:
            with open(cand, "rb") as fh:
                s = fh.read().strip()
            if len(s) >= 16:
                return s
        except OSError:
            continue
    raise SystemExit(
        f"no shared secret found (looked in {path} and the cwd).\n"
        "\n"
        "  the server prints a one-liner on its first run. copy it here:\n"
        "\n"
        "    echo <BASE64> | base64 -d > secret_light_jupyter\n"
        "    icacls secret_light_jupyter /inheritance:r"
        " /grant:r \"%USERNAME%:R\"\n")


def hello_mac(secret: bytes, ts: int) -> bytes:
    return hmac.new(secret, struct.pack("!I", ts),
                    hashlib.sha256).digest()[:MAC_LEN]


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def local_addr() -> str:
    """Our address on the path to the server (a UDP connect sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.117.41", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- tunnel

class Tunnel:
    """The one socket the server dialled in on; multiplexes browser streams."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
        self.streams: Dict[int, socket.socket] = {}
        self.next_id = 1
        self.alive = True

    def send(self, typ: int, sid: int, payload: bytes = b"") -> None:
        with self.lock:
            if not self.alive:
                raise ConnectionError("tunnel closed")
            self.sock.sendall(HDR.pack(HDR.size + len(payload), typ, sid)
                              + payload)

    def new_stream(self) -> int:
        with self.lock:
            sid = self.next_id
            self.next_id += 1
        self.send(OPEN, sid)
        return sid

    def register(self, sid: int, browser: socket.socket) -> None:
        with self.lock:
            self.streams[sid] = browser

    def reader(self) -> None:
        try:
            while self.alive:
                hdr = recv_exact(self.sock, HDR.size)
                length, typ, sid = HDR.unpack(hdr)
                if length > MAX_FRAME:
                    break
                payload = (recv_exact(self.sock, length - HDR.size)
                           if length > HDR.size else b"")
                if typ in (DATA, ACK):
                    with self.lock:
                        target = self.streams.get(sid)
                    if target is not None:
                        target.sendall(payload)
                    if typ == ACK:
                        log(f"  stream {sid} attached to lj")
                elif typ == CLOSE:
                    with self.lock:
                        self.streams.pop(sid, None)
        except (OSError, ConnectionError):
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        with self.lock:
            if not self.alive:
                return
            self.alive = False
            streams = list(self.streams.values())
            self.streams.clear()
        for s in streams:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
        log("  tunnel closed")


# ---------------------------------------------------------------- sessions

class Session:
    """Holds the live tunnel so the browser listener can find it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tunnel: Optional[Tunnel] = None

    def set(self, t: Tunnel) -> None:
        with self.lock:
            self.tunnel = t

    def get(self) -> Optional[Tunnel]:
        with self.lock:
            return self.tunnel if self.tunnel and self.tunnel.alive else None

    def clear(self) -> None:
        with self.lock:
            self.tunnel = None


def authenticate(conn: socket.socket, secret: bytes) -> bool:
    """Banner + fresh timestamp + constant-time HMAC."""
    conn.settimeout(15)
    try:
        head = recv_exact(conn, len(HELLO) + MAC_LEN + STAMP_LEN)
    except (OSError, ConnectionError):
        return False
    if not head.startswith(HELLO):
        log("  ! rejected: bad banner")
        return False
    mac = head[len(HELLO):len(HELLO) + MAC_LEN]
    (ts,) = struct.unpack("!I", head[len(HELLO) + MAC_LEN:])
    if abs(time.time() - ts) > HELLO_WINDOW:
        log("  ! rejected: stale handshake")
        return False
    if not hmac.compare_digest(mac, hello_mac(secret, ts)):
        log("  ! rejected: bad secret")
        return False
    conn.settimeout(None)
    return True


def server_session(conn: socket.socket, addr, secret: bytes,
                   session: Session) -> None:
    log(f"  dial-in from {addr[0]}:{addr[1]}")
    if not authenticate(conn, secret):
        try:
            conn.close()
        except OSError:
            pass
        return
    log(f"  server authenticated ({addr[0]})")
    t = Tunnel(conn)
    session.set(t)
    t.reader()                       # blocks until the server hangs up
    session.clear()


def browser_session(browser: socket.socket, addr, tunnel: Tunnel) -> None:
    """One browser connection = one multiplexed stream."""
    try:
        sid = tunnel.new_stream()
    except (OSError, ConnectionError):
        try:
            browser.close()
        except OSError:
            pass
        return
    tunnel.register(sid, browser)
    log(f"  browser {addr[0]} -> stream {sid}")
    try:
        while True:
            data = browser.recv(65536)
            if not data:
                break
            tunnel.send(DATA, sid, data)
    except OSError:
        pass
    finally:
        try:
            tunnel.send(CLOSE, sid)
        except (OSError, ConnectionError):
            pass
        try:
            browser.close()
        except OSError:
            pass


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m light_jupyter_client",
        description="bridge your browser to a light_jupyter server that has "
                    "no inbound ports")
    p.add_argument("--listen-port", type=int, default=45901,
                   help="port the server dials (default: 45901)")
    p.add_argument("--listen-host", default="0.0.0.0",
                   help="bind for the server's dial-in (default: 0.0.0.0)")
    p.add_argument("--browser-port", type=int, default=48999,
                   help="port your browser connects to (default: 48999)")
    p.add_argument("--browser-host", default="127.0.0.1",
                   help="bind for the browser; 0.0.0.0 exposes it to the LAN "
                        "(default: 127.0.0.1)")
    p.add_argument("--secret-file", default=DEFAULT_SECRET)
    p.add_argument("--print-only", action="store_true",
                   help="print the server-side command, then exit")
    args = p.parse_args(argv)

    secret = load_secret(args.secret_file)
    me = local_addr()

    # A beacon cannot work here (inbound UDP is filtered too), so the one
    # thing the user has to do is copy this address to the server.
    server_cmd = (f"python -m light_jupyter.gateway --client {me} "
                  f"{args.listen_port} --lj-port <LJ-PORT>")

    log("")
    log("  light_jupyter client  (egress-only tunnel)")
    log(f"    this machine : {me}")
    log(f"    server dials : {me}:{args.listen_port}")
    log(f"    browser at   : http://127.0.0.1:{args.browser_port}")
    log("")
    log("  copy this line to the server terminal:")
    log("")
    log(f"    {server_cmd}")
    log("")
    if args.print_only:
        return 0

    session = Session()

    br = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    br.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    br.bind((args.browser_host, args.browser_port))
    br.listen(64)
    br.settimeout(1.0)

    sr = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sr.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sr.bind((args.listen_host, args.listen_port))
    sr.listen(4)
    sr.settimeout(1.0)

    log(f"  browser : {args.browser_host}:{args.browser_port}")
    log(f"  awaiting the server on {args.listen_host}:{args.listen_port}")
    log("")

    def accept_browser() -> None:
        while True:
            try:
                conn, addr = br.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = session.get()
            if t is None:
                log("  browser arrived with no tunnel up -- dropped")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=browser_session, args=(conn, addr, t),
                             daemon=True).start()

    def accept_server() -> None:
        while True:
            try:
                conn, addr = sr.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=server_session,
                             args=(conn, addr, secret, session),
                             daemon=True).start()

    threading.Thread(target=accept_browser, daemon=True).start()
    threading.Thread(target=accept_server, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())