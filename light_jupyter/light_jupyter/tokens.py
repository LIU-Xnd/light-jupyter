"""Key-grade tokens.

What "key grade" means here, concretely:

* **256 bits of entropy minimum.** 32 bytes from ``secrets`` (the OS CSPRNG),
  never ``random``. The previous implementation used
  ``secrets.token_urlsafe(24)`` = 192 bits; that was already CSPRNG-sourced but
  the spec is looser than it should be for a bearer credential.
* **Unbiased, unambiguous alphabet.** Crockford Base32: 32 symbols, so exactly
  5 bits per character with no rounding waste, and ``I``/``L``/``O``/``U`` are
  excluded so a token read aloud or transcribed from a screen cannot be
  mangled into a different valid token. ``secrets.choice`` does rejection
  sampling internally, so there is no modulo bias.
* **Constant-time comparison.** ``hmac.compare_digest``, never ``==``.
* **Grouped for transcription.** 52 chars in 4 groups of 13.
* **Persistable.** A token that changes on every restart is not a credential,
  it is a speed bump. ``load_or_create`` keeps it in a 0600 file.

Layout::

    lj_XXXXXXXXXXXXX-XXXXXXXXXXXXX-XXXXXXXXXXXXX-XXXXXXXXXXXXX
    \\___________________________________/  52 chars = 260 bits

Plus a throttle: a 256-bit secret behind a gate with no attempt limit is only
half a control.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

# Crockford Base32 -- 32 symbols, 5 bits each, no I/L/O/U.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BITS_PER_CHAR = 5
ENTROPY_BYTES = 32            # 256 bits
GROUP_SIZE = 13
PREFIX = "lj_"

TOKEN_RE_BAD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ---------------------------------------------------------------- generation


def make_token(entropy_bytes: int = ENTROPY_BYTES,
               prefix: str = PREFIX,
               group: int = GROUP_SIZE) -> str:
    """Generate a key-grade token.

    ``entropy_bytes`` is rounded UP to whole characters, so the result carries
    at least the requested number of bits (32 bytes -> 52 chars -> 260 bits).
    """
    if entropy_bytes < 16:
        raise ValueError("refusing to generate a token below 128 bits")
    n_chars = -(-(entropy_bytes * 8) // BITS_PER_CHAR)     # ceil division
    body = "".join(secrets.choice(ALPHABET) for _ in range(n_chars))
    if group and group > 0:
        body = "-".join(body[i:i + group] for i in range(0, len(body), group))
    return prefix + body


def entropy_bits(token: str) -> int:
    """Bits of entropy carried by a token, ignoring prefix and separators."""
    body = token
    if body.startswith(PREFIX):
        body = body[len(PREFIX):]
    body = body.replace("-", "")
    return len(body) * BITS_PER_CHAR


# ---------------------------------------------------------------- verification


def verify(expected: str, provided: Optional[str]) -> bool:
    """Constant-time token check. False for None/empty rather than raising."""
    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(expected.encode("utf-8"),
                                   provided.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def is_wellformed(token: str) -> bool:
    """Shape check only -- no secret involved, safe for logs and tests."""
    if not token:
        return False
    body = token[len(PREFIX):] if token.startswith(PREFIX) else token
    parts = body.split("-")
    if not parts:
        return False
    if len({len(p) for p in parts}) != 1:            # ragged grouping
        return False
    return all(c in TOKEN_RE_BAD for c in "".join(parts))


# ---------------------------------------------------------------- persistence


def load_or_create(path: str,
                   prefix: str = PREFIX,
                   entropy_bytes: int = ENTROPY_BYTES) -> Tuple[str, bool]:
    """Return ``(token, created)``.

    Reads ``path`` when it already holds a well-formed token, otherwise
    generates a fresh one and writes it with mode 0600. The file is created
    with the right permissions from the start -- never write-then-chmod, which
    leaves a window where the secret is world-readable.
    """
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing and is_wellformed(existing):
            return existing, False
    except FileNotFoundError:
        pass
    except OSError:
        pass

    token = make_token(entropy_bytes=entropy_bytes, prefix=prefix)

    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # 1. Try an exclusive create first: that is the race-free path, and it
    #    also gets 0600 from the moment the file exists (never write-then-chmod,
    #    which leaves a window where the secret is world-readable).
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # 2. The file exists. Re-read: another process may have just won the
        #    race and written a valid token.
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read().strip()
        except OSError:
            existing = ""
        if existing and is_wellformed(existing):
            return existing, False

        # 3. It exists but holds something unusable (truncated, hand-edited,
        #    written by an older version). O_EXCL can never succeed here, so
        #    we must overwrite in place -- and still keep 0600 by opening the
        #    existing inode with O_TRUNC rather than unlink+create, so we do
        #    not leave a window with no file at all.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        except OSError as e:
            raise RuntimeError(f"cannot rewrite {path}: {e}")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token, True


# ---------------------------------------------------------------- throttle


class AuthThrottle:
    """Per-IP failure tracking with exponential backoff.

    A successful attempt clears the counter. Failures back off 0.5s, 1s, 2s,
    4s ... capped at ``max_delay``. Entries idle past ``ttl`` are pruned, so a
    long-lived server does not accumulate one entry per IP forever.
    """

    def __init__(self, threshold: int = 5, base_delay: float = 0.5,
                 max_delay: float = 30.0, ttl: float = 300.0):
        self.threshold = threshold
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.ttl = ttl
        self._state: Dict[str, Tuple[int, float]] = {}   # ip -> (fails, until)
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        stale = [ip for ip, (_, until) in self._state.items()
                 if until + self.ttl < now]
        for ip in stale:
            self._state.pop(ip, None)

    def retry_after(self, ip: str) -> float:
        """Seconds this IP must wait, or 0.0 when it may try now."""
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._state.get(ip)
            if not entry:
                return 0.0
            fails, until = entry
            if fails < self.threshold:
                return 0.0
            return max(0.0, until - now)

    def fail(self, ip: str) -> float:
        """Record a failure; returns the delay now imposed on this IP."""
        now = time.time()
        with self._lock:
            self._prune(now)
            fails, _ = self._state.get(ip, (0, 0.0))
            fails += 1
            delay = 0.0
            if fails >= self.threshold:
                over = fails - self.threshold
                delay = min(self.base_delay * (2 ** over), self.max_delay)
            self._state[ip] = (fails, now + delay)
            return delay

    def success(self, ip: str) -> None:
        with self._lock:
            self._state.pop(ip, None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    def stats(self) -> Dict[str, int]:
        now = time.time()
        with self._lock:
            self._prune(now)
            return {ip: f for ip, (f, _) in self._state.items()}