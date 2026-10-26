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


def _has_display() -> bool:
    """Is there a display to open a window on?

    A headless Linux has no DISPLAY, so the GUI tests cannot run there.
    Windows always has one, even over a remote session.
    """
    if os.name == "nt":
        return True
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# The GUI section's check count. The README's number is the one for a
# platform that runs everything, and a headless Linux skips the GUI
# tests, so the count has to be known to work out what that platform
# should see. It is counted at run time rather than written down: a
# check added to that section would otherwise make this number wrong
# in a way nothing else would catch.
def _gui_check_names() -> list:
    """The names of the checks in the GUI section.

    Read from the source rather than written down, because the number
    of them is a property of the source and a list kept by hand goes
    stale the moment one is added -- which is exactly the sort of
    quiet drift a self-test is supposed to catch.
    """
    src = (HERE / "selftest.py").read_text(encoding="utf-8")
    # The section headings are the anchors. They are found by their
    # TEXT, not their number, so a renumbering does not silently
    # break this.
    #
    # They have to be found as HEADINGS, not as text anywhere: this
    # function's own source mentions the same words, and a plain
    # find() matched THAT first -- so the function returned its own
    # body, found no checks in it, and the skipped checks were never
    # recorded. The count on a headless Linux came out twenty short
    # and nothing said why. A heading is a line that STARTS with
    # print(" and carries the section title.
    def _heading(title: str, after: int = 0) -> int:
        pat = re.compile(r'^print\("\\n=== ' + re.escape(title) + r'[^"]*"\)',
                         re.M)
        m = pat.search(src, after)
        return m.start() if m else -1

    # The range stops at the NEXT section, so this section's list does
    # not swallow the one after it -- each section has its own list,
    # and the gate skips them all.
    i = _heading("6g. the GUI")
    j = _heading("6h. the GUI takes the server down", i)
    if i < 0 or j < 0:
        return []
    return re.findall(r'check\("([^"]+)"', src[i:j])


def _gui_exit_check_names() -> list:
    """The names of the checks in the "the server goes down with it"
    section.

    Read from the source, like the GUI section's own list: the count
    has to be a property of the source, not something written down
    next to it.
    """
    src = (HERE / "selftest.py").read_text(encoding="utf-8")
    i = src.find('print("\\n=== 6h. the GUI takes the server down')
    j = src.find('print("\\n=== 6i. the launcher', i)
    if i < 0 or j < 0:
        return []
    # These checks are in f-strings, so the plain pattern does not see
    # them: the name is a format field, not a literal. And each name
    # is used for TWO modes, so the list has to carry both -- a name
    # list of two for four checks makes the count short on a headless
    # machine, and short by exactly the number that is missing.
    # The name list has to match what actually RUNS, or the count is
    # off on a headless machine. Two of the three checks run for every
    # mode; the third -- "the port can be bound after the GUI is
    # killed" -- only runs in the killed branch, because that is the
    # only mode where the process is gone and the OS owns the port.
    names = re.findall(r'check\(f"([^"]*\{_mode\}[^"]*)"', src[i:j])
    modes = ("close", "mainloop", "killed")
    out = []
    for n in names:
        for m in modes:
            if "is killed" in n and m != "killed":
                continue
            out.append(n.format(_mode=m))
    return out


SKIPPED = []       # the checks this platform could not run


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    return cond


def skip(name, why):
    """A check this platform cannot run.

    Recorded rather than printed and forgotten, because the README
    quotes the number of checks and that number has to be the same on
    every platform -- so the count is the total, plus what was
    skipped here.
    """
    SKIPPED.append(name)
    print(f"  skip  {name}   ({why})")


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


def get(port, path, token=None, timeout=10):
    h = {"X-LJ-Token": token} if token else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    skip("token file is 0600", "no POSIX modes on this platform")
c, _ = L.load_or_create_token(str(tf), regen=True)
check("regen-token changes it", c != a)

# ---------------------------------------------------------------- parsing

print("\n=== 2. target parsing ===")
check("host:port", D.split_target("1.2.3.4:9999") == ("1.2.3.4", 9999, None))
check("host only -> 10000", D.split_target("1.2.3.4") == ("1.2.3.4", 10000, None))
check("bare port -> localhost", D.split_target("8080") == ("127.0.0.1", 8080, None))
check("trailing colon", D.split_target("host:") == ("host", 10000, None))
# The scheme is accepted and ignored: there is no encryption, so a
# target is an address and a port. An old "https://..." target string
# still parses rather than failing.
check("https:// is accepted and ignored",
      D.split_target("https://1.2.3.4:9999") == ("1.2.3.4", 9999, None))
