"""light_jupyter client — your side of the reverse tunnel.

Run this on YOUR machine. It:

1. announces itself to the server with a token-protected UDP beacon, so the
   server knows where to send traffic back;
2. listens on a TCP port; every connection the server opens here is verified
   and then relayed to your local lj (which you reach through an SSH tunnel,
   because the server cannot open an inbound connection to this machine on a
   random port).

    lj (server) --ssh -L--> localhost:PORT --client--> browser

Frame protocol, both directions share one secret file:

* UDP beacon (repeated every ``--beacon-interval`` seconds)::

      b'LJGATE1' | 4-byte timestamp | 16-byte HMAC(timestamp|ip|port) | 2-byte port

  The HMAC is truncated to 16 bytes. The server also rejects beacons whose
  timestamp is more than ``--beacon-window`` seconds away, so a captured
  beacon cannot be replayed later.

* TCP (server -> client)::

      b'LJGATE' + <16-byte nonce>

  Checked before a single payload byte is forwarded, so only the holder of
  the secret file may use the relay. This leg carries the web session in
  cleartext -- run it on a network you trust.
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
from typing import Tuple

BEACON_MAGIC = b"LJGATE1"
TCP_BANNER = b"LJGATE"
NONCE_LEN = 16
MAGIC_LEN = len(BEACON_MAGIC)
DEFAULT_SECRET = os.path.expanduser("~/.light_jupyter/nonce_light_jupyter")


def log(msg: str) -> None:
    print(msg, flush=True)


def load_secret(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            s = fh.read().strip()
    except OSError as e:
        raise SystemExit(
            f"cannot read secret file {path}: {e}\n"
            "  copy it from the server:  "
            "~/.light_jupyter/nonce_light_jupyter")
    if len(s) < NONCE_LEN:
        raise SystemExit(f"secret too short ({len(s)} bytes, need >= {NONCE_LEN})")
    return s


def local_ip(target: str) -> str:
    """Our address on the path to `target` (a UDP connect sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def beacon_mac(secret: bytes, ts: int, ip: str, port: int) -> bytes:
    msg = struct.pack("!I", ts) + ip.encode() + struct.pack("!H", port)
    return hmac.new(secret, msg, hashlib.sha256).digest()[:NONCE_LEN]


def beacon_loop(secret: bytes, server: str, beacon_port: int,
                listen_port: int, ip: str, interval: float) -> None:
    """Announce ourselves on a loop; the server forgets us if this stops."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        try:
            ts = int(time.time())
            packet = (BEACON_MAGIC + struct.pack("!I", ts)
                      + beacon_mac(secret, ts, ip, listen_port)
                      + struct.pack("!H", listen_port))
            sock.sendto(packet, (server, beacon_port))
        except OSError as e:
            log(f"  ! beacon failed: {e}")
        time.sleep(interval)


def pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy src -> dst until either side closes."""
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


def relay(conn: socket.socket, target: Tuple[str, int],
          secret: bytes) -> None:
    """Verify the frame, then splice the connection to the local lj.

    recv() deliberately over-reads: payload bytes may share the segment with
    the banner, and they must be forwarded, not dropped.
    """
    try:
        conn.settimeout(10)
        need = len(TCP_BANNER) + NONCE_LEN
        head = b""
        while len(head) < need:
            chunk = conn.recv(65536)
            if not chunk:
                return
            head += chunk
        if head[:len(TCP_BANNER)] != TCP_BANNER:
            log("  ! rejected: bad banner")
            return
        if not hmac.compare_digest(head[len(TCP_BANNER):need],
                                   secret[:NONCE_LEN]):
            log("  ! rejected: bad secret")
            return
        up = socket.create_connection(target, timeout=10)
        conn.settimeout(None)
        up.settimeout(None)
        leftover = head[need:]
        if leftover:
            up.sendall(leftover)
        log("  + relayed to lj")
        threading.Thread(target=pump, args=(conn, up), daemon=True).start()
        pump(up, conn)
    except OSError as e:
        log(f"  ! relay failed: {e}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(listen_port: int, lj_target: Tuple[str, int],
          secret: bytes) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(64)
    srv.settimeout(1.0)
    log(f"  listening on 0.0.0.0:{listen_port}")
    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log(f"  connection from {addr[0]}")
            threading.Thread(target=relay, args=(conn, lj_target, secret),
                             daemon=True).start()
    finally:
        srv.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m light_jupyter.client",
        description="announce to the server and relay its traffic to lj")
    p.add_argument("--listen-port", type=int, required=True,
                   help="TCP port the server dials back to")
    p.add_argument("--server", default="192.168.117.41",
                   help="lj server address (default: 192.168.117.41)")
    p.add_argument("--beacon-port", type=int, default=47811,
                   help="UDP port to announce to (default: 47811)")
    p.add_argument("--beacon-interval", type=float, default=10.0,
                   help="seconds between beacons (default: 10)")
    p.add_argument("--lj-host", default="127.0.0.1",
                   help="where lj listens for us (default: 127.0.0.1)")
    p.add_argument("--lj-port", type=int, required=True,
                   help="local port from the ssh -L tunnel, e.g. 48999")
    p.add_argument("--secret-file", default=DEFAULT_SECRET)
    args = p.parse_args(argv)

    secret = load_secret(args.secret_file)
    ip = local_ip(args.server)

    log("")
    log("  light_jupyter client")
    log(f"    my address : {ip}")
    log(f"    listening  : {ip}:{args.listen_port}")
    log(f"    announcing : {args.server}:{args.beacon_port}/udp"
        f" every {args.beacon_interval:g}s")
    log(f"    relaying to: {args.lj_host}:{args.lj_port}")
    log("")

    threading.Thread(target=beacon_loop,
                     args=(secret, args.server, args.beacon_port,
                           args.listen_port, ip, args.beacon_interval),
                     daemon=True).start()
    serve(args.listen_port, (args.lj_host, args.lj_port), secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())