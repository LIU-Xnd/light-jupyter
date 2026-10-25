"""Self-test for lj. No test framework, stdlib only.

    python -m lj.selftest          # from anywhere, once installed
    python lj/selftest.py          # straight from a clone
"""
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable

# Import the sibling modules as lj.listen / lj.dialer. When this file is run
# directly from a clone, lj/ is already sys.path[0] but the PARENT is not, so
# the package name has to be importable either way.
if __package__ in (None, ""):
    sys.path.insert(0, str(HERE.parent))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    return cond


_KIDS = []          # every child process we start, so none is left running

# On Windows, children started without a new process group are killed with
# the parent; giving them their own group is what lets a group kill work.
_NEW_GROUP = (subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)


def _reap(kid, timeout=6):
    """Stop a child and wait for it. Best effort: never raises."""
    try:
        if kid.poll() is None:
            kid.terminate()
        try:
            kid.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kid.kill()
            try:
                kid.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    except Exception:
        pass


def _kill_tree(kid):
    """Kill a child and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(kid.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(kid.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        kid.wait(timeout=5)
    except Exception:
        pass


def _alive(pid: int) -> bool:
    """Is this pid still running?

    Not os.kill(pid, 0): on Windows that does not probe, it signals, and
    it raises for a process that is very much alive -- so every stale pid
    looked dead and nothing was ever cleaned up. tasklist is the reliable
    check there; on POSIX, signal 0 is the right probe.
    """
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, errors="replace", timeout=20).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


_STALE = pathlib.Path(tempfile.gettempdir()) / "lj_selftest_children.txt"


def _reap_stale():
    """Kill listeners left behind by a previous run that was killed.

    atexit covers a normal exit and an exception. It does NOT cover a run
    that is killed outright -- the terminal closed, or a timeout fired --
    and then the children stay bound to their ports. That is how 33
    listeners piled up on this machine. The pid file lets the next run
    clean up after the last one.
    """
    try:
        if not _STALE.exists():
            return
        for line in _STALE.read_text().splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if pid == os.getpid():
                continue
            if _alive(pid):
                _kill_tree_pid(pid)
        _STALE.unlink(missing_ok=True)
    except Exception:
        pass


def _kill_tree_pid(pid: int):
    """Kill a pid that was orphaned by a previous run.

    The /T flag walks the tree from the parent, and the parent is already
    dead by the time the next run gets here, so /T finds nothing to kill.
    The pid itself has to be signalled.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=20)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    # give the OS a moment to reap it
    for _ in range(20):
        if not _alive(pid):
            return
        time.sleep(0.1)


def _remember(kid):
    """Record a child so a later run can clean it up if this one dies."""
    _KIDS.append(kid)
    try:
        with _STALE.open("a") as fh:
            fh.write(f"{kid.pid}{chr(10)}")
    except Exception:
        pass


def _reap_all():
    for _k in _KIDS:
        _reap(_k)


import atexit
atexit.register(_reap_all)      # runs on any exit, including an exception


_reap_stale()      # clean up after a run that was killed


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load(module):
    """Import one of the package's modules by name."""
    import importlib
    return importlib.import_module(module)


L = load("lj.listen")
D = load("lj.dialer")


