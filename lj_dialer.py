"""lj_dialer -- the sending side, on the machine that cannot be reached.

No inbound port here, so it dials OUT to wherever lj_listen is holding the
port, and POSTs a message.

    python -m lj_dialer --target 192.168.77.160:10000 --token lj_...
    echo hello | python -m lj_dialer --target 192.168.77.160:10000 --token lj_...
    python -m lj_dialer --target ... --token ... -m "hello"
    python -m lj_dialer --target ... --token ... --repeat 5 --interval 4
"""

import argparse
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 10.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


def split_target(spec: str) -> tuple:
    """'host:port', bare 'host' (default port), or a bare port."""
    spec = spec.strip()
    if spec.isdigit():
        return "127.0.0.1", int(spec)
    host, sep, port = spec.rpartition(":")
    if not sep:
        return spec, 10000
    if not port:                  # "host:" -- port left off, take the default
        return (host or "127.0.0.1"), 10000
    if not port.isdigit():
        # an IPv6 literal, or junk: hand it back as a bare host and let the
        # connect fail with a clear socket error rather than a parse error
        return spec, 10000
    return (host or "127.0.0.1"), int(port)


def read_message(args) -> str:
    if args.message is not None:
        return args.message
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            return fh.read().rstrip("\n")
    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n")
    return ""


def send(host: str, port: int, token: str, msg: str,
         timeout: float) -> tuple:
    """POST one message. Returns (status, detail)."""
    url = f"http://{host}:{port}/msg"
    req = urllib.request.Request(
        url, data=msg.encode("utf-8"), method="POST",
        headers={"X-LJ-Token": token,
                 "Content-Type": "text/plain; charset=utf-8"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, f"{time.time() - t0:.2f}s"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()
        if e.code == 401:
            return e.code, "token rejected -- the two sides disagree"
        return e.code, body or str(e.reason)
    except urllib.error.URLError as e:
        return None, f"cannot reach {host}:{port} ({e.reason})"
    except OSError as e:
        return None, str(e)


def fetch(host: str, port: int, token: str, timeout: float) -> str:
    """Read the queue back, the way the browser would."""
    url = f"http://{host}:{port}/msg"
    req = urllib.request.Request(url, headers={"X-LJ-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"({type(e).__name__}: {e})"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lj_dialer",
        description="push a message to an lj_listen that is holding the "
                    "port; this side only ever dials out")
    p.add_argument("--target", required=True, metavar="IP[:PORT]",
                   help="where lj_listen is listening (default port 10000)")
    p.add_argument("--token", required=True,
                   help="the token lj_listen printed")
    p.add_argument("--message", "-m", default=None,
                   help="the text to send (default: stdin)")
    p.add_argument("--file", "-f", default=None,
                   help="send the contents of this file")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="send N times (default: 1)")
    p.add_argument("--interval", type=float, default=5.0, metavar="SEC",
                   help="seconds between repeats (default: 5)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help=f"per-request timeout (default: {DEFAULT_TIMEOUT:g}s)")
    p.add_argument("--show", action="store_true",
                   help="read the queue back after sending")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    host, port = split_target(args.target)
    msg = read_message(args)

    if not msg:
        log("nothing to send: pass -m, -f, or pipe text on stdin")
        return 2

    log("")
    log("  lj_dialer")
    log(f"    target : {host}:{port}")
    log(f"    sends  : {len(msg)} chars")
    log("")

    failures = 0
    for i in range(1, args.repeat + 1):
        stamp = time.strftime("%H:%M:%S")
        status, detail = send(host, port, args.token, msg, args.timeout)
        if status in (200, 204):
            log(f"  [{stamp}] {i}/{args.repeat} -> {status} in {detail}")
        else:
            failures += 1
            log(f"  [{stamp}] {i}/{args.repeat} -> FAILED: {detail}")
        if i < args.repeat:
            time.sleep(args.interval)

    if args.show:
        log("")
        log("  queue as the browser sees it:")
        log(f"    {fetch(host, port, args.token, args.timeout)}")

    log("")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())