check("http:// is accepted and ignored",
      D.split_target("http://1.2.3.4:9999") == ("1.2.3.4", 9999, None))
check("the scheme is case-insensitive",
      D.split_target("HTTPS://1.2.3.4:9999") == ("1.2.3.4", 9999, None))
# The token rides in the fragment, so the listener's browser URL can be
# handed to the far side as it stands.
check("the token comes out of the fragment",
      D.split_target("1.2.3.4:9999/#token=lj_abc") == ("1.2.3.4", 9999, "lj_abc"))
check("a full url with a fragment",
      D.split_target("http://1.2.3.4:9999/#token=lj_abc")
      == ("1.2.3.4", 9999, "lj_abc"))
check("a trailing slash does not break the port",
      D.split_target("http://1.2.3.4:9999/") == ("1.2.3.4", 9999, None))
check("a fragment with no token is no token",
      D.split_target("1.2.3.4:9999/#something") == ("1.2.3.4", 9999, None))
check("the token survives another parameter in the fragment",
      D.split_target("1.2.3.4:9999/#a=1&token=lj_abc") == ("1.2.3.4", 9999, "lj_abc"))

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
check("the page polls the results", "fetch('res" in html, html[:160])
check("the page does not poll the message queue", "fetch('msg'" not in html,
      "the chat is back")
check("page is self-contained", "script" in html and "src=" not in html)

# The message endpoints are GONE. They are not to be brought back, so
# the test asserts they 404 rather than just not testing them.
for _path in ("/msg", "/in", "/send"):
    try:
        get(PORT, _path, TOK)
        check(f"GET {_path} is gone", False, "it still answers")
    except urllib.error.HTTPError as e:
        check(f"GET {_path} is gone", e.code == 404, str(e.code))
    try:
        post(PORT, _path, "should not land", TOK)
        check(f"POST {_path} is gone", False, "it still accepts")
    except urllib.error.HTTPError as e:
        check(f"POST {_path} is gone", e.code == 404, str(e.code))

# ---------------------------------------------------------------- auth

print("\n=== 4. the token is actually enforced ===")
try:
    get(PORT, "/cmd", "lj_wrong_token_wrong_token_wrong_token_wrong")
    check("wrong token is rejected", False, "it was accepted")
except urllib.error.HTTPError as e:
    check("wrong token is rejected", e.code == 401, str(e.code))

try:
    get(PORT, "/cmd")
    check("no token is rejected", False, "it was accepted")
except urllib.error.HTTPError as e:
    check("no token is rejected", e.code == 401, str(e.code))

try:
    post(PORT, "/cmd", json.dumps({"op": "ls", "args": {}}), "lj_wrong")
    check("wrong token cannot post", False, "it was accepted")
except urllib.error.HTTPError as e:
    check("wrong token cannot post", e.code == 401, str(e.code))

# the cookie path is what a browser uses
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/cmd",
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

# The far side is a worker, so the checks here are about the TARGET
# and the TOKEN, not about messages.
r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT2}", "--token", TOK2,
                    "--once"], capture_output=True, text=True, errors="replace",
                   timeout=30)
check("dialer exits 0", r.returncode == 0, r.stdout[-200:] + r.stderr[-200:])
check("dialer says what it is polling", "127.0.0.1" in r.stdout, r.stdout[-200:])