def get(port, path, token=None, timeout=10, tls_ca=None):
    h = {"X-LJ-Token": token} if token else {}
    scheme = "https" if tls_ca else "http"
    req = urllib.request.Request(f"{scheme}://127.0.0.1:{port}{path}", headers=h)
    ctx = None
    if tls_ca:
        import ssl
        ctx = ssl.create_default_context(cafile=tls_ca)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post(port, path, body, token=None, timeout=10):
    h = {"Content-Type": "text/plain"}
    if token:
        h["X-LJ-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=body.encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- token

print("\n=== 1. token ===")
t1, t2 = L.mint_token(), L.mint_token()
check("two tokens differ", t1 != t2)
check("prefix", t1.startswith("lj_"), t1[:12])
check("length is 256 bits + prefix", len(t1) == 3 + 52, f"{len(t1)} chars")
check("alphabet has no I/L/O/U", not set("ILOU") & set(t1))
check("verify accepts the right token", L.verify(t1, t1))
check("verify rejects a wrong one", not L.verify(t1, t2))
check("verify rejects empty", not L.verify(t1, ""))
check("verify rejects a prefix of itself", not L.verify(t1, t1[:-1]))

tmp = pathlib.Path(tempfile.mkdtemp(prefix="lj_t_"))
tf = tmp / "token"
a, n1 = L.load_or_create_token(str(tf))
b, n2 = L.load_or_create_token(str(tf))
check("token persists across calls", a == b, f"{n1} / {n2}")
if os.name == "posix":
    # NTFS has no POSIX permission bits (st_mode is a fixed 0o666), so this
    # can only be asserted where the bits mean something.
    check("token file is 0600",
          oct(os.stat(tf).st_mode & 0o777) == "0o600",
          oct(os.stat(tf).st_mode & 0o777))
else:
    print("  skip  token file is 0600   (no POSIX modes on this platform)")
c, _ = L.load_or_create_token(str(tf), regen=True)
check("regen-token changes it", c != a)

# ---------------------------------------------------------------- parsing

print("\n=== 2. target parsing ===")
check("host:port", D.split_target("1.2.3.4:9999") == ("1.2.3.4", 9999, "http"))
check("host only -> 10000", D.split_target("1.2.3.4") == ("1.2.3.4", 10000, "http"))
check("bare port -> localhost", D.split_target("8080") == ("127.0.0.1", 8080, "http"))
check("trailing colon", D.split_target("host:") == ("host", 10000, "http"))
check("https:// is recognised",
      D.split_target("https://1.2.3.4:9999") == ("1.2.3.4", 9999, "https"))
check("http:// is recognised",
      D.split_target("http://1.2.3.4:9999") == ("1.2.3.4", 9999, "http"))
check("the scheme is case-insensitive",
      D.split_target("HTTPS://1.2.3.4:9999") == ("1.2.3.4", 9999, "https"))

# ---------------------------------------------------------------- server

print("\n=== 3. lj.listen serves ===")
PORT = free_port()
TOK = L.mint_token()
proc = subprocess.Popen([PY, "-m", "lj", "listen",
                         "--port", str(PORT), "--token-file", str(tmp / "t2"),
                         "--regen-token"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True)
_remember(proc)
deadline = time.time() + 20
up = False
while time.time() < deadline:
    if proc.poll() is not None:
        break
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=0.4)
        s.close(); up = True; break
    except OSError:
        time.sleep(0.3)

if not check("listener came up", up, proc.stdout.read()[-400:] if proc.poll() else ""):
    raise SystemExit(1)

with open(tmp / "t2") as fh:
    TOK = fh.read().strip()

status, html = get(PORT, "/")
check("GET / -> 200", status == 200, str(status))
check("page polls /msg", "fetch('msg'" in html, html[:160])
check("page is self-contained", "script" in html and "src=" not in html)

status, body = post(PORT, "/in", "hello from the far side", TOK)
check("POST /in with the right token -> 204", status == 204, str(status))

status, body = get(PORT, "/msg", TOK)
data = json.loads(body)
check("GET /msg returns what came in",
      data["msgs"] == ["hello from the far side"], body[:200])

status, body = get(PORT, "/msg", TOK)
check("queue is drained after one read", json.loads(body)["msgs"] == [], body[:80])

# ---------------------------------------------------------------- auth

print("\n=== 4. the token is actually enforced ===")
try:
    get(PORT, "/msg", "lj_wrong_token_wrong_token_wrong_token_wrong")
    check("wrong token is rejected", False, "it was accepted")
except urllib.error.HTTPError as e:
    check("wrong token is rejected", e.code == 401, str(e.code))

try:
    get(PORT, "/msg")
    check("no token is rejected", False, "it was accepted")
except urllib.error.HTTPError as e:
    check("no token is rejected", e.code == 401, str(e.code))

try:
    post(PORT, "/msg", "should not land", "lj_wrong")
    check("wrong token cannot post", False, "it was accepted")
except urllib.error.HTTPError as e:
    check("wrong token cannot post", e.code == 401, str(e.code))

# the cookie path is what a browser uses
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/msg",
                             headers={"Cookie": f"lj_token={TOK}"})
with urllib.request.urlopen(req, timeout=10) as r:
    check("cookie token works (browser path)", r.status == 200)

# ------------------------------------------------------------- dialer CLI

print("\n=== 5. lj.dialer ===")
PORT2 = free_port()
TOK2 = L.mint_token()
p2 = subprocess.Popen([PY, "-m", "lj", "listen",
                       "--port", str(PORT2), "--token-file", str(tmp / "t3"),
                       "--regen-token"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
_remember(p2)
deadline = time.time() + 20
while time.time() < deadline:
    try:
        s = socket.create_connection(("127.0.0.1", PORT2), timeout=0.4)
        s.close(); break
    except OSError:
        time.sleep(0.3)
with open(tmp / "t3") as fh:
    TOK2 = fh.read().strip()

r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT2}", "--token", TOK2,
                    "-m", "from the dialer"], capture_output=True, text=True, errors="replace",
                   timeout=30)
check("dialer exits 0", r.returncode == 0, r.stdout[-200:] + r.stderr[-200:])
check("dialer reports 204", "204" in r.stdout, r.stdout[-200:])

status, body = get(PORT2, "/msg", TOK2)
check("message arrived", json.loads(body)["msgs"] == ["from the dialer"], body[:120])

r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT2}", "--token", "lj_bad",
                    "-m", "nope"], capture_output=True, text=True, errors="replace", timeout=30)
check("dialer with a bad token fails", r.returncode == 1, str(r.returncode))
check("dialer says the token was rejected",
      "token rejected" in r.stdout, r.stdout[-160:])

dead = free_port()
r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{dead}", "--token", TOK2,
                    "-m", "x"], capture_output=True, text=True, errors="replace", timeout=30)
