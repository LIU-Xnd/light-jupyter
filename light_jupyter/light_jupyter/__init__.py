"""light_jupyter - a lightweight token-gated web IDE over SSH + SFTP.

Runs on a remote Linux box, exposes a browser UI with:
  - an interactive terminal (real PTY, driven over paramiko SSH)
  - an SFTP file browser (upload / download / mkdir / rename / delete)
  - a small editor pane

Access is gated by a token: http://host:port/#token=<TOKEN>
"""

try:
    from importlib.metadata import version as _version
    __version__ = _version("light-jupyter")
except Exception:
    __version__ = "0.4.3"       # fallback when not installed as a distribution
__all__ = ["__version__"]