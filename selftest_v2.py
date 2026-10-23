"""Self-test for the v2 framework. No test framework, stdlib only.

    python selftest_v2.py
"""
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    return cond


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load(name):
    s = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


L = load("lj_listen")
D = load("lj_dialer")


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
    print("  skip  token file is 0600   (no POSIX modes on this platform)")
c, _ = L.load_or_create_token(str(tf), regen=True)
check("regen-token changes it", c != a)

# ---------------------------------------------------------------- parsing

print("\n=== 2. target parsing ===")
check("host:port", D.split_target("1.2.3.4:9999") == ("1.2.3.4", 9999))
check("host only -> 10000", D.split_target("1.2.3.4") == ("1.2.3.4", 10000))
check("bare port -> localhost", D.split_target("8080") == ("127.0.0.1", 8080))
check("trailing colon", D.split_target("host:") == ("host", 10000))

# ---------------------------------------------------------------- server

print("\n=== 3. lj_listen serves ===")
PORT = free_port()
TOK = L.mint_token()
proc = subprocess.Popen([PY, str(HERE / "lj_listen.py"),
                         "--port", str(PORT), "--token-file", str(tmp / "t2"),
                         "--regen-token"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True)
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

print("\n=== 5. lj_dialer ===")
PORT2 = free_port()
TOK2 = L.mint_token()
p2 = subprocess.Popen([PY, str(HERE / "lj_listen.py"),
                       "--port", str(PORT2), "--token-file", str(tmp / "t3"),
                       "--regen-token"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
deadline = time.time() + 20
while time.time() < deadline:
    try:
        s = socket.create_connection(("127.0.0.1", PORT2), timeout=0.4)
        s.close(); break
    except OSError:
        time.sleep(0.3)
with open(tmp / "t3") as fh:
    TOK2 = fh.read().strip()

r = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{PORT2}", "--token", TOK2,
                    "-m", "from the dialer"], capture_output=True, text=True,
                   timeout=30)
check("dialer exits 0", r.returncode == 0, r.stdout[-200:] + r.stderr[-200:])
check("dialer reports 204", "204" in r.stdout, r.stdout[-200:])

status, body = get(PORT2, "/msg", TOK2)
check("message arrived", json.loads(body)["msgs"] == ["from the dialer"], body[:120])

r = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{PORT2}", "--token", "lj_bad",
                    "-m", "nope"], capture_output=True, text=True, timeout=30)
check("dialer with a bad token fails", r.returncode == 1, str(r.returncode))
check("dialer says the token was rejected",
      "token rejected" in r.stdout, r.stdout[-160:])

dead = free_port()
r = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{dead}", "--token", TOK2,
                    "-m", "x"], capture_output=True, text=True, timeout=30)
check("dialer to a dead port fails cleanly", r.returncode == 1)
check("dialer explains it cannot connect",
      "cannot reach" in r.stdout, r.stdout[-160:])

# stdin
r = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{PORT2}", "--token", TOK2],
                   input="piped text\n", capture_output=True, text=True,
                   timeout=30)
status, body = get(PORT2, "/msg", TOK2)
check("dialer reads stdin", json.loads(body)["msgs"] == ["piped text"], body[:120])

# ------------------------------------------------------------- two-way

print("\n=== 6. scope: messages only ===")
# A guard, not a feature: the endpoints below must stay message-shaped. If
# someone adds a raw/stream mode for tunnels, this is where it should fail.
_page = get(PORT, "/")[1]
_dialer_src = (HERE / "lj_dialer.py").read_text(encoding="utf-8")
_listen_src = (HERE / "lj_listen.py").read_text(encoding="utf-8")

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
check("the dialer has no stream/pty option",
      not any(f"--{o}" in _dialer_src
              for o in ("raw", "stream", "pty", "shell", "interactive")))
check("the listener has no stream/pty option",
      not any(f'"{o}"' in _listen_src
              for o in ("/raw", "/stream", "/ws", "/shell", "/exec")))
check("the scope note explains why", "SCOPE -- messages only" in _listen_src)

print("\n=== 6. two-way (v2.1.0) ===")
PORT3 = free_port()
TOK3 = L.mint_token()
p3 = subprocess.Popen([PY, str(HERE / "lj_listen.py"),
                       "--port", str(PORT3), "--token-file", str(tmp / "t4"),
                       "--regen-token"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
d = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{PORT3}", "--token", TOK3,
                    "--listen", "--once"], capture_output=True, text=True,
                   timeout=30)
check("dialer --listen exits 0", d.returncode == 0, d.stdout[-200:])
post(PORT3, "/msg", "browser says hi", TOK3)
d = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{PORT3}", "--token", TOK3,
                    "--listen", "--once"], capture_output=True, text=True,
                   timeout=30)
check("dialer --listen prints the browser's message",
      "browser says hi" in d.stdout, d.stdout[-200:])

d = subprocess.run([PY, str(HERE / "lj_dialer.py"),
                    "--target", f"127.0.0.1:{PORT3}", "--token", "lj_bad",
                    "--listen", "--once"], capture_output=True, text=True,
                   timeout=30)
check("dialer --listen with a bad token fails", d.returncode == 1)

# ------------------------------------------------- the page authenticates

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

p3.terminate()
try:
    p3.wait(timeout=8)
except subprocess.TimeoutExpired:
    p3.kill()

for p in (proc, p2):
    p.terminate()
    try:
        p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        p.kill()

print("\n" + "=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  -", f)
raise SystemExit(1 if FAIL else 0)
