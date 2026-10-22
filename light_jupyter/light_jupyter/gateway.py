"""light_jupyter gateway — server side of the reverse tunnel.

The lab gateway admits inbound TCP on only a few ports, so a randomly
auto-picked lj port cannot be reached from the browser. This solves it
without any inbound path: the client on your machine announces itself with
a token-protected UDP beacon, this process listens for that beacon, and then
*dials out* (which is never filtered) to the client's TCP port. Each browser
connection is spliced straight to the client.

    client --UDP beacon (HMAC-authenticated)--> gateway
    browser --admitted port--> gateway --dial out--> client --ssh -L--> lj

The gateway is a pure relay: it never connects to lj itself.

Wire formats
------------
UDP beacon (client -> gateway), repeated every ``--beacon-interval``::

    b'LJGATE1' | 4-byte timestamp | 16-byte truncated HMAC-SHA256 | 2-byte port

The HMAC covers ``timestamp | ip | port`` under the shared secret, so a
third party can neither forge a beacon nor replay an old one (the gateway
rejects timestamps more than ``--beacon-window`` seconds away).

TCP (gateway -> client)::

    b'LJGATE' + <16-byte nonce>

Everything this stores uses the ``_light_jupyter`` suffix.
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
from typing import Optional, Tuple

BEACON_MAGIC = b"LJGATE1"
TCP_BANNER = b"LJGATE"
NONCE_LEN = 16
DEFAULT_SECRET = os.path.expanduser("~/.light_jupyter/nonce_light_jupyter")
MAGIC_LEN = len(BEACON_MAGIC)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- shared secret

def load_or_make_secret(path: str) -> bytes:
    """Read the shared secret, or create one (0600)."""
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "rb") as fh:
            s = fh.read().strip()
        if len(s) >= NONCE_LEN:
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
    return s


def beacon_mac(secret: bytes, ts: int, ip: str, port: int) -> bytes:
    msg = struct.pack("!I", ts) + ip.encode() + struct.pack("!H", port)
    return hmac.new(secret, msg, hashlib.sha256).digest()[:NONCE_LEN]


# ---------------------------------------------------------------- beacon listener

class BeaconListener(threading.Thread):
    """UDP listener that remembers the most recent authenticated client."""

    def __init__(self, port: int, secret: bytes, window: float = 90.0,
                 ttl: float = 60.0):
        super().__init__(daemon=True)
        self.secret = secret
        self.window = window
        self.ttl = ttl
        self.client: Optional[Tuple[str, int]] = None
        self.seen_at = 0.0
        self._lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(1.0)

    def run(self) -> None:
        log(f"  beacon listener on 0.0.0.0:{self.sock.getsockname()[1]}/udp")
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            port = self._verify(data, addr[0])
            if port is None:
                continue
            with self._lock:
                changed = self.client != (addr[0], port)
                self.client = (addr[0], port)
                self.seen_at = time.time()
            if changed:
                log(f"  *** client found: {addr[0]}:{port}")
            else:
                log(f"    beacon from {addr[0]}:{port}")

    def _verify(self, data: bytes, ip: str) -> Optional[int]:
        """Return the announced port, or None when the beacon is not ours."""
        if len(data) < MAGIC_LEN + 4 + NONCE_LEN + 2:
            return None
        if not data.startswith(BEACON_MAGIC):
            return None
        off = MAGIC_LEN
        (ts,) = struct.unpack("!I", data[off:off + 4])
        off += 4
        mac = data[off:off + NONCE_LEN]
        off += NONCE_LEN
        (port,) = struct.unpack("!H", data[off:off + 2])
        if abs(int(time.time()) - ts) > self.window:
            log(f"  ! beacon from {ip} too old (replay?)")
            return None
        if not hmac.compare_digest(mac,
                                   beacon_mac(self.secret, ts, ip, port)):
            log(f"  ! beacon from {ip} failed the MAC check")
            return None
        return port

    def current(self):
        """The live client, or None when no fresh beacon has arrived."""
        with self._lock:
            if self.client and time.time() - self.seen_at <= self.ttl:
                return self.client
            return None


# ---------------------------------------------------------------- relay

def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(browser: socket.socket, client_addr: Tuple[str, int],
           secret: bytes) -> None:
    """Dial the client, greet with the nonce, then splice browser <-> client.

    No priming read on the browser socket: HTTP servers may wait for the
    request, and a blocking recv here would stall both directions.
    """
    up = None
    try:
        up = socket.create_connection(client_addr, timeout=10)
        up.sendall(TCP_BANNER + secret[:NONCE_LEN])
        for a, b in ((browser, up), (up, browser)):
            threading.Thread(target=pump, args=(a, b), daemon=True).start()
    except OSError as e:
        log(f"  ! {e}")
        try:
            browser.close()
        except OSError:
            pass


def serve(listen_port: int, beacon: BeaconListener, secret: bytes) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(128)
    srv.settimeout(1.0)
    log(f"gateway listening on 0.0.0.0:{listen_port}/tcp")
    announced = False
    while True:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            if not announced and beacon.current():
                t = beacon.current()
                log(f"  ready: relaying to {t[0]}:{t[1]}")
                announced = True
            continue
        except OSError:
            break
        target = beacon.current()
        if target is None:
            log(f"  browser from {addr[0]}: no client beacon yet -- dropped")
            try:
                conn.close()
            except OSError:
                pass
            continue
        log(f"  browser from {addr[0]} -> {target[0]}:{target[1]}")
        threading.Thread(target=handle, args=(conn, target, secret),
                         daemon=True).start()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m light_jupyter.gateway",
        description="relay browser traffic back to your announced client")
    p.add_argument("--listen-port", type=int, required=True,
                   help="a port the network admits (e.g. 8890)")
    p.add_argument("--beacon-port", type=int, default=47811,
                   help="UDP port the client beacons to (default: 47811)")
    p.add_argument("--secret-file", default=DEFAULT_SECRET,
                   help="shared secret, byte-identical on both machines")
    p.add_argument("--beacon-window", type=float, default=90.0,
                   help="accept beacons up to this many seconds old")
    p.add_argument("--client-ttl", type=float, default=60.0,
                   help="forget a client after this long without a beacon")
    args = p.parse_args(argv)

    secret = load_or_make_secret(args.secret_file)
    bl = BeaconListener(args.beacon_port, secret, window=args.beacon_window,
                        ttl=args.client_ttl)
    bl.start()

    log("")
    log("  light_jupyter gateway")
    log(f"    tcp listen : {args.listen_port}")
    log(f"    udp beacon : {args.beacon_port}")
    log(f"    secret     : {args.secret_file}")
    log("")
    log("  copy that secret file to your machine, then run the client")
    log("")
    serve(args.listen_port, bl, secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())