# The target can be positional, and the token can ride in its
# fragment. The listener's banner prints ONE line with the token in
# it, and that line is what a person hands to the far side -- so the
# far side has to be able to take it as it stands.
r = subprocess.run([PY, "-m", "lj", "dialer",
                    f"127.0.0.1:{PORT2}/#token={TOK2}",
                    "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("the target can be positional, with the token in its fragment",
      r.returncode == 0, r.stdout[-200:] + r.stderr[-200:])

r = subprocess.run([PY, "-m", "lj", "dialer",
                    f"127.0.0.1:{PORT2}", "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("a target with no token is refused", r.returncode == 2, str(r.returncode))
check("it says how to give the token", "--token" in (r.stdout + r.stderr),
      (r.stdout + r.stderr)[-160:])

r = subprocess.run([PY, "-m", "lj", "dialer"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("no target at all prints the usage", "usage:" in r.stdout,
      r.stdout[-160:])
check("no target at all is not a success", r.returncode != 0, str(r.returncode))

r = subprocess.run([PY, "-m", "lj", "dialer",
                    "127.0.0.1:1", "--target", f"127.0.0.1:{PORT2}",
                    "--token", TOK2, "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("two different targets are refused",
      r.returncode == 2 and "two different targets" in r.stdout,
      (r.stdout + r.stderr)[-160:])

r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{PORT2}", "--token", "lj_bad",
                    "--once"], capture_output=True, text=True, errors="replace", timeout=30)
check("dialer with a bad token fails", r.returncode == 1, str(r.returncode))
check("dialer says the token was rejected",
      "token rejected" in r.stdout, r.stdout[-160:])

# A worker does not EXIT when the far side is not there -- it says
# so and tries again, because the far side may come back. That is the
# whole point of a worker: it is a daemon, and a daemon that gives
# up is not one.
dead = free_port()
r = subprocess.run([PY, "-m", "lj", "dialer",
                    "--target", f"127.0.0.1:{dead}", "--token", TOK2,
                    "--once"], capture_output=True, text=True, errors="replace", timeout=30)
check("dialer to a dead port does not crash", r.returncode in (0, 1),
      str(r.returncode))
check("dialer explains it cannot connect",
      "cannot reach" in r.stdout, r.stdout[-160:])

# -------------------------------------------------------- the worker mode

# The far side is ONE command and it IS the worker: lj dialer. There is
# no --worker flag and no other mode. It takes the same target shapes
# the banner's line for the far side uses: positional, or --target, and
# the token in the fragment.
r = subprocess.run([PY, "-m", "lj", "dialer",
                    f"127.0.0.1:{PORT2}/#token={TOK2}",
                    "--root", str(tmp), "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("the worker takes a positional target with the token in its fragment",
      r.returncode == 0, (r.stdout + r.stderr)[-200:])
check("the worker says what it is listening to",
      f"127.0.0.1:{PORT2}" in r.stdout, r.stdout[-200:])
check("the worker says what it is",
      "lj dialer" in r.stdout and "far side" in r.stdout, r.stdout[-200:])

# --root names the tree the browser may touch.
r = subprocess.run([PY, "-m", "lj", "dialer",
                    f"127.0.0.1:{PORT2}/#token={TOK2}",
                    "--root", str(tmp), "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("--root is accepted", r.returncode == 0, (r.stdout + r.stderr)[-200:])
check("the worker says which tree it serves", str(tmp) in r.stdout,
      r.stdout[-200:])

r = subprocess.run([PY, "-m", "lj", "dialer",
                    f"127.0.0.1:{PORT2}", "--root", str(tmp), "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("the worker with no token is refused", r.returncode == 2, str(r.returncode))

r = subprocess.run([PY, "-m", "lj", "dialer"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("the worker with no target prints the usage", "usage:" in r.stdout,
      r.stdout[-160:])

r = subprocess.run([PY, "-m", "lj", "dialer",
                    "127.0.0.1:1", "--target", f"127.0.0.1:{PORT2}",
                    "--token", TOK2, "--root", str(tmp), "--once"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("the worker with two different targets is refused",
      r.returncode == 2 and "two different targets" in r.stdout,
      (r.stdout + r.stderr)[-160:])

# there is no second command any more
r = subprocess.run([PY, "-m", "lj", "remote"],
                   capture_output=True, text=True, errors="replace", timeout=30)
check("there is no lj remote any more", r.returncode != 0, str(r.returncode))

# ------------------------------------------------------------- two-way

print("\n=== 6. the file and terminal channel ===")
# The scope changed on purpose: the far side is now a file manager and
# a command runner, not just a message queue. What the guards below
# still protect is the SHAPE: every operation is a request that comes
# back whole. There is no byte stream, no PTY, and no open connection
# that carries partial output -- because those are the parts that turn
# a timeout into a hang.
_page = get(PORT, "/")[1]
_dialer_src = (HERE / "dialer.py").read_text(encoding="utf-8")
_listen_src = (HERE / "listen.py").read_text(encoding="utf-8")
_agent_src = (HERE / "agent.py").read_text(encoding="utf-8")

for _path in ("/raw", "/stream", "/ws", "/tcp"):
    try:
        get(PORT, _path)
        check(f"{_path} is not served", False, "it responded")
    except urllib.error.HTTPError as _e:
        check(f"{_path} is not served", _e.code == 404, str(_e.code))
    except Exception as _e:
        check(f"{_path} is not served", False, type(_e).__name__)

check("the page has a file pane", 'id="tree"' in _page)
check("the page has a terminal pane", 'id="out"' in _page)
check("the page parses ANSI colour", "renderAnsi" in _page)
check("the page splits a command line like a shell does",
      "splitArgs" in _page)
# There is still no PTY and no byte stream: a command runs to
# completion and its output comes back whole.
check("there is no PTY", not any(w in _agent_src
                                for w in ("pty.spawn", "os.openpty", "termios")))
check("the exec is a subprocess, not a shell",
      "subprocess.run" in _agent_src and "shell=True" not in _agent_src)
check("the scope note says what this is now",
      "SCOPE" in _listen_src and "shell credential" in _listen_src.lower())

print("\n=== 6b. the far side's operations ===")
# The agent is exercised directly: it is the far side's half, and the
# HTTP round trip is already covered by the tests above.
import lj.agent as _A

_root = tmp / "remote_root"
_root.mkdir(parents=True, exist_ok=True)
(_root / "hello.txt").write_text("hello from the far side\n", encoding="utf-8")
(_root / "sub").mkdir(exist_ok=True)
(_root / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")

_r = _A.run_op("ls", {"path": "."}, str(_root))
check("ls works", _r.get("ok"), str(_r))
_names = [e["name"] for e in _r.get("entries", [])]
check("ls lists the files", "hello.txt" in _names and "sub" in _names, str(_names))
_kinds = {e["name"]: e["kind"] for e in _r.get("entries", [])}
check("ls knows what is a directory", _kinds.get("sub") == "d", str(_kinds))

_r = _A.run_op("read", {"path": "hello.txt"}, str(_root))
check("read works", _r.get("ok"), str(_r))
check("read gets the text back",
      _r.get("text", "").rstrip("\r\n") == "hello from the far side",
      repr(_r.get("text")))
check("read says it is not binary", _r.get("binary") is False)

_r = _A.run_op("write", {"path": "new.txt", "text": "written\n"}, str(_root))
check("write works", _r.get("ok"), str(_r))
check("write put the file there",
      (_root / "new.txt").read_text(encoding="utf-8") == "written\n")

_r = _A.run_op("copy", {"path": "hello.txt", "to": "sub/copy.txt"}, str(_root))
check("copy works", _r.get("ok"), str(_r))
check("copy put the file there", (_root / "sub" / "copy.txt").exists())

_r = _A.run_op("move", {"path": "sub/copy.txt", "to": "sub/moved.txt"}, str(_root))
check("move works", _r.get("ok"), str(_r))
check("move left the old name", not (_root / "sub" / "copy.txt").exists())
check("move made the new one", (_root / "sub" / "moved.txt").exists())

_r = _A.run_op("delete", {"path": "new.txt"}, str(_root))
check("delete works", _r.get("ok"), str(_r))
check("delete removed the file", not (_root / "new.txt").exists())

_r = _A.run_op("delete", {"path": "sub"}, str(_root))
check("delete refuses a directory without the recursive flag",
      not _r.get("ok"), str(_r))
check("the directory is still there", (_root / "sub").exists())
_r = _A.run_op("delete", {"path": "sub", "recursive": True}, str(_root))
check("the recursive delete works", _r.get("ok"), str(_r))
check("the directory is gone", not (_root / "sub").exists())

# the jail
_r = _A.run_op("ls", {"path": "/etc"}, str(_root))
check("an absolute path outside the root is refused", not _r.get("ok"), str(_r))
_r = _A.run_op("ls", {"path": "../../etc"}, str(_root))
check("a relative path out of the root is refused", not _r.get("ok"), str(_r))
_r = _A.run_op("delete", {"path": "."}, str(_root))
check("the root itself cannot be deleted", not _r.get("ok"), str(_r))

# the shell
_r = _A.run_op("exec", {"argv": ["echo", "hello"]}, str(_root))
check("exec works", _r.get("ok"), str(_r))
if _r.get("ok"):
    import base64 as _b64
    _out = _b64.b64decode(_r.get("output", "")).decode("utf-8", "replace")
    check("exec returns the output", _out.strip() == "hello", repr(_out))
    check("exec returns the exit code", _r.get("code") == 0, str(_r.get("code")))

_r = _A.run_op("exec", {"argv": ["definitely-not-a-command-xyz"]}, str(_root))
check("a missing command is reported", not _r.get("ok"), str(_r))

_r = _A.run_op("exec", {"argv": []}, str(_root))
check("an empty command is refused", not _r.get("ok"), str(_r))

_r = _A.run_op("exec", {"argv": "echo hello"}, str(_root))
check("a command as a string is refused", not _r.get("ok"), str(_r))

# the colour has to survive the trip
_esc = chr(27)
_r = _A.run_op("exec",
              {"argv": [sys.executable, "-c",
                        "import sys;sys.stdout.write(chr(27)+'[31mRED')"]},
              str(_root))
if _r.get("ok"):
    import base64 as _b64
    _out = _b64.b64decode(_r.get("output", "")).decode("utf-8", "replace")
    check("the ANSI escape survives", _esc + "[31m" in _out, repr(_out))
else:
    check("the ANSI escape survives", False, str(_r))

# the unknown operation
_r = _A.run_op("nonsense", {}, str(_root))
check("an unknown operation is refused", not _r.get("ok"), str(_r))

# and the wire
import lj.wire as _Wi
_w = _Wi.Wire()
_s = _w.put_command("ls", {"path": "."})
check("the wire numbers the commands", isinstance(_s, int), str(type(_s)))
_cmds = _w.take_commands()
check("the wire hands the command over", len(_cmds) == 1, str(_cmds))
check("the command kept its number", _cmds[0]["seq"] == _s, str(_cmds))
check("the wire does not hand it over twice", _w.take_commands() == [])
_w.put_result({"seq": _s, "ok": True, "value": 42})
_res = _w.take_results(after=0)
check("the result comes back", len(_res) == 1 and _res[0]["value"] == 42, str(_res))
check("a result already collected is not repeated", _w.take_results(after=0) == [])
_w.put_result({"seq": _s + 1, "ok": True})
_res = _w.take_results(after=_s)
check("only the newer result comes back", len(_res) == 1 and _res[0]["seq"] == _s + 1,
      str(_res))

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

# The message endpoints are GONE. They carried the two message queues,
# and there is one channel now: commands and their results. So the
# endpoints that carried messages must be gone -- a 404, not a quiet
# success, because a quiet success would let a stale client think it
# still works.
for _path in ("/msg", "/in", "/send"):
    for _method in ("GET", "POST"):
        try:
            if _method == "GET":
                get(PORT3, _path, TOK3)
            else:
                post(PORT3, _path, "should not land", TOK3)
            check(f"{_method} {_path} is gone", False, "it still answers")
        except urllib.error.HTTPError as e:
            check(f"{_method} {_path} is gone", e.code == 404, str(e.code))

# and the command channel still works, both ways
_seq = post(PORT3, "/cmd", json.dumps({"op": "ls", "args": {}}), TOK3)
check("POST /cmd with the right token is accepted", _seq[0] == 200,
      str(_seq[0]))
_status, _body = get(PORT3, "/cmd", TOK3)
_cmds = json.loads(_body).get("cmds") or []
check("GET /cmd gives the browser's command to the far side",
      len(_cmds) == 1 and _cmds[0].get("op") == "ls", _body[:160])
check("the command carries a sequence number",
      isinstance(_cmds[0].get("seq"), int) if _cmds else False,
      str(_cmds[0].get("seq")) if _cmds else "no command")

# the far side answers it
post(PORT3, "/res", json.dumps({"seq": _cmds[0]["seq"], "op": "ls",
                                 "ok": True, "data": {"files": []}}),
     TOK3)
_status, _body = get(PORT3, "/res", TOK3)
_results = json.loads(_body).get("results") or []
check("GET /res gives the answer back to the browser",
      len(_results) == 1 and _results[0].get("ok") is True, _body[:160])

# and the bad token is still refused on the command channel
for bad in ("lj_wrong", ""):
    try:
        get(PORT3, "/cmd", bad)
        check(f"/cmd rejects token {bad!r}", False, "accepted")
    except urllib.error.HTTPError as e:
        check(f"/cmd rejects token {bad!r}", e.code == 401, str(e.code))

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
    # The line for the far side carries the token IN the target, so it
    # can be handed over as it stands -- there is nothing to mistype.
    check("the dialer line is filled in too",
          "lj dialer" in _out and _tok in _out, _out[-200:])
    # There is ONE line for the far side now, and it has no --worker
    # on it: the dialer IS the worker.
    check("the line for the far side has no --worker on it",
          "--worker" not in _out, _out[-200:])
    check("the line for the far side carries the token in the fragment",
          f"#token={_tok}" in _out, _out[-200:])
    # The URL must carry an address a browser on ANOTHER machine can
    # reach. 127.0.0.1 only works on this machine, and the browser
    # line is the one a person pastes into a browser that is somewhere
    # else -- so a loopback URL there means "it does not work" for
    # everyone but the person at the keyboard.
    _host = _u.split("://", 1)[1].split(":", 1)[0].split("/")[0]
    check("the browser URL is not a loopback address",
          _host not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"),
          _u)
    check("the browser URL uses this machine's own address",
          _host == L.local_addr(), f"{_host} vs {L.local_addr()}")

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

    # "the port is released" means nothing is LISTENING on it any more.
    # It is not the same question as "can this socket bind the port":
    #   - the readiness probe above left a TIME_WAIT socket on it, and
    #     a plain bind fails on that on Linux even though the listener
    #     is gone
    #   - SO_REUSEADDR would fix the bind but make the check blind: on
    #     Windows it binds happily even with a live listener, so the
    #     check would pass when the port is still held
    # Connecting is the honest test: a refused connection means
    # nothing is listening.
    time.sleep(0.5)
    _still_up = False
    try:
        _s = socket.create_connection(("127.0.0.1", _ctrl_port), timeout=1)
        _s.close()
        _still_up = True
    except OSError:
        _still_up = False
    check("the port is released after ctrl-c", not _still_up,
          "something is still listening on it")

print("\n=== 6d. the REPL is a real two-way session ===")
# Driven as a subprocess with scripted keystrokes, against a real listener.
# The REPL is GONE. It was the messenger's half, and the messenger is
# gone. The dialer is a worker: it polls, it works, it answers. There
# is nothing to type into and nothing to read.
_d = load("lj.dialer")
check("lj.dialer has no repl()", not hasattr(_d, "repl"),
      "the REPL is back")
check("lj.dialer has no collect()", not hasattr(_d, "collect"),
      "the collector is back")
check("lj.dialer has no send()", not hasattr(_d, "send"),
      "the sender is back")
check("lj.dialer has no read_message()", not hasattr(_d, "read_message"),
      "the message reader is back")
_r = subprocess.run([PY, "-m", "lj", "dialer", "--interactive",
                     "--target", f"127.0.0.1:{PORT}", "--token", TOK],
                    capture_output=True, text=True, errors="replace", timeout=30)
check("lj dialer --interactive is refused", _r.returncode != 0,
      str(_r.returncode))
check("it says the option is not known", "unrecognized" in
      (_r.stdout + _r.stderr) or "not recognized" in (_r.stdout + _r.stderr),
      (_r.stdout + _r.stderr)[-160:])

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

print("\n=== 6f. the README matches the code ===")
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


print("\n=== 6g. the GUI ===")
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

            # With the default host ("0.0.0.0", every interface) the
            # URL must carry an address a browser on another machine
            # can reach -- not 127.0.0.1, which only works on this
            # machine. That is the case a user hits: the default.
            #
            # The app's own token is kept: _started sets it, and the
            # page request below authenticates with it.
            _app.host_var.set("0.0.0.0")
            _app._started(_app.worker.srv, _app.token, "test")
            _gurl2 = _app.url_var.get()
            _ghost = _gurl2.split("://", 1)[1].split(":", 1)[0].split("/")[0]
            check("the GUI's url is not a loopback address",
                  _ghost not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"),
                  _gurl2)
            check("the GUI's url uses this machine's own address",
                  _ghost == L.local_addr(),
                  f"{_ghost} vs {L.local_addr()}")
            # put it back for the rest of the GUI checks
            _app.host_var.set("127.0.0.1")
            _app._started(_app.worker.srv, _app.token, "test")

            # the page is really served on that port
            _s, _b = get(_gport, "/", None)
            check("the GUI's listener serves the page", _s == 200, str(_s))

            # the window has no send box: the far side is a worker,
            # not a messenger
            check("the window has no send box", not hasattr(_app, "msg_var"),
                  "the send box is back")
            check("the window has no _send()", not hasattr(_app, "_send"),
                  "the send method is back")

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

            # Closing the window must release the port, and it must do
            # so BEFORE the window goes: the server is a thread in this
            # process, so the port would go with the process -- but the
            # window can close without the process exiting, and a server
            # left running in that gap keeps the port and answers the
            # NEXT launch with the OLD token. That looks exactly like
            # the new one failing.
            _app._start()
            _deadline = time.time() + 25
            while time.time() < _deadline:
                _app._drain()
                if (_app.worker and _app.worker.is_alive()
                        and _app.worker.srv is not None):
                    break
                time.sleep(0.2)
            _closport = int(_app.port_var.get())
            # The worker's stop is what releases the port; _on_close
            # also destroys the window, and the rest of the section
            # still needs a live one.
            _app.worker.stop()
            _s2 = socket.socket()
            _s2.settimeout(2)
            try:
                _s2.connect(("127.0.0.1", _closport))
                _still = True
            except OSError:
                _still = False
            finally:
                _s2.close()
            check("closing the window releases the port", not _still,
                  "something is still listening")

        _app.destroy()
        _root.destroy()
    except Exception as _e:
        check("the GUI works", False, f"{type(_e).__name__}: {_e}")
else:
    for _n in _gui_check_names():
        skip(_n, "no display")

# The listener is a thread in the GUI's own process, so the port goes
# when the process does -- normally. The paths that are NOT normal are
# the ones that matter:
#
#   * the window is closed but the process lives on
#   * the mainloop ends because of an exception
#
# In both, a server left running keeps the port and answers the next
# launch with the OLD token, which reads exactly like the new one
# failing. So the port has to be released HERE, not left to the
# process.
#
# These run in a child process, because what is being checked is what
# the port does after the process is gone -- which the parent can only
# see from outside. It needs a display too: it opens a window of its
# own.
print("\n=== 6h. the GUI takes the server down with it ===")
if _have_display:
    _GUI_CHILD = """
import os, sys, time
sys.path.insert(0, {root!r})
import lj.gui as G

KEEP = []
port = int(sys.argv[1])
mode = sys.argv[2]
app = G.App()
app.withdraw()
app.port_var.set(str(port))
app.host_var.set("127.0.0.1")
app._start()
deadline = time.time() + 25
while time.time() < deadline:
    app._drain()
    if app.worker and app.worker.is_alive() and app.token:
        break
    time.sleep(0.2)
print("LISTENING", flush=True)
if mode == "close":
    # The window is closed and the PROCESS LIVES ON. That is the case
    # that matters: the server is a daemon thread, so it does not keep
    # the process alive, but it DOES keep the socket open for as long
    # as the process lives. A server left behind in that gap holds the
    # port and answers the next launch with the OLD token -- which
    # reads exactly like the new one failing.
    #
    # The reference is kept deliberately: without it the App is
    # collected, the server object with it, and the socket is closed by
    # the garbage collector -- which would make this test pass for the
    # wrong reason.
    app._on_close()
    KEEP.append(app.worker)
    print("CLOSED", flush=True)
    time.sleep(3.0)
elif mode == "mainloop":
    # the mainloop ends because of an exception in a callback
    app._on_close()
    raise RuntimeError("boom")
elif mode == "killed":
    # the process is killed outright -- no cleanup runs at all. The
    # OS releases the port when the process dies, but Windows can
    # leave it in TIME_WAIT, and a port in TIME_WAIT refuses a new
    # bind for a moment. So this is about the next launch, not about
    # the port being held.
    os._exit(9)
"""

    def _gui_port_free(port, timeout=3.0):
        """Is the port free?

        The honest way to ask is the OS's own table: is anything
        LISTENING on this port? A connect is not the answer, because a
        socket that is bound but not accepting answers a connect with
        nothing at all -- the connect succeeds and then the read times
        out, which looks exactly like a firewall drop.

        That is not hypothetical. With the server left running and
        serve_forever() already returned, the socket was still bound:
        the connect SUCCEEDED, the read timed out, and the first
        version of this check -- which read the timeout as "free" --
        passed while the port was held.

        So: the OS's table. It says LISTEN, the port is held.
        """
        import subprocess as _sp
        _out = _sp.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | Measure-Object).Count"],
            capture_output=True, text=True, errors="replace").stdout.strip()
        if _out:
            return _out == "0"

        # No powershell (Linux): fall back to a connect. A refused
        # connect means free; a connect that succeeds means held.
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return False
        except ConnectionRefusedError:
            return True
        except OSError:
            return False        # a timeout is not a refusal
        finally:
            s.close()

    for _mode in ("close", "mainloop", "killed"):
        _gp = free_port()
        _src = tmp / f"gui_child_{_mode}.py"
        _src.write_text(_GUI_CHILD.format(root=str(HERE.parent)),
                        encoding="utf-8")
        _c = subprocess.Popen([PY, str(_src), str(_gp), _mode],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               text=True, errors="replace")
        _remember(_c)
        # The child announces that it is listening, and (for the
        # "close" mode) that the window is closed -- and then STAYS
        # ALIVE. So the port has to be free while the process is still
        # running: a server left behind in a live process keeps the
        # port and answers the next launch with the OLD token, which
        # reads exactly like the new one failing.
        _seen = ""
        _deadline = time.time() + 60
        while time.time() < _deadline:
            _line = _c.stdout.readline()
            if not _line:
                break
            _seen += _line
            if "LISTENING" in _seen and (
                    _mode == "killed" or _mode == "mainloop"
                    or "CLOSED" in _seen):
                break
        _cout = _seen
        check(f"the GUI child started a listener ({_mode})",
              "LISTENING" in _cout, _cout[-160:])

        # The port must be free NOW, with the child still running.
        #
        # Not "free a moment later": a wait loop turns this check into
        # a rubber stamp, because the socket is released by the garbage
        # collector a second or two after the window goes, whatever the
        # code does. The question is whether the CLOSE released it --
        # and that is answered the moment the window closes.
        _free = _gui_port_free(_gp)
        check(f"the port is free after the GUI exits ({_mode})",
              _free, "something is still listening on the port")

        if _mode == "killed":
            # the process is gone, so the port is the OS's business.
            # What matters is that the NEXT launch can bind it.
            _c.wait(timeout=20)
            _rebound = False
            _deadline = time.time() + 15
            while time.time() < _deadline:
                _s = socket.socket()
                try:
                    _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    _s.bind(("127.0.0.1", _gp))
                    _rebound = True
                    _s.close()
                    break
                except OSError:
                    _s.close()
                    time.sleep(0.3)
            check(f"the port can be bound after the GUI is killed ({_mode})",
                  _rebound, "the port is still held")
        else:
            _c.kill()
            try:
                _c.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # the names above are two per mode, and there are two modes, so
        # the section has four checks -- the name list has to say so
        # or the count is short on a headless machine.
else:
    for _n in _gui_exit_check_names():
        skip(_n, "no display")

print("\n=== 6i. the launcher opens the window when there is no console ===")
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
        skip("pythonw reports that it has no console",
          "no pythonw.exe next to this python")
    _probe.unlink(missing_ok=True)

print("\n=== 6j. every subcommand takes the dispatcher's calling convention ===")
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

# The far side is ONE command. The old "lj remote" must be gone, not
# quietly still working: a second name for the same thing is the thing
# this change was to remove.
_r = subprocess.run([PY, "-m", "lj", "remote"], capture_output=True,
                    text=True, timeout=60, errors="replace")
check("the dispatcher does not know lj remote",
      "unknown command" in (_r.stderr or ""), (_r.stderr or "")[-120:])

# and the one command has ONE mode: the worker. The --worker flag is
# gone, and it must not come back -- a flag that selects between a
# worker and a messenger is what this change removed.
_d = load("lj.dialer")
_help = _d.build_parser().format_help() or ""
check("lj dialer has no --worker flag", "--worker" not in _help,
      "the flag is back")
for _gone in ("-m", "--message", "--file", "--repeat", "--listen",
              "--show", "--interactive", "--worker"):
    check(f"lj dialer has no {_gone} flag", f" {_gone}" not in _help
          and f", {_gone}" not in _help, "the flag is back")

print("\n=== 7. the page can actually authenticate ===")
status, html = get(PORT3, "/")
check("page reads the token from the fragment", "location.hash" in html)
check("page stores it in a cookie", "document.cookie" in html)
check("page sends the X-LJ-Token header", "X-LJ-Token" in html)
check("page has no chat drawer", 'id="chatForm"' not in html
      and 'id="chatToggle"' not in html, "the chat is back")
check("page clears the fragment from the URL",
      "replaceState" in html, "token would stay in the address bar")

# and a page that did NOT set the cookie really is refused -- this is the
# case the v2.0.0 test missed by setting the cookie by hand
try:
    get(PORT3, "/cmd")
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
        # The total is what ran plus what was skipped: the README
        # quotes the number for a platform that runs everything, so a
        # platform that cannot run some of them has to add them back.
        _actual = len(PASS) + len(FAIL) + len(SKIPPED) + 1   # + this check
        # A checkout runs more checks than an installed copy: some of
        # them need the repository (the README at the root, the source
        # tree itself). So the README's number is the checkout's, and an
        # installed copy is allowed to run fewer.
        _in_checkout = (HERE.parent / "pyproject.toml").is_file()
        # The number is also platform-dependent: the 0600 assertion
        # needs POSIX modes (not Windows/NTFS), the GUI tests need a
        # display (not a headless Linux) and the pythonw check needs a
        # pythonw.exe next to the interpreter (not Linux). So the
        # README's number is the one for a checkout on the platform
        # that runs everything, and a platform that skips some is
        # allowed to run fewer -- the same rule as for an installed
        # copy.
        # The checks this platform cannot run are added back, so the
        # number is the same everywhere: the README quotes the count
        # for a platform that runs everything.
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
# The skips are part of the total: the README quotes the number of
# CHECKS, so a summary that says "195 passed" when 196 ran is
# confusing. The skip is a check this platform cannot run, not a
# failure and not a pass.
_total = len(PASS) + len(FAIL) + len(SKIPPED)
print(f"{_total} checks: {len(PASS)} passed, {len(FAIL)} failed"
      + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
if FAIL:
    for f in FAIL:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)
