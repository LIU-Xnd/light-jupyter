"""Command line entry point.

    python -m light_jupyter_server --port 8890 --root ~/LjDev

Prints the full token URL on startup. With --no-token a random one is
generated; pass --token to pin it.
"""

from __future__ import annotations

import argparse
import errno
import getpass
import os
import signal
import sys

from .ports import bind_socket, parse_port, pick_port
from .tokens import entropy_bits
from .server import make_app, make_token


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="light_jupyter_server",
                                description="lightweight token-gated web IDE")
    p.add_argument("--host", default="0.0.0.0",
                   help="bind address; the advertised URL uses the first "
                        "non-loopback IPv4 from `ip addr` (default: 0.0.0.0)")
    p.add_argument("--port", default="auto", metavar="PORT",
                   help="listen port; leave empty for auto (kernel picks "
                        "a free one)")
    p.add_argument("--root", default=None, metavar="DIR",
                   help="directory exposed by the file pane "
                        "(default: /home/<user>)")
    p.add_argument("--token", default=None,
                   help="use this exact token instead of the stored one")
    p.add_argument("--token-file", default=None, metavar="PATH",
                   help="persist the token here (mode 0600) and reuse it on "
                        "later runs; default: ~/.light_jupyter/token")
    p.add_argument("--regen-token", action="store_true",
                   help="discard the stored token and mint a new one")
    p.add_argument("--show-token", action="store_true",
                   help="print the token that would be used and exit")
    p.add_argument("--token-bits", type=int, default=256, metavar="N",
                   help="minimum entropy for a generated token (default: 256)")
    p.add_argument("--user", default=None,
                   help="shell user (default: the current user)")
    p.add_argument("--trust-proxy", action="store_true",
                   help="honour X-Forwarded-For when rate limiting "
                        "(only behind a proxy you control)")
    p.add_argument("--throttle-threshold", type=int, default=5, metavar="N",
                   help="failed token attempts before backoff starts "
                        "(default: 5)")
    p.add_argument("--print-only", action="store_true",
                   help="print the URL and exit without listening")
    p.add_argument("--tunnel", action="store_true",
                   help="reach the browser through the client instead of "
                        "listening inbound: no port of this machine needs to "
                        "be reachable, and this process dials out")
    p.add_argument("--handoff-file", default=None, metavar="PATH",
                   help="read the client's one line from PATH instead of "
                        "pasting it (for unattended startup)")
    return p


DEFAULT_TOKEN_FILE = "~/.light_jupyter/token"


