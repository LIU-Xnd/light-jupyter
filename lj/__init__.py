"""lj -- a minimal, egress-only message channel.

One side holds the port, the other dials out to it. No inbound port is needed
on the machine that cannot be reached.

    lj listen                      # holds :10000, mints a token
    lj dialer --target IP:PORT --token TOKEN -m 'hello'

Both subcommands are also runnable as modules, which is useful from a clone
with nothing installed:

    python -m lj.listen --help
    python -m lj.dialer --help

SCOPE -- messages only, deliberately
-------------------------------------
This is a message queue with a web page on it. It is NOT a transport for
arbitrary byte streams, and it must not grow into one.

Carrying a shell over it would mean re-implementing what ssh already does
correctly: PTY allocation, windowing and flow control, reconnection
semantics, and end-to-end encryption. Worse, forwarding raw bytes hands the
holder of the token a remote command channel, so the token stops being a
message-passing credential and becomes a shell credential. That is a much
larger blast radius than the one it replaces.

Where a shell on a server with no inbound ports is needed, use ssh's own
mechanism: an sshd on a reachable port here, another sshd on the server
bound to loopback, and `ssh -N -R <port>:localhost:<sshd>` dialled out from
the server. Real SSH, real encryption, and nothing to maintain here.

Standard library only. `lj.selftest` asserts this scope.
"""

import sys

try:
    from importlib.metadata import version as _version
    __version__ = _version("lj")
except Exception:
    __version__ = "2.7.1"       # fallback when not installed as a distribution

__all__ = ["__version__", "main"]

USAGE = """\
lj {version} -- an egress-only message channel

usage: lj <command> [options]

commands:
  listen     hold the port the far side dials; serves the page
  dialer     push a message out to that port, or collect what the page sends

run 'lj <command> --help' for a command's own options.

A session is two processes, one on each machine:

  machine with the browser          machine that cannot be reached
    lj listen                        lj dialer --target <that-ip>:<port> \\
                                       --token <token> -m 'hello'

Both are standard library only and need no dependencies.
"""


def main(argv=None) -> int:
    """Dispatch `lj <command> ...` to the subcommand's own main()."""
    argv = list(sys.argv[1:] if argv is None else argv)

    # `lj`, `lj -h` and `lj --help` all print the top-level usage.
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.format(version=__version__), end="")
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0

    command, rest = argv[0], argv[1:]

    # imported lazily so `lj --help` does not pay for the subcommand modules
    if command == "listen":
        from .listen import main as run
    elif command == "dialer":
        from .dialer import main as run
    else:
        print(f"lj: unknown command {command!r}\n", file=sys.stderr)
        print(USAGE.format(version=__version__), end="", file=sys.stderr)
        return 2

    return run(rest)


if __name__ == "__main__":
    sys.exit(main())