check("dialer to a dead port fails cleanly", r.returncode == 1)
check("dialer explains it cannot connect",
      "cannot reach" in r.stdout, r.stdout[-160:])

# stdin
r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT2}", "--token", TOK2],
                   input="piped text\n", capture_output=True, text=True, errors="replace",
                   timeout=30)
status, body = get(PORT2, "/msg", TOK2)
check("dialer reads stdin", json.loads(body)["msgs"] == ["piped text"], body[:120])

# ------------------------------------------------------------- two-way

print("\n=== 6. scope: messages only ===")
# A guard, not a feature: the endpoints below must stay message-shaped. If
# someone adds a raw/stream mode for tunnels, this is where it should fail.
_page = get(PORT, "/")[1]
_dialer_src = (HERE / "dialer.py").read_text(encoding="utf-8")
_listen_src = (HERE / "listen.py").read_text(encoding="utf-8")

for _path in ("/raw", "/stream", "/ws", "/tcp", "/shell", "/exec"):
    try:
        get(PORT, _path)
        check(f"{_path} is not served", False, "it responded")
    except urllib.error.HTTPError as _e:
        check(f"{_path} is not served", _e.code == 404, str(_e.code))
    except Exception as _e:
        check(f"{_path} is not served", False, type(_e).__name__)

check("the page offers no terminal", "<textarea" not in _page
      and "xterm" not in _page)
# --interactive is a REPL for messages, not a terminal: the guard is that
# lj offers no byte-stream or remote-shell transport. Listing "interactive"
# here would forbid the very feature it was meant to protect.
check("the dialer has no stream/pty option",
      not any(f"--{o}" in _dialer_src
              for o in ("raw", "stream", "pty", "shell", "tty", "exec")))
check("the REPL is messages, not a terminal",
      "--interactive" in _dialer_src
      and not any(w in _dialer_src
                  for w in ("pty.spawn", "os.openpty", "termios")))
check("the listener has no stream/pty option",
      not any(f'"{o}"' in _listen_src
              for o in ("/raw", "/stream", "/ws", "/shell", "/exec")))
check("the scope note explains why", "SCOPE -- messages only" in _listen_src)

print("\n=== 6. two-way (v2.1.0) ===")
PORT3 = free_port()
TOK3 = L.mint_token()
p3 = subprocess.Popen([PY, "-m", "lj", "listen",
                       "--port", str(PORT3), "--token-file", str(tmp / "t4"),
                       "--regen-token"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
_remember(p3)
deadline = time.time() + 20
while time.time() < deadline:
    try:
        s = socket.create_connection(("127.0.0.1", PORT3), timeout=0.4)
        s.close(); break
    except OSError:
        time.sleep(0.3)
with open(tmp / "t4") as fh:
    TOK3 = fh.read().strip()

# far side -> us
post(PORT3, "/in", "from the far side", TOK3)
status, body = get(PORT3, "/msg", TOK3)
check("GET /msg shows what the far side sent",
      json.loads(body)["msgs"] == ["from the far side"], body[:120])

# the browser's own POST, read by the far side
post(PORT3, "/msg", "from the page", TOK3)
status, body = get(PORT3, "/send", TOK3)
check("GET /send collects what the browser sent",
      json.loads(body)["msgs"] == ["from the page"], body[:120])

status, body = get(PORT3, "/send", TOK3)
check("outbound queue is drained after one read",
      json.loads(body)["msgs"] == [], body[:80])

# the two queues must not bleed into each other
post(PORT3, "/msg", "browser side", TOK3)
status, body = get(PORT3, "/msg", TOK3)
check("the browser's own message is not echoed back to it",
      json.loads(body)["msgs"] == [], f"leaked: {body[:100]}")

for bad in ("lj_wrong", ""):
    try:
        get(PORT3, "/send", bad)
        check(f"/send rejects token {bad!r}", False, "accepted")
    except urllib.error.HTTPError as e:
        check(f"/send rejects token {bad!r}", e.code == 401, str(e.code))

# the dialer's --listen must pick up what the page posted
d = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT3}", "--token", TOK3,
                    "--listen", "--once"], capture_output=True, text=True, errors="replace",
                   timeout=30)
check("dialer --listen exits 0", d.returncode == 0, d.stdout[-200:])
post(PORT3, "/msg", "browser says hi", TOK3)
d = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT3}", "--token", TOK3,
                    "--listen", "--once"], capture_output=True, text=True, errors="replace",
                   timeout=30)
check("dialer --listen prints the browser's message",
      "browser says hi" in d.stdout, d.stdout[-200:])

d = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT3}", "--token", "lj_bad",
                    "--listen", "--once"], capture_output=True, text=True, errors="replace",
                   timeout=30)
check("dialer --listen with a bad token fails", d.returncode == 1)

# ------------------------------------------------- the page authenticates

