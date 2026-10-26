"""lj.dialer -- the sending side, on the machine that cannot be reached.

No inbound port here, so it dials OUT to wherever lj listen is holding
the port.

TWO MODES, ONE COMMAND
----------------------
The far side does two jobs, and they were two commands. They are one
now, because they are one thing: the far side talks to the listener.

    # as the WORKER: poll for work, do it, bring the answers back.
    # This is the daemon the browser drives -- it lists, reads, edits,
    # moves, copies and deletes files there, and runs commands there.
    lj dialer --worker --root /srv/project 192.168.77.160:10000/#token=lj_...

    # as a MESSENGER: push a message, or collect what the page sends
    lj dialer 192.168.77.160:10000/#token=lj_... -m "hello"
    lj dialer 192.168.77.160:10000/#token=lj_... --listen

The mode is chosen by the flags, not by a second command name: --worker
for the daemon, and the message flags (-m, --listen, --show) for the
messenger. With neither, it is the messenger -- so the old one-line
`lj dialer ... -m hello` still works, and there is nothing to learn.

WHY ONE COMMAND AND NOT TWO
----------------------------
They had the same arguments, the same target format, the same token
handling, and the same "dial out, never accept" shape. Two commands
meant two parsers, two sets of flags, two help texts, and a user who
had to know which half of the far side they were running. The mode is
a flag now, and the flag is the thing that was already different.

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
import threading
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 10.0


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


def _timeout(args) -> float:
    """The per-request timeout, with the default for the mode.

    The message mode and the worker mode had different defaults (10s
    and 15s) and the flag is now shared, so the default is picked by
    the mode rather than by the parser.
    """
    if args.timeout is not None:
        return args.timeout
    return 15.0 if (args.worker or args.root is not None) else DEFAULT_TIMEOUT


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
    # /in is the far-side-inbound endpoint; the browser's own sends use /msg
    url = f"http://{host}:{port}/in"
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
    url = f"http://{host}:{port}/send"
    req = urllib.request.Request(url, headers={"X-LJ-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"({type(e).__name__}: {e})"


def collect(host: str, port: int, token: str, timeout: float,
            interval: float, forever: bool, once: bool) -> int:
    """Poll for what the browser sends back.

    Polling rather than a held connection, on purpose: every request stands
    alone, so a timeout costs one message instead of a connection that has
    to be detected and rebuilt. NAT tables expire silently and give no FIN,
    which is what makes a held socket quietly wedge.
    """
    url = f"http://{host}:{port}/send"
    log("")
    log(f"  listening for the browser on {url}")
    log(f"    poll every {interval:g}s")
    log("")
    seen = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"X-LJ-Token": token})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for m in data.get("msgs", []):
                seen += 1
                log(f"  [{time.strftime('%H:%M:%S')}] browser: {m}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log("  token rejected -- the two sides disagree")
            else:
                log(f"  {e.code}: {e.read().decode('utf-8', 'replace')[:80]}")
            if once:
                return 1
        except Exception as e:
            log(f"  {type(e).__name__}: {e}")
            if once:
                return 1
        if once:
            return 0
        if not forever:
            return 0
        time.sleep(interval)




# ---------------------------------------------------------------- REPL

HELP = """\
type a message and press enter to send it to the page
  /help      this text
  /send T    send T without putting it on its own line
  /quit      leave (ctrl-d works too)

