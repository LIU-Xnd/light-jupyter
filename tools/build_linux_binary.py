"""Build a single-file Linux binary for the dialer.

The point is a machine that cannot be reached -- a server with no
Python, no conda, no pip -- still being able to send a message out.
So the binary has to carry its own interpreter.

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


def main() -> int:
    if platform.system() != "Linux":
        raise SystemExit(
            "a Linux binary has to be built on Linux: it links against the "
            "glibc of the machine that builds it"
        )
    if not shutil.which("pyinstaller"):
        raise SystemExit("pyinstaller is not installed -- pip install pyinstaller")

    import lj
    log(f"lj      : {lj.__version__}")
    log(f"python  : {sys.version.split()[0]}  ({platform.platform()})")

    DIST.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "pyinstaller"
    if work.exists():
        shutil.rmtree(work)

    cmd = [
        "pyinstaller",
        "--onefile",              # one file, not a directory of them
        "--name", "lj-dialer",
        "--distpath", str(DIST),
        "--workpath", str(work),
        "--specpath", str(work),
        "--noconfirm",
        "--clean",
        # tkinter is not used by the dialer and pulls in a lot of X11
        # libraries; excluding it keeps the binary small.
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

    out = DIST / "lj-dialer"
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