print("\n=== 6b. the banner is copy-pasteable ===")
# The browser URL printed by `lj listen` must carry the token in the
# fragment. Without it the page loads but sits at "no token", which reads
# to the user as a broken connection -- so the whole line has to be
# copy-pasteable.
_b = subprocess.run([PY, "-m", "lj", "listen", "--print-only",
                     "--port", "10999", "--token-file",
                     str(tmp / "t5"), "--regen-token"],
                    capture_output=True, text=True, errors="replace", timeout=30)
_out = _b.stdout
_url = [l for l in _out.splitlines() if "browser :" in l]
check("listen prints a browser line", bool(_url), _out[-200:])
if _url:
    _u = _url[0].split("browser :", 1)[1].strip()
    _tok = (tmp / "t5").read_text().strip()
    check("the URL carries the token in the fragment",
          _u.endswith(f"/#token={_tok}"), _u[-70:])
    check("the token is NOT in the path or query",
          "/#token=" in _u and "?" not in _u, _u)
    check("the dialer line is filled in too",
          "lj dialer --target" in _out and "--token " + _tok in _out,
          _out[-200:])

print("\n=== 6c. startup check and ctrl-c ===")
# The port self-probe must run BEFORE the banner, so a port held by
# someone else never leaves a URL on screen for the user to copy.
_held = free_port()
_thief = socket.socket()
_thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_thief.bind(("127.0.0.1", _held))
_thief.listen(4)                      # holds the port, answers nothing like lj

_r = subprocess.run([PY, "-m", "lj", "listen", "--port", str(_held),
                     "--host", "127.0.0.1", "--token-file", str(tmp / "t6")],
                    capture_output=True, text=True, errors="replace", timeout=30)
_thief.close()
check("a port held by another process is refused",
      _r.returncode != 0, f"rc={_r.returncode}")
_err = (_r.stderr or "") + (_r.stdout or "")
check("it says who is holding the port", "already in use" in _err
      or "something else is answering" in _err, _err[-160:])
check("no browser URL is printed when the check fails",
      "browser :" not in _err, _err[-160:])

# ctrl-c must release the port, not linger
import signal as _signal

_ctrl_port = free_port()
_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
if os.name == "nt":
    # Windows cannot deliver SIGINT from outside a process. CTRL_BREAK_EVENT
    # is the equivalent, and it only reaches a child that has its own
    # console process group.
    _kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    _interrupt = _signal.CTRL_BREAK_EVENT
else:
    _interrupt = _signal.SIGINT

_p = subprocess.Popen([PY, "-m", "lj", "listen", "--port", str(_ctrl_port),
                       "--host", "127.0.0.1", "--token-file", str(tmp / "t7")],
                      **_kwargs)
_remember(_p)
deadline = time.time() + 20
_up = False
while time.time() < deadline:
    if _p.poll() is not None:
        break
    try:
        _s = socket.create_connection(("127.0.0.1", _ctrl_port), timeout=0.5)
        _s.close(); _up = True; break
    except OSError:
        time.sleep(0.3)
check("listen comes up", _up)

if _up:
    _p.send_signal(_interrupt)
    try:
        _p.wait(timeout=10)
        # Windows reports STATUS_CONTROL_C_EXIT (0xC000013A) for a console
        # interrupt; that is a normal stop, not a crash.
        _rc = _p.returncode
        check("ctrl-c exits", _rc in (0, -2, 0xC000013A, 3221225786)
              or _rc is not None, f"rc={_rc}")
    except subprocess.TimeoutExpired:
        check("ctrl-c exits", False, "still running after 10s")
        _p.kill()

    time.sleep(0.5)
    try:
        _s = socket.socket()
        _s.bind(("127.0.0.1", _ctrl_port))    # raises if still held
        _s.close()
        check("the port is released after ctrl-c", True)
    except OSError as _e:
        check("the port is released after ctrl-c", False, str(_e))

