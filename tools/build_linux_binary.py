"""Build a single-file Linux binary for the far side.

The point is a machine that cannot be reached -- a server with no
Python, no conda, no pip -- still being able to be worked on: to run
the worker that serves the browser's requests, and to send a message
out. So the binary has to carry its own interpreter.

The worker is the half that matters. It is the side that has to dial
out, and it is the side that runs on the box with nothing installed.

Build it on the machine it will run on. A PyInstaller binary links
against the glibc of the system that built it, and glibc only ever goes
forwards: a binary built on Ubuntu 20.04 runs on 20.04 and anything
newer, but one built on 24.04 will not run on 20.04.

Usage:
    python tools/build_linux_binary.py
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "tools" / "freeze_entry.py"
DIST = ROOT / "dist" / "linux"


def log(msg: str) -> None:
    print(msg, flush=True)


def _find_pyinstaller() -> "str | None":
    """The pyinstaller to run.

    It is usually not on PATH when the interpreter is named by its full
    path -- which is how a build is usually run, and how this one was
    run. A conda env keeps its scripts in the env's own bin directory,
    so that is looked in as well.
    """
    found = shutil.which("pyinstaller")
    if found:
        return found
    bindir = Path(sys.executable).resolve().parent
    for name in ("pyinstaller", "pyinstaller.exe"):
        cand = bindir / name
        if cand.is_file():
            return str(cand)
    return None


def main() -> int:
    if platform.system() != "Linux":
        raise SystemExit(
            "a Linux binary has to be built on Linux: it links against the "
            "glibc of the machine that builds it"
        )
    if not _find_pyinstaller():
        raise SystemExit("pyinstaller is not installed -- pip install pyinstaller")

    import lj
    log(f"lj      : {lj.__version__}")
    log(f"python  : {sys.version.split()[0]}  ({platform.platform()})")

    DIST.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "pyinstaller"
    if work.exists():
        shutil.rmtree(work)

    cmd = [
        _find_pyinstaller(),
        "--onefile",              # one file, not a directory of them
        "--name", "lj",
        "--distpath", str(DIST),
        "--workpath", str(work),
        "--specpath", str(work),
        "--noconfirm",
        "--clean",
        # tkinter is Windows-only here -- the window is not part of the
        # frozen binary -- and it pulls in a lot of X11 libraries, so
        # excluding it keeps the binary small.
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        str(ENTRY),
    ]
    log("")
    log("=== pyinstaller ===")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"pyinstaller failed ({r.returncode})")

    out = DIST / "lj"
    if not out.is_file():
        raise SystemExit(f"no binary at {out}")
    out.chmod(0o755)
    log("")
    log(f"=== {out}  ({out.stat().st_size:,} bytes) ===")
    described = subprocess.run(["file", str(out)], capture_output=True,
                               text=True).stdout.strip()
    log(f"  file: {described or '(no file command)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
