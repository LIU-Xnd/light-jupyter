"""lj.dialer -- the sending side, on the machine that cannot be reached.

No inbound port here, so it dials OUT to wherever lj_listen is holding the
port, and POSTs a message.

    python -m lj.dialer --target 192.168.77.160:10000 --token lj_...
    echo hello | python -m lj.dialer --target 192.168.77.160:10000 --token lj_...
    python -m lj.dialer --target ... --token ... -m "hello"
    python -m lj.dialer --target ... --token ... --repeat 5 --interval 4

SCOPE -- messages only, deliberately
--------------------------------------
Requests and responses are HTTP messages, never opaque byte streams. There
is no raw/stream mode here and one should not be added: see the scope note
in lj.listen. Remote shells belong to ssh, which already handles the PTY,
the flow control and the encryption.
"""

import argparse
import json
import sys
import threading
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
    print(f"    connected to {host}:{port}", flush=True)
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
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    host, port = split_target(args.target)

    if args.listen:
        return collect(host, port, args.token, args.timeout,
                       args.interval, forever=not args.once,
                       once=args.once)

    # REPL when asked for, or when there is nothing to send and a human is
    # there to type. Piped stdin or an explicit -m is a one-shot send, so
    # scripts do not suddenly enter an interactive session.
    have_msg = args.message is not None or args.file or not sys.stdin.isatty()
    if args.interactive or (not have_msg and sys.stdin.isatty()):
        return repl(host, port, args.token, args.timeout, args.poll)

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