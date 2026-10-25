"""Entry point for the frozen Linux binary.

PyInstaller needs a script, not a module path. This calls the same
dispatcher the console script does, so the frozen binary behaves
exactly like `lj`.

The worker is the reason this exists. The machine that cannot be
reached is the one that has nothing installed -- no Python, no conda,
no pip -- and it is the side that has to dial out. So the binary that
matters is the one that runs the far side's worker, and it has to
carry its own interpreter.

The dialer is in there too: the message queue is a separate half and
a box that can only send messages is still useful on its own.
"""
import sys

from lj import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
