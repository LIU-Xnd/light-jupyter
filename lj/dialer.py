"""lj.dialer -- the far side, on the machine that cannot be reached.

No inbound port here, so it dials OUT to wherever lj listen is holding
the port.

WHAT IT DOES
------------
It polls for work, does it, and brings the answers back. The browser
can list, read, edit, move, copy and delete files there, and run
commands there.

    lj dialer <the reachable machine>:10000/#token=lj_...
    lj dialer --root /srv/project <the reachable machine>:10000/#token=lj_...

IT IS NOT A MESSENGER
---------------------
There is no message queue, no -m, no REPL, and no chat drawer in the
page. Those were the other half of the far side, and they are gone:
one command that does one thing is easier to reason about than one
command that does two things depending on a flag.

The endpoints that carried messages (/msg, /in, /send) are gone too,
and the self-test asserts they 404 -- so they cannot come back
quietly.

WHY POLLING AND NOT A HELD CONNECTION
--------------------------------------
The far side cannot be reached: it has no inbound port. So it dials
OUT and polls. A held connection would be nicer, but NAT tables
expire silently and send no FIN, which turns a dropped connection
into a hang that nothing detects. Every request here stands alone: a
timeout costs one operation, not a session.

SCOPE
-----
Requests and responses are HTTP messages, never opaque byte streams.
There is no raw/stream mode here and one should not be added: see the
scope note in lj.listen.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 15.0
DEFAULT_INTERVAL = 1.0


def log(msg: str = "") -> None:
    print(msg, flush=True)


def split_target(spec: str) -> tuple:
    """Parse a target into (host, port, token).

    The target can be:

        host:port
        http://host:port
        http://host:port/#token=lj_xxx

    The token is the third value, or None when the target has no
    fragment. It is there because the listener's banner prints ONE
    line -- the browser URL -- and the fragment of that line already
    carries the token. So a line copied out of the banner can be
    handed straight to the far side, and there is nothing to mistype.

    The traffic is plain HTTP: there is no encryption here, so a target
    is an address and a port. A 'https://' prefix is accepted and
    ignored, so an old target string still works instead of failing to
    parse.
    """
    spec = spec.strip()

    # The fragment. The browser URL puts the token there, and the
    # browser never sends a fragment to the server, so it is the one
    # place a token can ride without being in a log or a Referer.
    token = None
    if "#" in spec:
        spec, _, frag = spec.partition("#")
        for part in frag.split("&"):
            name, _, value = part.partition("=")
            if name.strip() == "token" and value.strip():
                token = value.strip()
                break

    for _s in ("https://", "http://"):
        if spec.lower().startswith(_s):
            spec = spec[len(_s):]
            break

    # A path is not part of a target -- lj's API has no paths -- so
    # "http://host:10000/" is the same target as "http://host:10000".
    # Without this the trailing slash lands in the port and the target
    # does not parse.
    spec = spec.split("/", 1)[0]

    if spec.isdigit():
        return "127.0.0.1", int(spec), token
    host, sep, port = spec.rpartition(":")
    if not sep:
        return spec, 10000, token
    if not port:                  # "host:" -- port left off, take the default
        return (host or "127.0.0.1"), 10000, token
    if not port.isdigit():
        # an IPv6 literal, or junk: hand it back as a bare host and let the
        # connect fail with a clear socket error rather than a parse error
        return spec, 10000, token
    return (host or "127.0.0.1"), int(port), token


def pick_target(args) -> str:
    """The target from the positional or from --target, or "" if neither.

    Both spellings are accepted: the option is what the older scripts
    use, and the positional is what reads better on a command line.
    """
    pos = getattr(args, "target_pos", None)
    opt = getattr(args, "target", None)
    if pos and opt and pos != opt:
        raise ValueError(
            f"two different targets given: --target {opt!r} and {pos!r}")
    return pos or opt or ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lj dialer",
        description="the far side: poll for work, do it (browse, read, "
                    "edit, move, copy, delete files; run commands) and "
                    "bring the answers back. This side only ever dials out")
    p.add_argument("target_pos", nargs="?", metavar="TARGET",
                   help="where lj listen is listening; the same thing as "
                        "--target")
    p.add_argument("--target", default=None, metavar="IP[:PORT]",
                   help="where lj listen is listening (default port 10000). "
                        "May carry the token as a fragment: "
                        "http://HOST:PORT/#token=lj_...")
    p.add_argument("--token", default=None,
                   help="the token lj listen printed; not needed when the "
                        "target carries it")
    p.add_argument("--root", default=None, metavar="DIR",
                   help="the tree the browser's paths are relative to "
                        "(default: the home directory)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   metavar="SEC",
                   help=f"seconds between polls (default: {DEFAULT_INTERVAL:g})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   metavar="SEC",
                   help=f"per-request timeout (default: {DEFAULT_TIMEOUT:g})")
    p.add_argument("--once", action="store_true",
                   help="poll once, do the work, and exit")
    p.add_argument("--print-only", action="store_true",
                   help="print what this would do and exit")
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

    # The token comes from the flag, or from the target's fragment. The
    # fragment is there because the listener's banner prints the browser
    # URL, and the token is in that URL's fragment -- so a line copied
    # out of the banner needs nothing added to it.
    token = args.token or frag_token
    if not token:
        log("no token: pass --token, or put it in the target as "
            "http://HOST:PORT/#token=lj_...")
        return 2

    root = os.path.abspath(os.path.expanduser(args.root or "~"))

    if args.print_only:
        log(f"lj dialer  target http://{host}:{port}  root {root}")
        return 0

    return _run_worker(args, host, port, token, root)


# ------------------------------------------------------- the worker


def _run_worker(args, host: str, port: int, token: str, root: str) -> int:
    """Poll for work, do it, bring it back."""
    from . import agent as A

    def _post(path: str, payload: dict):
        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"X-LJ-Token": token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                body = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(body)
                except ValueError:
                    return r.status, {}
        except urllib.error.HTTPError as e:
            return e.code, {}
        except Exception:
            return None, {}

    def _get(path: str):
        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(url, headers={"X-LJ-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                body = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(body)
                except ValueError:
                    return r.status, {}
        except urllib.error.HTTPError as e:
            return e.code, {}
        except Exception:
            return None, {}

    def poll_once() -> list:
        """Take whatever commands are waiting, run them, post the results.

        One poll, one pass -- the caller decides whether to do it again.
        """
        status, data = _get("/cmd")
        if status is None:
            raise ConnectionError(f"cannot reach {host}:{port}")
        if status == 401:
            raise PermissionError(
                "token rejected -- the two sides disagree")
        if status != 200:
            raise ConnectionError(f"the listener answered {status}")

        commands = data.get("cmds") or []
        results = []
        for cmd in commands:
            result = A.run_op(cmd.get("op", ""), cmd.get("args") or {}, root)
            result["seq"] = cmd.get("seq")
            result["op"] = cmd.get("op", "")
            results.append(result)
            _post("/res", result)
        return results

    log("")
    log("  lj dialer -- the far side")
    log(f"    listening to http://{host}:{port}")
    log(f"    root       : {root}")
    log(f"    poll every {args.interval:g}s")
    log("")
    log("  the browser can now list, read, edit and delete files there,")
    log("  and run commands.  ctrl-c to stop.")
    log("")

    while True:
        try:
            for r in poll_once():
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

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
