"""Entry point for ``python -m light_jupyter_client``."""

import sys

from .client import main

if __name__ == "__main__":
    sys.exit(main())