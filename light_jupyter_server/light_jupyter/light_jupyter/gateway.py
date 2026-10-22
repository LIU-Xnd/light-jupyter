"""light_jupyter gateway — the server side of the egress-only tunnel.

The server has **no inbound port open** (egress only), so it cannot accept a
browser connection, and it cannot be discovered by a beacon either: on this
network inbound UDP is filtered just like TCP, so a client cannot announce
itself. The address is therefore typed in once:

    python -m light_jupyter.gateway --client 192.168.77.160 45901 --lj-port 8890

From then on the server dials *out* to the client (never filtered) and keeps
that single socket open, multiplexing every browser connection over it:

    browser -> 127.0.0.1:48999      (client, on the browser's machine)
            -> the one outbound socket from here
            -> stream -> lj on 127.0.0.1:<lj-port>

The dial-in is authenticated: 'LJGATE' + 4-byte timestamp + 16-byte truncated
HMAC-SHA256 over the timestamp, compared in constant time. A replayed
handshake is rejected (the timestamp must be fresh) and, without the secret
file, nothing can attach. Every browser connection then arrives as its own
stream on the same socket:

    4-byte length | 1-byte type | 4-byte stream id | payload
    0x01 OPEN, 0x02 DATA, 0x03 CLOSE, 0x04 ACK

This module never listens for inbound traffic.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
import socket
import struct
import sys
import threading
import time
from typing import Dict, Optional, Tuple

HELLO = b"LJGATE"
MAC_LEN = 16
STAMP_LEN = 4

OPEN, DATA, CLOSE, ACK = 1, 2, 3, 4
HDR = struct.Struct("!IBI")
MAX_FRAME = 1 << 20

DEFAULT_SECRET = os.path.expanduser("~/.light_jupyter/secret_light_jupyter")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- secret

def load_or_make_secret(path: str) -> bytes:
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "rb") as fh:
            s = fh.read().strip()
        if len(s) >= 16:
            return s
    except OSError:
        pass
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    s = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(s)
    log(f"  secret created: {path}")
    log(f"  copy it to the browser machine:  scp <this-host>:{path} .")
    return s


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


# ---------------------------------------------------------------- server side

class ClientLink:
    """The single outbound socket to the client, plus its streams."""

    def __init__(self, sock: socket.socket, lj: Tuple[str, int], secret: bytes):
        self.sock = sock
        self.lj = lj
        self.secret = secret
        self.lock = threading.Lock()
        self.streams: Dict[int, Optional[socket.socket]] = {}
        self.alive = True

    def send(self, typ: int, sid: int, payload: bytes = b"") -> None:
        with self.lock:
            if not self.alive:
                raise ConnectionError("link closed")
            self.sock.sendall(HDR.pack(HDR.size + len(payload), typ, sid)
                              + payload)

    def open_stream(self, sid: int) -> socket.socket:
        """Attach a fresh socket to lj for stream ``sid``."""
        up = socket.create_connection(self.lj, timeout=10)
        up.settimeout(None)
        with self.lock:
            self.streams[sid] = up
        self.send(ACK, sid)
        threading.Thread(target=self._pump, args=(sid, up), daemon=True).start()
        return up

    def _pump(self, sid: int, up: socket.socket) -> None:
        try:
            while True:
                data = up.recv(65536)
                if not data:
                    break
                self.send(DATA, sid, data)
        except OSError:
            pass
        finally:
            with self.lock:
                self.streams.pop(sid, None)
            try:
                self.send(CLOSE, sid)
            except (OSError, ConnectionError):
                pass
            try:
                up.close()
            except OSError:
                pass

    def close(self) -> None:
        with self.lock:
            if not self.alive:
                return
            self.alive = False
            streams = [s for s in self.streams.values() if s is not None]
            self.streams.clear()
        for s in streams:
            try:
                s.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
        log("  link closed")


def run_link(sock: socket.socket, lj: Tuple[str, int],
             secret: bytes) -> None:
    """Read the client's frames until it hangs up."""
    link = ClientLink(sock, lj, secret)
    log(f"  link up -> lj {lj[0]}:{lj[1]}")
    try:
        while link.alive:
            hdr = recv_exact(sock, HDR.size)
            length, typ, sid = HDR.unpack(hdr)
            if length > MAX_FRAME:
                break
            payload = (recv_exact(sock, length - HDR.size)
                       if length > HDR.size else b"")
            if typ == OPEN:
                link.open_stream(sid)
                log(f"  stream {sid} opened")
            elif typ == DATA:
                with link.lock:
                    up = link.streams.get(sid)
                if up is None:
                    # data before ACK: attach lazily rather than drop it
                    up = link.open_stream(sid)
                try:
                    up.sendall(payload)
                except OSError:
                    with link.lock:
                        link.streams.pop(sid, None)
            elif typ == CLOSE:
                with link.lock:
                    up = link.streams.pop(sid, None)
                if up is not None:
                    try:
                        up.close()
                    except OSError:
                        pass
    except (OSError, ConnectionError):
        pass
    finally:
        link.close()


def connect(client: Tuple[str, int], lj: Tuple[str, int], secret: bytes,
            retry: float = 3.0, forever: bool = True) -> int:
    """Dial out to the client and keep the link alive."""
    while True:
        try:
            log(f"dialing {client[0]}:{client[1]} ...")
            sock = socket.create_connection(client, timeout=15)
            sock.settimeout(None)
            ts = int(time.time())
            sock.sendall(HELLO + hello_mac(secret, ts)
                         + struct.pack("!I", ts))
            log("  handshake sent")
            run_link(sock, lj, secret)
        except OSError as e:
            log(f"  ! {e}")
        if not forever:
            return 1
        log(f"  retrying in {retry:g}s")
        time.sleep(retry)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m light_jupyter.gateway",
        description="dial out to the browser machine and carry its traffic "
                    "to lj (no inbound ports are used)")
    p.add_argument("--client", nargs=2, required=True,
                   metavar=("IP", "PORT"),
                   help="the browser machine, printed by the client")
    p.add_argument("--lj-port", type=int, required=True,
                   help="lj's local port")
    p.add_argument("--lj-host", default="127.0.0.1")
    p.add_argument("--secret-file", default=DEFAULT_SECRET)
    p.add_argument("--once", action="store_true",
                   help="give up instead of retrying when the client is away")
    args = p.parse_args(argv)

    secret = load_or_make_secret(args.secret_file)
    client = (args.client[0], int(args.client[1]))
    lj = (args.lj_host, args.lj_port)

    log("")
    log("  light_jupyter gateway  (egress only)")
    log(f"    dialing  : {client[0]}:{client[1]}")
    log(f"    lj       : {lj[0]}:{lj[1]}")
    log(f"    secret   : {args.secret_file}")
    log("")
    return connect(client, lj, secret, forever=not args.once)


if __name__ == "__main__":
    sys.exit(main())