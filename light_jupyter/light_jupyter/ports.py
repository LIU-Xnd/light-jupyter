"""Port selection.

``--port`` accepts a number, or empty/auto meaning "pick for me": scan
1024-32767 from a random starting point and take the first free port.

Note for restricted networks: if the printed URL never loads from your
client machine, the upstream ACLs may be dropping the port (measured on one
lab network 2026-10-22: only a handful of ports admitted). In that case
pass ``--port`` with a port your network allows.
"""

from __future__ import annotations

import random
import socket
from typing import Optional

# Ports we refuse to auto-pick, because binding them would need privileges or
# would collide with the SSH daemon / system services in confusing ways.
RESERVED = frozenset({22, 25, 53, 111, 123, 389, 445, 636, 1433, 3306})

LO, HI = 1024, 32767


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
    """Scan LO-HI from a random start and return the first free port.

    The scan deliberately stays below the OS ephemeral range: ``bind(0)``
    would hand back something in 32768+, which is both less predictable to
    share and, on some lab networks, sits in a dropped-by-ACL range.

    ``start`` picks the scan's first port (random by default) so repeated
    launches do not all queue up behind the same number.
    """
    if start is None:
        start = random.randint(LO, HI - 1)
    span = HI - LO
    for i in range(span):
        p = LO + ((start - LO + i) % span)
        if p in RESERVED:
            continue
        if is_port_free(p, host):
            return p
    raise RuntimeError(
        f"no free port in {LO}-{HI}; pass --port to choose one explicitly")