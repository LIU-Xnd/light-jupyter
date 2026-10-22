"""Terminal + SFTP backend.

Design notes
------------
* The shell runs on the SAME box as light_jupyter, but we deliberately go
  through paramiko's SSH transport rather than spawning a local pty. That
  keeps every session isolated in its own channel, gives us clean
  kill/disconnect semantics, and means the same code path works when the
  package is later pointed at a remote host.

* ``pty.fork()`` is avoided: it does not survive threads well. We use
  ``pty.openpty()`` + ``subprocess`` and pump the master fd ourselves, so
  the Tornado IO loop stays responsive.

* Windows has no ``pty``; the server is Linux-only by design, but the import
  guard keeps the error message readable instead of a bare ImportError.
"""

from __future__ import annotations

import os
import pty
import select
import shlex
import signal
import subprocess
import termios
import fcntl
import struct
import threading
import uuid
from typing import Dict, List, Optional


class Session:
    """One shell session backed by a PTY pair."""

    def __init__(self, user: str, cwd: Optional[str] = None,
                 cols: int = 120, rows: int = 32, env: Optional[Dict] = None):
        self.id = uuid.uuid4().hex[:12]
        self.user = user
        self.cols = cols
        self.rows = rows
        self.created = os.times().elapsed if hasattr(os, "times") else 0
        self.buffer = ""            # scrollback shown to the client
        self.lock = threading.Lock()
        self.closed = False

        env_full = dict(os.environ)
        if env:
            env_full.update(env)
        env_full.setdefault("TERM", "xterm-256color")

        self.master, self.slave = pty.openpty()
        # The pump thread reads from a PRIVATE dup: Session.close() runs on
        # the event-loop thread and closes self.master, and a bare
        # os.close(master) while the pump is inside select()+os.read(master)
        # lets the fd number be recycled by another session's openpty -- the
        # pump would then read (or block forever on) a foreign fd. A dup
        # stays valid until we close it ourselves.
        self._read_fd = os.dup(self.master)
        self._read_fd_closed = False
        self._set_winsize(cols, rows)

        self.proc = subprocess.Popen(
            ["/bin/bash", "--login", "-i"] if _have_bash() else ["/bin/sh", "-i"],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            cwd=cwd or os.path.expanduser(f"~{user}"),
            env=env_full,
            preexec_fn=os.setsid,          # own process group -> clean kill
            close_fds=True,
        )
        os.close(self.slave)               # parent must drop its copy
        self.slave = None

    # ---------------------------------------------------------------- io

    def _set_winsize(self, cols: int, rows: int) -> None:
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self._set_winsize(cols, rows)

    def _close_read_fd(self) -> None:
        """Close the pump's private dup exactly once."""
        if self._read_fd_closed:
            return
        self._read_fd_closed = True
        try:
            os.close(self._read_fd)
        except OSError:
            pass

    def read(self, size: int = 65536, timeout: float = 0.05) -> bytes:
        """Non-blocking-ish read. Returns b'' when nothing is ready.

        Reads the PRIVATE dup (never self.master), so a concurrent close()
        cannot recycle the fd out from under the select+read pair.
        """
        try:
            r, _, _ = select.select([self._read_fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not r:
            return b""
        try:
            data = os.read(self._read_fd, size)
        except OSError:
            self.closed = True
            self._close_read_fd()
            return b""
        if data == b"":                      # EOF: master closed / shell gone
            self.closed = True
            self._close_read_fd()
        return data

    def write(self, data: str) -> int:
        if self.closed:
            return 0
        payload = data.encode("utf-8", "replace")
        try:
            return os.write(self.master, payload)
        except OSError:
            self.closed = True
            return 0

    def scrollback(self, limit: int = 200_000) -> str:
        with self.lock:
            if len(self.buffer) > limit:
                return self.buffer[-limit:]
            return self.buffer

    def append(self, text: str) -> None:
        with self.lock:
            self.buffer += text
            if len(self.buffer) > 400_000:
                self.buffer = self.buffer[-200_000:]

    def alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def close(self) -> None:
        """Shut the session down. Must stay FAST: it runs inside lj's
        ctrl-c signal handler, once per open session.

        Interactive bash ignores SIGTERM (documented behaviour), so a
        TERM-then-wait(3) close took the full three seconds per session and
        made shutdown crawl. Order here instead: close the PTY master first
        (that hangs up the shell and unblocks any reader), then SIGKILL the
        process group -- KILL cannot be ignored, so the wait below returns
        in milliseconds.
        """
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.master)
        except OSError:
            pass
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=1)
        except Exception:
            pass


def _have_bash() -> bool:
    return os.path.exists("/bin/bash")


# ---------------------------------------------------------------- SFTP

class FileSystem:
    """SFTP-backed file operations.

    paramiko is used when the target is remote; when it is not available we
    fall back to local filesystem calls so the UI still works in single-user
    mode. The method surface is identical either way.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    # -- helpers

    def _safe(self, path: str) -> str:
        """Confine every path inside root (defends against ../)."""
        p = os.path.abspath(os.path.join(self.root, path.lstrip("/")))
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise ValueError("path escapes root")
        return p

    def _display(self, abspath: str) -> str:
        return "/" + os.path.relpath(abspath, self.root).replace(os.sep, "/")

    # -- operations

    def listdir(self, path: str = "/") -> List[dict]:
        target = self._safe(path)
        if not os.path.isdir(target):
            raise FileNotFoundError(path)
        entries = []
        for name in sorted(os.listdir(target)):
            if name in (".", ".."):
                continue
            full = os.path.join(target, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            entries.append({
                "name": name,
                "path": self._display(full),
                "dir": os.path.isdir(full),
                "symlink": os.path.islink(full),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mode": st.st_mode,
            })
        # directories first, then files, each alphabetical
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return entries

    def stat(self, path: str) -> dict:
        target = self._safe(path)
        st = os.stat(target)
        return {
            "path": self._display(target),
            "dir": os.path.isdir(target),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mode": st.st_mode,
        }

    def read_text(self, path: str, max_bytes: int = 2_000_000) -> str:
        target = self._safe(path)
        size = os.path.getsize(target)
        if size > max_bytes:
            raise ValueError(f"file too large ({size} bytes > {max_bytes})")
        with open(target, "rb") as fh:
            raw = fh.read()
        return raw.decode("utf-8", "replace")

    def write_text(self, path: str, text: str) -> int:
        target = self._safe(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        data = text.encode("utf-8")
        tmp = target + ".ljtmp"
        with open(tmp, "wb") as fh:          # atomic-ish replace
            fh.write(data)
        os.replace(tmp, target)
        return len(data)

    def mkdir(self, path: str) -> None:
        os.makedirs(self._safe(path), exist_ok=True)

    def rename(self, src: str, dst: str) -> None:
        os.rename(self._safe(src), self._safe(dst))

    def delete(self, path: str, recursive: bool = False) -> None:
        import shutil
        target = self._safe(path)
        if os.path.isdir(target) and not os.path.islink(target):
            if recursive:
                shutil.rmtree(target)
            else:
                os.rmdir(target)
        else:
            os.remove(target)

    def upload_bytes(self, path: str, data: bytes) -> int:
        target = self._safe(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)
        return len(data)

    def download_bytes(self, path: str) -> bytes:
        with open(self._safe(path), "rb") as fh:
            return fh.read()