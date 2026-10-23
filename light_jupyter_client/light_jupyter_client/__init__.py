"""light_jupyter_client — your side of an egress-only reverse tunnel.

The light_jupyter server has no inbound port open (only outbound), so it
cannot be reached by a browser directly. This package runs on the machine
with the browser and provides the missing path:

* it listens on a port the server dials, authenticates that dial-in with an
  HMAC handshake, and carries every browser connection over it;
* the browser talks to ``127.0.0.1:<browser-port>`` -- nothing is exposed to
  the network.

Run:

    python -m light_jupyter_client

It prints the exact command to run on the server.
"""

try:
    from importlib.metadata import version as _version
    __version__ = _version("light_jupyter")
except Exception:
    __version__ = "1.2.0"       # fallback when not installed as a distribution
__all__ = ["__version__"]