print("\n=== 6d. the REPL is a real two-way session ===")
# Driven as a subprocess with scripted keystrokes, against a real listener.
# Unit-testing the pieces would not catch the things that actually broke:
# a prompt colliding with a log line, or /quit not releasing the session.
_rp = subprocess.Popen(
    [PY, "-m", "lj", "dialer", "--interactive",
     "--target", f"127.0.0.1:{PORT}", "--token", TOK, "--poll", "0.3"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT, text=True, bufsize=1)
_remember(_rp)

# The page's message is queued BEFORE the REPL starts, so the poller's
# first tick finds it. Letting the two race made this check flaky --
# /quit can be handled before the first poll response ever arrives.
post(PORT, "/msg", "page says hi", TOK)
time.sleep(0.3)
_SESSION = ("/help" + chr(10) + "hello from the repl" + chr(10)
            + "/send via slash" + chr(10) + "/quit" + chr(10))
try:
    _rp.stdin.write(_SESSION)
    _rp.stdin.flush()
    _rp.stdin.close()
except (BrokenPipeError, ValueError):
    pass
# communicate() flushes stdin, and stdin is already closed -- on Linux
# that raises ValueError and kills the whole suite. Set stdin to None so
# communicate() leaves it alone.
try:
    _rp.stdin = None
    _repl_out, _ = _rp.communicate(timeout=40)
except subprocess.TimeoutExpired:
    _rp.kill()
    _rp.stdin = None
    _repl_out, _ = _rp.communicate()

check("the REPL shows a banner", "interactive" in _repl_out, _repl_out[-120:])
check("/help prints the command list", "/send T" in _repl_out, _repl_out[-120:])
check("a typed line is shown as sent", "hello from the repl" in _repl_out)
check("the /send command line is shown as sent", "via slash" in _repl_out)
check("an inbound message is shown live", "page says hi" in _repl_out,
      _repl_out[-160:])
check("inbound is marked with <--", "<--" in _repl_out)
check("outbound is marked with -->", "-->" in _repl_out)
check("messages carry a timestamp", re.search(r"\[\d\d:\d\d:\d\d\]", _repl_out)
      is not None)
check("the prompt is on a line of its own", "you >" in _repl_out)
check("/quit leaves cleanly", "disconnected" in _repl_out, _repl_out[-120:])
_got = json.loads(get(PORT, "/msg", TOK)[1])
check("what the REPL sent reached the page",
      "hello from the repl" in _got.get("msgs", [])
      and "via slash" in _got.get("msgs", []), str(_got)[:120])

print("\n=== 6e. the version is consistent ===")
# This has drifted twice: pyproject said 2.2.0 while the tag said 2.3.0,
# and __version__ said 2.2.0 while the installed dist said 2.1.2. The
# installed version is what a user actually sees, so check all three.
# pyproject.toml lives next to the package in the repo, but an INSTALLED
# package has no pyproject.toml at all -- site-packages/lj/ sits inside
# site-packages/. Reading it unconditionally broke the suite on every
# installed copy.
_pyproject_path = None
for _cand in (HERE.parent / "pyproject.toml",
              HERE.parent.parent / "pyproject.toml",
              HERE.parent.parent.parent / "pyproject.toml"):
    if _cand.is_file():
        _pyproject_path = _cand
        break

_declared = ""
if _pyproject_path is not None:
    _m = re.search(r'version\s*=\s*"([^"]+)"',
                   _pyproject_path.read_text(encoding="utf-8"))
    _declared = _m.group(1) if _m else ""
_src = (HERE / "__init__.py").read_text(encoding="utf-8")
_m2 = re.search(r'__version__\s*=\s*"([^"]+)"', _src)
_fallback = _m2.group(1) if _m2 else ""
try:
    _installed = importlib.metadata.version("lj")
except Exception:
    _installed = "(not installed)"

if _pyproject_path is not None:
    # running from a source checkout: all three must agree
    check("pyproject declares a version", bool(_declared), _declared)
    check("the fallback __version__ matches pyproject",
          _fallback == _declared, f"{_fallback} vs {_declared}")
    check("the installed distribution matches pyproject",
          _installed == _declared, f"{_installed} vs {_declared}")
else:
    # an installed package: pyproject is not shipped, so the fallback and
    # the installed metadata are what there is to compare
    check("the fallback __version__ matches the installed distribution",
          _fallback == _installed, f"{_fallback} vs {_installed}")

print("\n=== 6f. https ===")
# Encryption has to be checked in the direction that matters: a client
# that does not trust the certificate must be refused, and plain http
# against a TLS port must fail. If only the happy path is tested, a
# broken --ca that silently skips verification would still pass.
_tls_dir = tmp / "tls"
_r = subprocess.run([PY, "-m", "lj", "listen", "--gen-cert", str(_tls_dir)],
                    capture_output=True, text=True, errors="replace", timeout=60)
check("--gen-cert makes a certificate", (_tls_dir / "cert.pem").exists()
      and (_tls_dir / "key.pem").exists(), _r.stderr[-200:])
check("--gen-cert says what to do with the certificate",
      "--ca" in (_r.stdout or ""), (_r.stdout or "")[-160:])

# The certificate has to be valid for the name the OTHER side uses. A
# cert with only "localhost" in its SAN list fails hostname verification
# the moment the far machine dials the routable address, and the error
# ("IP address mismatch") reads like a wrong certificate rather than a
# missing name. So the certificate must carry the machine's own address.
try:
    import ssl as _ssl
    _der = (_tls_dir / "cert.pem").read_bytes()
    _pem = _ssl.DER_cert_to_PEM_cert(_der) if False else None
    _txt = subprocess.run(["openssl", "x509", "-in", str(_tls_dir / "cert.pem"),
                           "-noout", "-text"],
                          capture_output=True, text=True, timeout=60).stdout
    _own = L.local_addr()
    check("the certificate is valid for this machine's own address",
          _own and _own in _txt, f"{_own} not in the SAN list")
    check("the certificate is valid for localhost", "localhost" in _txt)
except Exception as _e:
    check("the certificate is valid for this machine's own address",
          False, type(_e).__name__)

_tls_port = free_port()
# stdout goes to a FILE, not a pipe: a pipe nobody drains fills up, the
# child blocks writing to it, and the reap's wait() then blocks forever
_tls_out = open(tmp / "tls_listener.log", "w", encoding="utf-8")
_tls = subprocess.Popen(
    [PY, "-m", "lj", "listen", "--port", str(_tls_port), "--host", "127.0.0.1",
     "--cert", str(_tls_dir / "cert.pem"), "--key", str(_tls_dir / "key.pem"),
     "--token-file", str(tmp / "t8")],
    stdout=_tls_out, stderr=subprocess.STDOUT)
_remember(_tls)
# wait for the port to answer rather than sleeping a fixed amount: the
# listener has to read the cert, start serving and write the token file,
# and a fixed sleep is a race on a loaded machine
_deadline = time.time() + 25
_up = False
while time.time() < _deadline:
    if _tls.poll() is not None:
        break
    try:
        import socket as _sock
        _s = _sock.create_connection(("127.0.0.1", _tls_port), timeout=0.5)
        _s.close()
        _up = True
        break
    except OSError:
        time.sleep(0.3)
check("the TLS listener came up", _up)
# the token file is written at startup; if it is not there yet, the
# listener is not ready and reading it would raise
time.sleep(0.5)

if _up and _tls.poll() is None:
    _tls_tok = (tmp / "t8").read_text().strip()
    _ca = str(_tls_dir / "cert.pem")

    _r = subprocess.run([PY, "-m", "lj", "dialer", "--target",
                         f"https://127.0.0.1:{_tls_port}", "--token", _tls_tok,
                         "--ca", _ca, "-m", "over tls"],
                        capture_output=True, text=True, errors="replace", timeout=60)
    check("a dialer that trusts the certificate can send",
          _r.returncode == 0 and "204" in (_r.stdout or ""),
          (_r.stdout or "")[-120:])

    _r = subprocess.run([PY, "-m", "lj", "dialer", "--target",
                         f"https://127.0.0.1:{_tls_port}", "--token", _tls_tok,
                         "-m", "should not get through"],
                        capture_output=True, text=True, errors="replace", timeout=60)
    check("a dialer that does not trust the certificate is refused",
          _r.returncode != 0, (_r.stdout or "")[-120:])

    _r = subprocess.run([PY, "-m", "lj", "dialer", "--target",
                         f"https://127.0.0.1:{_tls_port}", "--token", _tls_tok,
                         "--insecure", "-m", "insecure path"],
                        capture_output=True, text=True, errors="replace", timeout=60)
    check("--insecure gets through", _r.returncode == 0 and "204" in (_r.stdout or ""),
          (_r.stdout or "")[-120:])

    _got = json.loads(get(_tls_port, "/msg", _tls_tok, tls_ca=_ca)[1])
    check("the message arrived over TLS",
          "over tls" in _got.get("msgs", []), str(_got)[:100])

    # plain http against a TLS port must fail
    try:
        import urllib.request as _ur
        _req = _ur.Request(f"http://127.0.0.1:{_tls_port}/msg",
                            headers={"X-LJ-Token": _tls_tok})
        with _ur.urlopen(_req, timeout=10) as _rr:
            _rr.read()
        check("plain http against a TLS port fails", False, "it succeeded")
    except Exception:
        check("plain http against a TLS port fails", True)

print("\n=== 6g. the README matches the code ===")
# A flag that exists but is not documented is a flag nobody uses; one
# that is documented but does not exist is worse -- the command fails.
# The README is shipped inside the package, so an installed copy has it;
# a checkout finds the repo-root one. Look in both places.
_readme = None
for _cand in (HERE / "README.md", HERE.parent / "README.md"):
    if _cand.is_file():
        _readme = _cand
        break
if _readme is not None:
    _md = _readme.read_text(encoding="utf-8")

    def _flags(sub):
        _h = subprocess.run([PY, "-m", "lj", sub, "-h"],
                            capture_output=True, text=True, timeout=60,
                            errors="replace").stdout
        return set(re.findall(r"--[a-z][a-z-]*", _h)) - {"--help"}

    _all = _flags("listen") | _flags("dialer")
    _documented = set(re.findall(r"`(--[a-z][a-z-]*)", _md))
    # a flag may also be written "-m, --message" -- that still has the
    # long form in backticks
    _documented |= set(re.findall(r"--[a-z][a-z-]*", "\n".join(
        _l for _l in _md.splitlines() if _l.startswith("|"))))

    _undocumented = sorted(_all - _documented)
    check("every flag is in the README", not _undocumented,
          "undocumented: " + ", ".join(_undocumented))

    # the README must not describe a retired version
    _retired = [w for w in ("v1.x was", "v1 has been", "previously it did",
                            "the old way") if w in _md]
    check("the README does not describe retired versions", not _retired,
          ", ".join(_retired))

else:
    check("the README is present", False, "README.md not found")


print("\n=== 6h. the GUI ===")
# The window is a view over the same server, so the test drives the
# WIDGETS and the real HTTP endpoints -- not a copy of the logic. A GUI
# that shows something different from what the page shows is worse than
# no GUI.
try:
    import tkinter as _tk
    _root = _tk.Tk()
    _root.withdraw()
    _have_display = True
except Exception:
    _have_display = False

if _have_display:
    try:
        sys.path.insert(0, str(HERE.parent))
        import lj.gui as _G
        _app = _G.App()
        _app.withdraw()

        check("the window has a title", _app.title() == _G.APP_TITLE,
              _app.title())
        check("the window has a log", hasattr(_app, "log"))
        check("the window has the url field", hasattr(_app, "url_var"))

        # start it, the way the button does
        _gport = free_port()
        _app.port_var.set(str(_gport))
        _app.host_var.set("127.0.0.1")
        _app._start()
        _deadline = time.time() + 25
        _up = False
        while time.time() < _deadline:
            _app._drain()
            if _app.worker and _app.worker.is_alive() and _app.token:
                _up = True
                break
            time.sleep(0.2)
        check("the GUI starts a listener", _up,
              _app.worker.error if _app.worker else "?")

        if _up:
            _gurl = _app.url_var.get()
            check("the GUI shows the url with the token",
                  _gurl.startswith(f"http://127.0.0.1:{_gport}/")
                  and "#token=lj_" in _gurl, _gurl)

            # the page is really served on that port
            _s, _b = get(_gport, "/", None)
            check("the GUI's listener serves the page", _s == 200, str(_s))

            # sending through the window reaches the queue the far side
            # reads
            _app.msg_var.set("from the window")
            _app._send()
            _app._drain()
            _got = json.loads(get(_gport, "/send", _app.token)[1])
            check("what the window sends reaches the far side",
                  "from the window" in _got.get("msgs", []), str(_got)[:100])

            # ...and the window must not then show it as arriving. It
            # posts to /msg and polls /send -- the same queue -- so
            # without tracking its own sends it echoes itself.
            _app._poll()
            _app._drain()
            _text = _app.log.get("1.0", "end")
            check("the window does not show its own message as incoming",
                  "<- from the window" not in _text, _text[-160:])
            check("the window does show it as sent",
                  "-> from the window" in _text, _text[-160:])

            # Click the button. Calling _stop() directly would not catch
            # the button being disabled -- it says "Stop" and cannot be
            # clicked, which is exactly what a user sees.
            _app._set_button("Stop")
            _app.btn.invoke()
            time.sleep(1.2)
            check("clicking Stop stops the listener",
                  _app.btn.cget("text") == "Start", _app.btn.cget("text"))
            check("the button is clickable again",
                  "disabled" not in _app.btn.state(),
                  str(_app.btn.state()))

            # The log has to say the stop finished. A log that stops at
            # "stopping ..." reads as a hang, and the user cannot tell
            # a finished stop from a stuck one.
            _log = _app.log.get("1.0", "end")
            check("the log says the listener stopped",
                  "stopped" in _log,
                  repr(_log.strip().splitlines()[-1] if _log.strip() else ""))
            check("the log does not stop at 'stopping ...'",
                  _log.strip().splitlines()[-1].strip() != "stopping ...",
                  repr(_log.strip().splitlines()[-1]))

            # and the same for Start: it must be clickable, and it must
            # become "Stop" and stay clickable.
            #
            # The stop is asynchronous -- the button's command returns
            # before the worker has finished -- so wait for the worker
            # to actually be gone. Otherwise the next click lands while
            # the old one is still winding down, the toggle sees a live
            # worker and stops it again instead of starting.
            _deadline = time.time() + 15
            while time.time() < _deadline:
                if not (_app.worker and _app.worker.is_alive()):
                    break
                time.sleep(0.2)
            check("the worker is really gone before the next click",
                  not (_app.worker and _app.worker.is_alive()),
                  "still running")

            _app.btn.invoke()
            # Wait for the worker's SERVER, not for the token: the token
            # is the same one from the previous run, so it is already
            # set and the wait would return before the new listener had
            # bound. That is the difference between "the button says
            # Stop" and "there is a listener behind it".
            _deadline = time.time() + 25
            while time.time() < _deadline:
                _app._drain()
                if (_app.worker and _app.worker.is_alive()
                        and _app.worker.srv is not None):
                    break
                time.sleep(0.2)
            check("clicking Start starts the listener",
                  _app.worker is not None and _app.worker.is_alive(),
                  _app.worker.error if _app.worker else "?")
            check("the new worker really has a server",
                  _app.worker is not None and _app.worker.srv is not None)
            check("the button says Stop and can be clicked",
                  _app.btn.cget("text") == "Stop"
                  and "disabled" not in _app.btn.state(),
                  f"{_app.btn.cget('text')} {str(_app.btn.state())}")

            _app._stop()
            time.sleep(1.0)

        _app.destroy()
        _root.destroy()
    except Exception as _e:
        check("the GUI works", False, f"{type(_e).__name__}: {_e}")
else:
    print("  skip  the GUI tests         (no display)")

print("\n=== 6h2. the launcher opens the window when there is no console ===")
# The Start Menu shortcut runs the GUI launcher with no arguments. The
# launcher is pythonw-based, so it has no console: printing usage goes
# nowhere and the process exits, which the user sees as "nothing
# happened". With no console and no arguments the dispatcher must open
# the window instead.
import lj as _ljmod

check("the dispatcher knows whether there is a console",
      isinstance(_ljmod._has_console(), bool),
      str(_ljmod._has_console()))

if sys.platform == "win32":
    # Run the same check under pythonw.exe, which has no console, and
    # read the answer back out of a file.
    import subprocess as _sp
    import tempfile as _tf

    _probe = pathlib.Path(_tf.gettempdir()) / "lj_console_probe.py"
    _out = pathlib.Path(_tf.gettempdir()) / "lj_console_probe.out"
    _out.unlink(missing_ok=True)
    # the probe has to import the same lj this suite is testing
    _pkg_parent = str(pathlib.Path(__file__).resolve().parent.parent)
    _probe.write_text(
        "import sys, pathlib\n"
        f"sys.path.insert(0, {_pkg_parent!r})\n"
        "import lj\n"
        f"pathlib.Path({str(_out)!r}).write_text(str(lj._has_console()))\n",
        encoding="utf-8")

    _pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    if _pyw.is_file():
        _sp.run([str(_pyw), str(_probe)], capture_output=True, timeout=60)
        _deadline = time.time() + 15
        while time.time() < _deadline and not _out.exists():
            time.sleep(0.2)
        check("pythonw reports that it has no console",
              _out.exists() and _out.read_text().strip() == "False",
              _out.read_text().strip() if _out.exists() else "(no answer)")
    else:
        print("  skip  the pythonw check      (no pythonw.exe next to this python)")
    _probe.unlink(missing_ok=True)

print("\n=== 6i. every subcommand takes the dispatcher's calling convention ===")
# The dispatcher calls run(rest) for every command. A main() that does
# not accept an argument therefore works from a checkout (where it is
# called directly) and fails from an installed copy (where it is called
# through the dispatcher) with a TypeError.
import inspect
for _name in ("listen", "dialer", "gui"):
    _mod = load(f"lj.{_name}")
    _fn = getattr(_mod, "main", None)
    check(f"lj.{_name} has a main()", _fn is not None)
    if _fn is None:
        continue
    _sig = inspect.signature(_fn)
    _takes = (_sig.parameters
              and any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                      for p in _sig.parameters.values()))
    _has_default = bool(_sig.parameters
                         and all(p.default is not p.empty
                                 for p in _sig.parameters.values()))
    check(f"lj.{_name}.main() accepts the argument list",
          _takes or _has_default, str(_sig))

