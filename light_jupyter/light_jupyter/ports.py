"""Port selection.

Any free port works. The box has no port whitelist, so there is nothing to
work around: when ``--port`` is empty we simply let the kernel assign one and
read the real number back off the socket.

    --port 8890     -> 8890
    --port ''       -> whatever the kernel hands out
    --port auto     -> same as ''

The only thing worth doing carefully is binding the socket ourselves instead
of calling ``app.listen()``: that way a port that was free a moment ago but
got taken in between surfaces as a clear message rather than an
EADDRINUSE traceback from deep inside Tornado, and the bound port number is
always known.
"""

from __future__ import annotations

import socket
import random
from typing import Optional

# Ports we refuse to auto-pick, because binding them would need privileges or
# would collide with the SSH daemon / system services in confusing ways.
RESERVED = frozenset({22, 25, 53, 111, 123, 389, 445, 636, 1433, 3306})


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
    """Choose a free port below the OS ephemeral range and return it.

    Why not just ``bind(port=0)``? That hands back something in 32768-60999,
    and on this network the ephemeral range is not reachable from the client
    (measured: 37233 TIMEOUT while 8890 and 9999 both answer). Scanning
    1024-32767 keeps us in the range that is reachable.

    ``start`` picks the scan's first port (random by default) so repeated
    launches do not all queue up behind the same number.
    """
    lo, hi = 1024, 32767
    if start is None:
        start = random.randint(lo, hi - 1)
    span = hi - lo
    for i in range(span):
        p = lo + ((start - lo + i) % span)
        if p in RESERVED:
            continue
        if is_port_free(p, host):
            return p
    raise RuntimeError(
        f"no free port in {lo}-{hi}; pass --port to choose one explicitly")