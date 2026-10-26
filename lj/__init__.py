"""lj -- a minimal, egress-only message channel.

One side holds the port, the other dials out to it. No inbound port is needed
on the machine that cannot be reached.

    lj listen                      # holds :10000, mints a token
    lj remote --target IP:PORT --token TOKEN   # the far side's worker
    lj dialer --target IP:PORT --token TOKEN -m 'hello'

Both subcommands are also runnable as modules, which is useful from a clone
with nothing installed:

    python -m lj.listen --help
    python -m lj.dialer --help

SCOPE -- a file manager and a command runner, deliberately not a terminal
---------------------------------------------------------------------------
This is a request/response channel with a web page on it: the browser asks
the far side to do something, and the whole answer comes back. It is NOT a
transport for arbitrary byte streams, and it must not grow into one.

There is no PTY here, and there is not going to be one. A real interactive
terminal needs a PTY, a resize channel, a signal channel and flow control --
and a browser that emulates one properly is xterm.js, which is a large
dependency to carry for a tool whose point is being small. Every operation
here is a request that comes back whole, so a timeout costs one operation
rather than a session that has to be detected and rebuilt.

The traffic is plain HTTP. There is no TLS, and the token in the URL is
what stands between a stranger and that machine -- so run this on a
network you control.

Standard library only. `lj.selftest` asserts this scope.
"""

import sys

try:
    from importlib.metadata import version as _version
    __version__ = _version("lj")
except Exception:
    __version__ = "3.0.1"       # fallback when not installed as a distribution

__all__ = ["__version__", "main"]

USAGE = """\
lj {version} -- an egress-only message channel

usage: lj <command> [options]

commands:
  listen     hold the port the far side dials; serves the page
  remote     the far side's worker: files and commands, on the far machine
  dialer     push a message out to that port, or collect what the page sends
  gui        the listener in a window, with buttons

run 'lj <command> --help' for a command's own options.

A session is two processes, one on each machine:

  machine with the browser          machine that cannot be reached
    lj listen                        lj remote --target <that-ip>:<port> \\
                                       --token <token>
    (open the URL it prints)         (the browser can now browse its files
                                     and run commands there)

All standard library only, no dependencies.
"""


def _has_console() -> bool:
    """Is there a console to print to?

    The Windows launcher is built on pythonw.exe, which has no console.
    Output written there goes nowhere: the process appears to do
    nothing at all. That matters most for the Start Menu shortcut,
    which runs the launcher with no arguments.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # GetConsoleProcessList fills the buffer with the pids attached
        # to this process's console and returns how many there were.
        # Zero means there is no console -- which is the case for the
        # Windows launcher, because it runs on pythonw.exe.
        #
        # AttachConsole is the wrong signal here and is tempting to
        # reach for: it succeeds precisely when the process has NO
        # console of its own and can attach to the parent's.
        buffer = (ctypes.c_ulong * 8)()
        count = kernel32.GetConsoleProcessList(buffer, len(buffer))
        return count > 0
    except Exception:
        # If the check itself fails, assume there is a console: printing
        # is the safer default on the platforms where output matters.
        return True


def _report_crash() -> None:
    """Show a traceback where the user can actually see it.

    With a console it goes to stderr. Without one -- the Windows
    launcher runs on pythonw.exe -- stderr is nowhere, so a message box
    is the only thing that will show the user what went wrong.
    """
    import traceback

    text = "".join(traceback.format_exception(*sys.exc_info()))
    if _has_console():
        print(text, file=sys.stderr)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, text, "lj -- something went wrong", 0x10)
    except Exception:
        pass


def main(argv=None) -> int:
    """Dispatch `lj <command> ...` to the subcommand's own main()."""
    argv = list(sys.argv[1:] if argv is None else argv)

    # `lj`, `lj -h` and `lj --help` all print the top-level usage.
    if not argv or argv[0] in ("-h", "--help", "help"):
        # With no console there is nowhere to print to, so the user
        # would see nothing happen. Open the window instead: that is
        # what the Start Menu shortcut runs.
        if not argv and not _has_console():
            from .gui import main as run
            return run([])
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
    elif command == "remote":
        from .remote import main as run
    elif command == "gui":
        from .gui import main as run
    else:
        print(f"lj: unknown command {command!r}\n", file=sys.stderr)
        print(USAGE.format(version=__version__), end="", file=sys.stderr)
        return 2

    try:
        return run(rest)
    except Exception:
        # A failure here would otherwise be invisible: the Windows
        # launcher is pythonw-based, so a traceback printed to stderr
        # goes nowhere and the process just disappears. Show it.
        _report_crash()
        raise


if __name__ == "__main__":
    sys.exit(main())