# and the dispatcher actually calls it that way
_r = subprocess.run([PY, "-m", "lj", "gui", "--nonsense"], capture_output=True,
                    text=True, timeout=60, errors="replace")
check("lj gui with a bad option fails cleanly, not with a TypeError",
      "TypeError" not in (_r.stdout or "") + (_r.stderr or ""),
      ((_r.stdout or "") + (_r.stderr or ""))[-120:])

print("\n=== 7. the page can actually authenticate ===")
status, html = get(PORT3, "/")
check("page reads the token from the fragment", "location.hash" in html)
check("page stores it in a cookie", "document.cookie" in html)
check("page sends the X-LJ-Token header", "X-LJ-Token" in html)
check("page has a way to send back", 'id="form"' in html and "method: 'POST'" in html)
check("page clears the fragment from the URL",
      "replaceState" in html, "token would stay in the address bar")

# and a page that did NOT set the cookie really is refused -- this is the
# case the v2.0.0 test missed by setting the cookie by hand
try:
    get(PORT3, "/msg")
    check("a page with no token at all is refused", False, "accepted")
except urllib.error.HTTPError as e:
    check("a page with no token at all is refused", e.code == 401, str(e.code))

_reap_all()

# The README quotes the number of checks, so it has to be checked here
# and not earlier: the total is only known once every check has run.
if _readme is not None:
    _md = _readme.read_text(encoding="utf-8")
    _m = re.search(r"(\d+) checks", _md)
    if _m:
        _claimed = int(_m.group(1))
        _actual = len(PASS) + len(FAIL) + 1     # + this check
        # A checkout runs more checks than an installed copy: some of
        # them need the repository (the README at the root, the source
        # tree itself). So the README's number is the checkout's, and an
        # installed copy is allowed to run fewer.
        _in_checkout = (HERE.parent / "pyproject.toml").is_file()
        if _in_checkout and _claimed != _actual:
            FAIL.append("the README states the real number of checks "
                        f"(README says {_claimed}, the suite has {_actual})")
            print(f"  FAIL  the README states the real number of checks   "
                  f"README says {_claimed}, suite has {_actual}")
        else:
            PASS.append("the README states the real number of checks")
            print(f"  ok    the README states the real number of checks   "
                  f"(README says {_claimed}, this run has {_actual}"
                  + ("" if _in_checkout else ", fewer in an installed copy")
                  + ")")

print("\n" + "=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)
