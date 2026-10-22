"""Port selection.

``--port`` accepts a number, or empty/auto meaning "pick for me".

Auto-pick used to scan 1024-32767 or let the kernel assign one. Measurement
on 2026-10-22 killed both ideas: the lab firewall admits inbound TCP only on
specific administratively opened ports (see pick_port), so a random port
produces a server that runs fine but that no client can reach.
"""

from __future__ import annotations

import socket
from typing import Optional

# Ports we refuse to auto-pick, because binding them would need privileges or
# would collide with the SSH daemon / system services in confusing ways.
RESERVED = frozenset({22, 25, 53, 111, 123, 389, 445, 636, 1433, 3306})

# Ports measured open on the lab firewall (see pick_port docstring).
DEFAULT_CANDIDATES = (8890, 9999)


def parse_port(text: Optional[str]) -> Optional[int]:
    """Accept '', None, 'auto', or a number. Returns None meaning "auto"."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if t in ("", "auto", "none", "any", "free", "0"):
        return None
    try:
        n = int(t)
    except ValueError:
        raise ValueError(f"invalid port {text!r} (use a number, or leave it empty)")
    if not 1 <= n <= 65535:
        raise ValueError(f"port {n} out of range 1-65535")
    return n if n > 0 else None


def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """True when nothing is listening and we can claim the port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def bind_socket(port: int, host: str = "0.0.0.0") -> socket.socket:
    """Bind and listen, returning the listening socket.

    ``port=0`` lets the kernel choose; read the actual value back with
    ``sock.getsockname()[1]``.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(16)
    return s


def pick_port(host: str = "0.0.0.0", start: Optional[int] = None) -> int:
    """Return a free port from the ports this network actually admits.

    Measured 2026-10-22 from the client segment: inbound TCP is dropped for
    almost every port -- 1024, 3000, 5000, 7000, 8000, 8080, 8500, 8800,
    9000, 10000, 11000, 15000, 20410, 25000, 30000, 32000, 37233 and 46361
    all TIME OUT, while 9999 answers RST (nothing listening) and 8890 serves
    HTTP. So admission is per-port (administratively opened), not a range,
    and a random scan would almost always land on a black hole: the server
    runs fine but no client can ever connect.

    The auto choice is therefore the first free port among the known-open
    ones. Pass --port explicitly to override.
    """
    for p in DEFAULT_CANDIDATES:
        if p in RESERVED:
            continue
        if is_port_free(p, host):
            return p
    raise RuntimeError(
        "no free port among {} -- all in use. "
        "Pass --port to choose one explicitly.".format(
            ", ".join(map(str, DEFAULT_CANDIDATES))))