messages arriving from the page appear above with their timestamps.
"""


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def _emit(prefix: str, text: str) -> None:
    """One log line, always starting on a fresh row.

    Incoming messages arrive on a background thread, possibly while the
    operator is halfway through typing a prompt, so the line begins with a
    newline: otherwise the log text lands in the middle of the prompt and
    the next prompt reuses the same row. The trailing newline then leaves
    the cursor somewhere clean to type.
    """
    print(f"\n  [{_stamp()}] {prefix} {text}", flush=True)


def repl(host: str, port: int, token: str, timeout: float,
         interval: float) -> int:
    """Interactive two-way session.

    A background thread polls /send for what the browser sends while
    this thread reads what the operator types and POSTs it. Polling
    rather than a held socket, as everywhere else in lj: every request
    stands alone, so a timeout costs one message instead of a
    connection that has to be detected and rebuilt (NAT tables expire
    silently and send no FIN).
    """
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            try:
                req = urllib.request.Request(
                    f"http://{host}:{port}/send",
                    headers={"X-LJ-Token": token})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                for m in data.get("msgs", []):
                    _emit("<--", m)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    _emit("!!", "token rejected -- the two sides disagree")
                    stop.set()
                    return
            except Exception:
                pass               # transient; the next tick retries
            stop.wait(interval)

    print("", flush=True)
    print("  lj dialer -- interactive", flush=True)
    print(f"    connected to http://{host}:{port}", flush=True)
    print("    type a message and press enter; /help for commands",
          flush=True)
    print("    inbound messages are shown with their time; /quit to leave",
          flush=True)
    print("", flush=True)

    th = threading.Thread(target=poller, daemon=True)
    th.start()

    try:
        while True:
            try:
                line = input("you > ")
            except (EOFError, KeyboardInterrupt):
                print("", flush=True)
                break

            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit", "/q"):
                break
            if line in ("/help", "/h", "/?"):
                print("\n" + HELP, end="", flush=True)
                continue
            if line.startswith("/send "):
                line = line[len("/send "):].strip()
                if not line:
                    continue

            status, detail = send(host, port, token, line, timeout)
            if status in (200, 204):
                _emit("-->", line)
            else:
                _emit("!!", f"send failed: {detail}")
    finally:
        stop.set()

    print("", flush=True)
    print("  disconnected", flush=True)
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lj dialer",
        description="the far side: push a message to the lj listen that "
                    "is holding the port, or --worker, do whatever the "
                    "browser asks for. This side only ever dials out")
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
    p.add_argument("--message", "-m", default=None,
                   help="the text to send (default: stdin)")
    p.add_argument("--file", "-f", default=None,
                   help="send the contents of this file")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="send N times (default: 1)")
    p.add_argument("--interval", type=float, default=None, metavar="SEC",
                   help="seconds between repeats (message mode, default: 5) "
                        "or between polls (worker mode, default: 1)")
    p.add_argument("--timeout", type=float, default=None,
                   help="per-request timeout (message mode, default: "
                        f"{DEFAULT_TIMEOUT:g}s; worker mode, default: 15s)")
    p.add_argument("--show", action="store_true",
                   help="read the queue back after sending")
    p.add_argument("--listen", action="store_true",
                   help="collect what the browser sends back instead of "
                        "sending (this is the two-way mode)")
    p.add_argument("--once", action="store_true",
                   help="with --listen, poll once and exit")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="two-way REPL: type to send, and messages from the "
                        "page appear live with timestamps (default when "
                        "stdin is a terminal and no message was given)")
    p.add_argument("--poll", type=float, default=1.0, metavar="SEC",
                   help="how often the REPL checks for new messages "
                        "(default: 1)")

    # -- the worker mode: the far side as a daemon ------------------
    #
    # These are the flags lj remote had. They are here now, in the
    # one command, because the two halves of the far side had the same
    # arguments and the same target format and differed only in what
    # they did with the connection.
    p.add_argument("--worker", action="store_true",
                   help="be the far side's worker: poll for work, do it "
                        "(browse, read, edit, move, copy, delete files; "
                        "run commands) and bring the answers back. This "
                        "is the daemon the browser drives")
    p.add_argument("--root", default=None, metavar="DIR",
                   help="the tree the browser's paths are relative to; "
                        "implies --worker (default: the home directory)")
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

    # --root implies --worker: naming the tree the browser may touch is
    # only meaningful in the worker mode, and making it imply the mode
    # means the old "lj remote --root ..." reads naturally here too.
    worker = args.worker or args.root is not None

    if worker:
        return _run_worker(args, host, port, token)

    if args.print_only:
        log(f"lj dialer  target http://{host}:{port}  (message mode)")
        return 0

    if args.listen:
        return collect(host, port, token, _timeout(args),
                       args.interval if args.interval is not None else 5.0,
                       forever=not args.once,
                       once=args.once)

    # REPL when asked for, or when there is nothing to send and a human is
    # there to type. Piped stdin or an explicit -m is a one-shot send, so
    # scripts do not suddenly enter an interactive session.
    have_msg = args.message is not None or args.file or not sys.stdin.isatty()
    if args.interactive or (not have_msg and sys.stdin.isatty()):
        return repl(host, port, token, _timeout(args), args.poll)

    msg = read_message(args)
    if not msg:
        log("nothing to send: pass -m, -f, or pipe text on stdin")
        return 2

    log("")
    log("  lj_dialer")
    log(f"    target : http://{host}:{port}")
    log(f"    sends  : {len(msg)} chars")
    log("")

    failures = 0
    _gap = args.interval if args.interval is not None else 5.0
    for i in range(1, args.repeat + 1):
        stamp = time.strftime("%H:%M:%S")
        status, detail = send(host, port, token, msg, _timeout(args))
        if status in (200, 204):
            log(f"  [{stamp}] {i}/{args.repeat} -> {status} in {detail}")
        else:
            failures += 1
            log(f"  [{stamp}] {i}/{args.repeat} -> FAILED: {detail}")
        if i < args.repeat:
            time.sleep(_gap)

    if args.show:
        log("")
        log("  queue as the browser sees it:")
        log(f"    {fetch(host, port, token, _timeout(args))}")

    log("")
    return 1 if failures else 0


# ------------------------------------------------------- the worker mode


def _run_worker(args, host: str, port: int, token: str) -> int:
    """Be the far side's worker: poll for work, do it, bring it back.

    This is what lj remote used to be. It is here because the two
    halves of the far side had the same arguments and the same target
    format, and the only thing that was really different was what they
    did with the connection.
    """
    from . import agent as A

    root = os.path.abspath(os.path.expanduser(args.root or "~"))
    if args.interval is None:
        args.interval = 1.0
    if args.timeout is None:
        args.timeout = 15.0

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

    if args.print_only:
        log(f"lj dialer  target http://{host}:{port}  root {root}  (worker)")
        return 0

    log("")
    log("  lj dialer -- the far side's worker")
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