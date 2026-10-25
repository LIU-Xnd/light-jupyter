"""Entry point for the frozen dialer.

PyInstaller needs a script, not a module path. This calls the dialer's
main() the same way the console script does, so the frozen binary
behaves exactly like `lj dialer`.

The listen side and the window are left out on purpose: the window is
Windows-only, and the dialer is the side that has to run on a machine
with nothing installed.
"""
import sys

from lj.dialer import main

if __name__ == "__main__":
    sys.exit(main())
