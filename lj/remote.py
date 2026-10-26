"""lj.remote -- the far side's half of the file and terminal channel.

The far side cannot be reached, so it dials OUT to the listener and
polls for work. When the browser asks for something, this carries it
out and posts the answer back.

    python -m lj.remote --target 192.168.77.160:10000 --token lj_...

It is a daemon: it polls forever, running whatever the browser asks
for. `--once` runs a single poll and exits, which is what the tests
use.

WHAT IT CAN DO
--------------
Anything the account running it can do: read, write, move, copy and
delete files, and run commands. The browser is the operator.

SECURITY
--------
The token is now a shell credential for the far side, not a messaging
one. Anyone holding it can run commands as the user running this
process. Use HTTPS, or a network you control.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import agent as A
from .dialer import pick_target, split_target

DEFAULT_INTERVAL = 1.0
DEFAULT_TIMEOUT = 15.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _post(host: str, port: int, token: str, path: str, payload: dict,
          timeout: float = DEFAULT_TIMEOUT):
    """POST JSON and return the status and the parsed body."""
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"X-LJ-Token": token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return None, {}


def _get(host: str, port: int, token: str, path: str,
         timeout: float = DEFAULT_TIMEOUT):
    """GET JSON and return the status and the parsed body."""
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, headers={"X-LJ-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return None, {}


def poll_once(host: str, port: int, token: str, root: str) -> list:
    """Take whatever commands are waiting, run them, post the results.

    Returns the list of results it produced. One poll, one pass -- the
    caller decides whether to do it again.
    """
    status, data = _get(host, port, token, "/cmd")
    if status is None:
        raise ConnectionError(f"cannot reach {host}:{port}")
    if status == 401:
        raise PermissionError("token rejected -- the two sides disagree")
    if status != 200:
        raise ConnectionError(f"the listener answered {status}")

    commands = data.get("cmds") or []
    results = []
    for cmd in commands:
        seq = cmd.get("seq")
        op = cmd.get("op", "")
        args = cmd.get("args") or {}
        result = A.run_op(op, args, root)
        result["seq"] = seq
        result["op"] = op
        results.append(result)
        _post(host, port, token, "/res", result)
    return results


def run(host: str, port: int, token: str, root: str, interval: float,
        once: bool = False, forever: bool = True) -> int:
    """Poll for work until told to stop."""
    log("")
    log("  lj remote -- the far side's worker")
    log(f"    listening to http://{host}:{port}")
    log(f"    root       : {root}")
    log(f"    poll every {interval:g}s")
    log("")
    log("  the browser can now list, read, edit and delete files there,")
    log("  and run commands.  ctrl-c to stop.")
    log("")

    seen = 0
    while True:
        try:
            results = poll_once(host, port, token, root)
            for r in results:
                seen += 1
                if r.get("ok"):
                    extra = ""
                    if r.get("op") == "exec":
                        extra = (f"  (exit {r.get('code')}, "
                                 f"{r.get('bytes', 0)} bytes, "
                                 f"{r.get('secs', 0)}s)")
                    log(f"  [{time.strftime('%H:%M:%S')}] "
                        f"{r.get('op')} ok{extra}")
                else:
                    log(f"  [{time.strftime('%H:%M:%S')}] "
                        f"{r.get('op')} FAILED: {r.get('error')}")
        except PermissionError:
            log("  token rejected -- the two sides disagree")
            return 1
        except ConnectionError as e:
            log(f"  {e}")
        except KeyboardInterrupt:
            log("")
            log("  stopped")
            return 0

        if once:
            return 0
        if not forever:
            return 0
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lj remote",
        description="the far side's worker: carry out what the browser asks")
    p.add_argument("target_pos", nargs="?", metavar="TARGET",
                   help="where lj listen is holding the port; the same "
                        "thing as --target")
    p.add_argument("--target", default=None, metavar="IP[:PORT]",
                   help="where lj listen is holding the port. May carry the "
                        "token as a fragment: http://HOST:PORT/#token=lj_...")
    p.add_argument("--token", default=None,
                   help="the token lj listen printed; not needed when the "
                        "target carries it")
    p.add_argument("--root", default="~", metavar="DIR",
                   help="the directory the browser's paths are relative to "
                        "(default: the home directory)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help=f"seconds between polls (default: {DEFAULT_INTERVAL:g})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help=f"seconds to wait for one request "
                        f"(default: {DEFAULT_TIMEOUT:g})")
    p.add_argument("--once", action="store_true",
                   help="poll once and exit")
    p.add_argument("--print-only", action="store_true",
                   help="print the banner and exit without polling")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        target = pick_target(args)
    except ValueError as e:
        log(str(e))
        return 2

    if not target:
        # No target at all: print the usage rather than a bare error.
        # "the following arguments are required: --target" does not say
        # what to type; the usage does.
        build_parser().print_help()
        return 2

    host, port, frag_token = split_target(target)
    root = os.path.abspath(os.path.expanduser(args.root))

    # The token comes from the flag, or from the target's fragment. The
    # fragment is there because the listener's banner prints the browser
    # URL, and the token is in that URL's fragment -- so a line copied
    # out of the banner needs nothing added to it.
    token = args.token or frag_token
    if not token:
        log("no token: pass --token, or put it in the target as "
            "http://HOST:PORT/#token=lj_...")
        return 2

    if args.print_only:
        log(f"lj remote  target http://{host}:{port}  root {root}")
        return 0

    return run(host, port, token, root, args.interval,
               once=args.once, forever=not args.once)


if __name__ == "__main__":
    sys.exit(main())