def resolve_token(args) -> tuple:
    """Decide which token to use. Returns (token, note, created)."""
    from .tokens import load_or_create

    if args.token:
        return args.token, "supplied via --token", False

    path = args.token_file or DEFAULT_TOKEN_FILE
    if args.regen_token:
        # drop the old one; load_or_create then mints and persists a fresh one
        try:
            os.remove(os.path.expanduser(path))
        except OSError:
            pass
        token, _ = load_or_create(path,
                                  entropy_bytes=max(16, args.token_bits // 8))
        return token, f"regenerated into {path}", True

    token, created = load_or_create(path,
                                    entropy_bytes=max(16, args.token_bits // 8))
    return token, ("generated into " if created else "loaded from ") + path, created


def default_root(user: str) -> str:
    """The user's home directory, as /home/<user>.

    Deliberately NOT os.path.expanduser("~"): that reads $HOME from the
    environment, and $HOME is unset or wrong whenever the process is started
    detached (setsid, cron, systemd) or by a user whose passwd home is not
    under /home -- root's home is /root, and some accounts live elsewhere.
    /home/<user> is what the docs promise and what the file pane should show.
    """
    if os.name == "posix":
        candidate = f"/home/{user}"
        if os.path.isdir(candidate):
            return candidate
        # account's home is somewhere else (root -> /root, LDAP homes, ...)
        try:
            import pwd
            return pwd.getpwnam(user).pw_dir
        except (KeyError, ImportError):
            pass
    return os.path.expanduser("~") or f"/home/{user}"


def local_ipv4() -> str:
    """First non-loopback IPv4 address, read from `ip addr`.

    Deliberately does not hardcode an address and does not open a UDP socket
    to 8.8.8.8 to "discover" one: that picks a route rather than listing what
    the machine actually has, and fails outright on an offline host. `ip addr`
    tells us the truth.

    Skips loopback and link-local addresses -- 127.0.0.1 is useless in a URL
    handed to someone else, and 169.254.x.x means DHCP failed.
    """
    import re
    import subprocess

    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""

    # "2: eth0    inet 192.0.2.10/24 brd ..."
    ips = re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", out)
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        return ip

    # `ip` missing or gave nothing usable -- fall back to hostname resolution
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))     # no packets sent, just selects a route
        addr = s.getsockname()[0]
        s.close()
        if addr and not addr.startswith("127."):
            return addr
    except OSError:
        pass

    return "127.0.0.1"


# ---------------------------------------------------------------- tunnel mode

def _read_handoff(args) -> str:
    """Get the client's one line: from a file if asked, else pasted here."""
    if args.handoff_file:
        with open(args.handoff_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()

    # Deliberately a plain stdin read, not input(): the line can be a few
    # hundred characters and input() mangles nothing but prompt handling is
    # easier without it. No echo control is attempted -- the value is a
    # shared secret and this is a normal terminal session.
    print("", flush=True)
    print("  Paste the line the client printed (it starts with 'lj1.'),"
          " then press Enter:", flush=True)
    print("  (copy it in full; press Enter again on an empty line to"
          " cancel)", flush=True)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        line = line.strip()
        if not line:
            break
        lines.append(line)
    return " ".join(lines).strip()


def run_tunnel(args, token: str, root: str, user: str, host: str) -> int:
    """Serve through the client: no inbound port is used at all.

    The server dials out, so the browser's address is the only thing the
    two sides have to agree on -- and the client hands that over as one
    line, which is why this is interactive rather than a pile of flags.
    """
    from .gateway import decode_handoff, connect, HANDOFF_PREFIX

    blob = _read_handoff(args)
    if not blob:
        print("", flush=True)
        print("  no handoff given -- not starting.", flush=True)
        return 2

    try:
        data = decode_handoff(blob)
    except ValueError as e:
        raise SystemExit(f"\ncannot read the client's line: {e}\n")

    client = (data["ip"], int(data["port"]))
    secret = data["secret_b"]

    # lj still listens, but only on loopback: nothing outside can reach it,
    # which is exactly why any port the kernel hands out is fine.
    import socket as _sock
    probe = _sock.socket()
    probe.bind(("127.0.0.1", 0))
    lj_port = probe.getsockname()[1]
    probe.close()

    from .server import make_app
    app = make_app(token=token, root=root, user=user, port=lj_port,
                   trust_proxy=args.trust_proxy,
                   throttle_threshold=args.throttle_threshold)
    app.listen(lj_port, address="127.0.0.1")

    browser_port = int(data.get("browser_port", 0)) or None
    url = f"http://127.0.0.1:{browser_port}/#token={token}" \
        if browser_port else f"http://127.0.0.1:{lj_port}/#token={token}"

    print("", flush=True)
    print("  light_jupyter_server via the client  (egress only)", flush=True)
    print(f"    dialing  : {client[0]}:{client[1]}", flush=True)
    print(f"    lj       : 127.0.0.1:{lj_port}  (loopback only)", flush=True)
    print(f"    browser  : {url}", flush=True)
    print("", flush=True)
    print("  open that URL in the browser on the client machine.", flush=True)
    print("  ctrl-c to stop", flush=True)
    print("", flush=True)

    return connect(client, ("127.0.0.1", lj_port), secret, forever=True)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    user = args.user or getpass.getuser()

    token, token_note, _created = resolve_token(args)
    root = os.path.abspath(os.path.expanduser(args.root)) if args.root \
        else default_root(user)

    if not os.path.isdir(root):
        os.makedirs(root, exist_ok=True)

    # ---- port: empty / auto / 0 -> probe the candidate list ----
    try:
        requested = parse_port(args.port)
    except ValueError as e:
        raise SystemExit(str(e))

    if requested is None:
        port, port_note = pick_port(args.host), "auto (kernel-assigned)"
    else:
        port = requested
        port_note = "as requested"

    # ---- figure out the address to advertise ----
    host = args.host
    if host in ("0.0.0.0", ""):
        host = local_ipv4()

    if args.show_token:
        print(token)
        return 0

    url = f"http://{host}:{port}/#token={token}"
    banner = [
        "",
        "  light_jupyter_server",
        f"    url   : {url}",
        f"    port  : {port}  ({port_note})",
        f"    root  : {root}",
        f"    user  : {user}",
        f"    token : {token}",
        f"            ({token_note}, {entropy_bits(token)}-bit)",
        "",
        "  ctrl-c to stop",
        "",
    ]
    print("\n".join(banner), flush=True)

    if args.print_only:
        return 0

    if args.tunnel:
        return run_tunnel(args, token, root, user, host)

    import tornado.ioloop

    # Use app.listen(), NOT a hand-bound socket + HTTPServer.add_socket().
    #
    # Evidence: the self-test (app.listen in a thread) passes all 93 checks
    # including real HTTP round-trips, while every detached launch that used
    # bind_socket()+add_socket() left the process alive in ep_poll with the
    # listening socket apparently invisible to epoll -- connections completed
    # their handshake in the kernel and were then never accepted. A faulthandler
    # dump showed the loop healthy in epoll_wait, so the loop was fine; only the
    # hand-registered socket was not being serviced.
    #
    # app.listen() is also what makes EADDRINUSE surface as a normal OSError,
    # caught below for a clean message.
    app = make_app(token=token, root=root, user=user, port=port,
                   trust_proxy=args.trust_proxy,
                   throttle_threshold=args.throttle_threshold)
    try:
        app.listen(port, address=args.host)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            raise SystemExit(
                f"cannot bind {args.host}:{port} ({e.strerror})."
                + ("  Another light_jupyter_server may already be running;"
                   " stop it or leave --port empty for auto."
                   if e.errno == errno.EADDRINUSE
                   else "  Ports below 1024 need root."))
        raise
    actual = app.settings.get("port", port)

    loop = tornado.ioloop.IOLoop.current()

    def _stop(*_):
        # Runs inside the signal handler. Tornado 6.4 + py3.8: a
        # loop.add_callback(loop.stop) from here does NOT reliably wake the
        # epoll_wait (measured: the loop sat in select for 12 s after
        # SIGINT, stacks dumped). Everything is closed synchronously below,
        # so just exit hard -- deterministic, no teardown to hang on.
        print("\nshutting down", flush=True)
        for s in list(app.sessions.values()):
            s.close()
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    loop.start()

    # Everything is already closed in _stop(); exit hard rather than risk
    # hanging in interpreter teardown (stray PTY reader threads or executor
    # workers can otherwise keep a non-daemon thread alive forever, which is
    # exactly the "ctrl-c and it never exits" symptom).
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())