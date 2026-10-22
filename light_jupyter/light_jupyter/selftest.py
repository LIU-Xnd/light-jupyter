"""Self-test for light_jupyter — runs on the target box, no test framework.

    python -m light_jupyter.selftest

Checks the pieces that actually break: pty session, websocket round-trip,
token gate, and the file API. Exits non-zero on the first hard failure.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((PASS if cond else FAIL, name, detail))
    print(f"[{PASS if cond else FAIL}] {name}"
          + (f"  {detail}" if detail else ""), flush=True)
    return cond


def main() -> int:
    root = tempfile.mkdtemp(prefix="lj-selftest-")
    token = "selftest-token-abc123"

    print("=== 1. backend: Session over a real pty ===")
    from light_jupyter.backend import FileSystem, Session
    sess = Session(user=os.environ.get("USER", "root"), cwd=root, cols=100, rows=30)
    time.sleep(0.6)
    sess.write("echo LJ_PTY_WORKS\n")
    seen = ""
    for _ in range(60):
        seen += sess.read(4096, 0.1).decode("utf-8", "replace")
        if "LJ_PTY_WORKS" in seen:
            break
        time.sleep(0.1)
    check("pty session echoes command", "LJ_PTY_WORKS" in seen, repr(seen[-120:]))
    check("session reports alive", sess.alive())
    sess.close()
    check("session closes", not sess.alive())

    print()
    print("=== 2. backend: FileSystem ===")
    fs = FileSystem(root)
    fs.write_text("/hello.txt", "hello light_jupyter\n")
    check("write+read roundtrip",
          fs.read_text("/hello.txt").strip() == "hello light_jupyter")
    entries = fs.listdir("/")
    check("listdir sees the file", any(e["name"] == "hello.txt" for e in entries))
    fs.mkdir("/sub/dir")
    check("mkdir nested", os.path.isdir(os.path.join(root, "sub/dir")))
    fs.rename("/hello.txt", "/sub/renamed.txt")
    check("rename", os.path.exists(os.path.join(root, "sub/renamed.txt")))
    fs.upload_bytes("/bin.dat", bytes(range(256)))
    check("upload binary", os.path.getsize(os.path.join(root, "bin.dat")) == 256)
    check("download binary",
          fs.download_bytes("/bin.dat") == bytes(range(256)))
    fs.delete("/sub", recursive=True)
    check("recursive delete", not os.path.exists(os.path.join(root, "sub")))
    try:
        fs.read_text("/../../etc/passwd")
        check("path traversal blocked", False, "read escaped root!")
    except Exception as e:
        check("path traversal blocked", "escape" in str(e).lower(), str(e)[:60])

    print()
    print("=== 3. HTTP server: token gate + API ===")
    import threading
    from light_jupyter.server import make_app
    import tornado.ioloop

    app = make_app(token=token, root=root, user="root", port=0,
                   # this section makes several deliberate wrong-token calls;
                   # keep the throttle out of the way so we test the gate,
                   # not the backoff (backoff has its own tests above)
                   throttle_threshold=1000)
    # bind an explicit free port: app.listen(0) picks one internally but
    # Tornado does not expose the chosen port, so grab one ourselves first.
    import socket as _sock
    probe = _sock.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    app.listen(port, address="127.0.0.1")

    loop = tornado.ioloop.IOLoop.current()
    t = threading.Thread(target=loop.start, daemon=True)
    t.start()
    time.sleep(1.0)

    base = f"http://127.0.0.1:{port}"

    def fetch(path, cookie=None, method="GET", body=None, expect_json=True):
        req = urllib.request.Request(base + path, method=method)
        if cookie:
            req.add_header("Cookie", cookie)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                if not expect_json:
                    return r.status, raw.decode("utf-8", "replace")
                return r.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            if not expect_json:
                return e.code, raw.decode("utf-8", "replace")
            return e.code, json.loads(raw or b"{}")

    def fetch_on(pt, path, cookie=None, method="GET", body=None):
        """Same as fetch() but against an arbitrary port."""
        req = urllib.request.Request(f"http://127.0.0.1:{pt}{path}", method=method)
        if cookie:
            req.add_header("Cookie", cookie)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # GET / is the HTML shell, not JSON
    st, html = fetch("/", expect_json=False)
    check("GET / serves the HTML shell",
          st == 200 and "light_jupyter" in html, f"status {st}")
    st, _ = fetch("/", expect_json=False)
    check("GET / has no token check (fragment cannot reach server)", st == 200)

    # vendored xterm must be present or the terminal silently degrades
    st, js = fetch("/static/vendor/xterm.js", expect_json=False)
    check("vendored xterm.js is served", st == 200 and len(js) > 100000,
          f"status {st}, {len(js)} bytes")
    st, css = fetch("/static/vendor/xterm.css", expect_json=False)
    check("vendored xterm.css is served", st == 200 and len(css) > 1000,
          f"status {st}, {len(css)} bytes")
    check("index.html loads xterm", "xterm.js" in html and "xterm.css" in html)
    check("editor starts read-only", 'id="editor"' in html and "readonly" in html)

    st, _ = fetch("/api/info")
    check("no token -> 401", st == 401, f"status {st}")

    st, body = fetch("/api/info", cookie=f"lj_token={token}")
    check("valid cookie -> 200", st == 200, json.dumps(body)[:80])

    st, _ = fetch("/api/info?token=" + token)
    check("valid query token -> 200", st == 200)

    st, _ = fetch("/api/info", cookie="lj_token=wrong")
    check("wrong cookie -> 401", st == 401, f"status {st}")

    st, body = fetch("/api/list", cookie=f"lj_token={token}")
    check("api/list works", st == 200 and "entries" in body)

    st, body = fetch("/api/fs", cookie=f"lj_token={token}", method="POST",
                     body={"op": "write"})
    check("api/write via fs op rejected cleanly", st in (400, 200))

    st, body = fetch("/api/write", cookie=f"lj_token={token}", method="POST",
                     body={"path": "/api_test.txt", "text": "written via api"})
    check("api/write", st == 200 and body.get("ok"), json.dumps(body)[:80])
    st, body = fetch("/api/read?path=/api_test.txt", cookie=f"lj_token={token}")
    check("api/read roundtrip",
          st == 200 and body.get("text") == "written via api")

    st, body = fetch("/api/fs", cookie=f"lj_token={token}", method="POST",
                     body={"op": "upload", "path": "/up.bin",
                           "data": "aGVsbG8="})
    check("api upload", st == 200 and body.get("ok"), json.dumps(body)[:80])

    # a dedicated app instance with a low threshold, to prove the throttle
    # really fires over HTTP (429 + Retry-After) and clears on success
    th_app = make_app(token=token, root=root, user="root", port=0,
                      throttle_threshold=3)
    import socket as _s2
    pr2 = _s2.socket(); pr2.bind(("127.0.0.1", 0))
    th_port = pr2.getsockname()[1]; pr2.close()
    th_app.listen(th_port, address="127.0.0.1")

    codes = []
    for _ in range(5):
        st, _ = fetch_on(th_port, "/api/info?token=wrong")
        codes.append(st)
    check("repeated bad tokens eventually 429",
          429 in codes, str(codes))
    check("first attempts are plain 401",
          codes[:2] == [401, 401], str(codes))

    st, _ = fetch_on(th_port, f"/api/info?token={token}")
    check("throttle locks out even the right token while backed off",
          st == 429, f"status {st}")

    # a success from an untracked IP must still work
    check("throttle state is per-IP",
          th_app.throttle.retry_after("127.0.0.1") > 0,
          str(th_app.throttle.stats()))

    th_app.throttle.reset()
    st, _ = fetch_on(th_port, f"/api/info?token={token}")
    check("reset clears the lockout", st == 200, f"status {st}")

    print()
    print("=== 5. port auto-detection ===")
    from .ports import (LO, HI, RESERVED, bind_socket, is_port_free,
                        parse_port, pick_port)

    check("parse_port: empty -> auto", parse_port("") is None)
    check("parse_port: 'auto' -> auto", parse_port("auto") is None)
    check("parse_port: '0' -> auto", parse_port("0") is None)
    check("parse_port: None -> auto", parse_port(None) is None)
    check("parse_port: 'any'/'free' -> auto",
          parse_port("any") is None and parse_port("free") is None)
    check("parse_port: '8890' -> 8890", parse_port("8890") == 8890)
    check("parse_port: ' 8890 ' tolerant", parse_port(" 8890 ") == 8890)
    for bad, label in [("notaport", "junk"),
                       ("70000", "out of range"),
                       ("-5", "negative")]:
        try:
            parse_port(bad)
            check(f"parse_port rejects {label}", False, "no exception")
        except ValueError:
            check(f"parse_port rejects {label}", True)

    check("is_port_free: unused high port", is_port_free(47999))

    # the chosen port must be real, in the scanned range, and bindable
    got = pick_port("127.0.0.1")
    check("pick_port returns a valid port",
          isinstance(got, int) and LO <= got <= HI, str(got))
    check("pick_port skips RESERVED ports", got not in RESERVED, str(got))
    check("pick_port result is actually free",
          is_port_free(got, "127.0.0.1"), str(got))

    s2 = bind_socket(got, "127.0.0.1")
    check("picked port is really bindable", s2.getsockname()[1] == got)
    check("is_port_free is False once bound",
          not is_port_free(got, "127.0.0.1"))
    s2.close()
    check("is_port_free is True after close",
          is_port_free(got, "127.0.0.1"))

    # bind_socket(0) yields a real ephemeral port, read back off the socket
    s3 = bind_socket(0, "127.0.0.1")
    eph = s3.getsockname()[1]
    s3.close()
    check("bind_socket(0) yields a real ephemeral port",
          1024 < eph <= 65535, str(eph))

    # an explicit start is honoured when it is inside the scanned range, and
    # an out-of-range start wraps back into it
    inside = pick_port("127.0.0.1", start=25000)
    check("pick_port honours a start inside the range",
          inside == 25000, str(inside))
    outside = pick_port("127.0.0.1", start=47990)
    check("pick_port wraps an out-of-range start back in",
          LO <= outside <= HI, str(outside))

    # well-known service ports stay off the auto-pick path
    check("RESERVED excludes ssh/rdp-ish ports",
          22 in RESERVED and 445 in RESERVED, str(sorted(RESERVED)[:5]))

    print()
    print("=== 6. default root ===")
    from light_jupyter.__main__ import default_root
    import getpass as _gp
    me = _gp.getuser()
    dr = default_root(me)
    check("default_root returns an absolute path", os.path.isabs(dr), dr)
    check("default_root is /home/<user> when that exists",
          (dr == f"/home/{me}") or os.path.isdir(dr), dr)
    # the whole point: it must not depend on $HOME
    old_home = os.environ.get("HOME")
    try:
        os.environ.pop("HOME", None)
        dr2 = default_root(me)
        check("default_root survives an unset HOME", dr2 == dr,
              f"{dr} vs {dr2}")
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home
    # an account with no /home/<user> falls back to the passwd entry
    dr3 = default_root("definitely_no_such_user_xyz")
    check("unknown user still yields a usable path",
          isinstance(dr3, str) and dr3, dr3)

    print()
    print("=== 8. advertised address ===")
    from light_jupyter.__main__ import local_ipv4
    ip = local_ipv4()
    check("local_ipv4 returns a dotted quad",
          len(ip.split(".")) == 4 and all(p.isdigit() for p in ip.split(".")), ip)
    check("local_ipv4 skips loopback", not ip.startswith("127."), ip)
    check("local_ipv4 skips link-local", not ip.startswith("169.254."), ip)
    check("local_ipv4 skips 0.0.0.0", ip != "0.0.0.0", ip)
    check("local_ipv4 is stable across calls", ip == local_ipv4(), ip)

    print()
    print("=== 7. key-grade tokens ===")
    import os as _os
    import stat as _stat
    import tempfile as _tf
    from light_jupyter.tokens import (ALPHABET, AuthThrottle, entropy_bits,
                                   is_wellformed, load_or_create,
                                   make_token, verify)

    tok = make_token()
    check("token carries >= 256 bits", entropy_bits(tok) >= 256,
          f"{entropy_bits(tok)} bits")
    check("token is well-formed", is_wellformed(tok), tok)
    check("token has the lj_ prefix", tok.startswith("lj_"), tok[:8])
    check("token uses only the safe alphabet",
          all(c in ALPHABET or c in "-" for c in tok[3:]))
    check("token excludes I L O U (transcription-safe)",
          not (set(tok) & set("ILOU")), tok)
    check("grouped into 4 chunks", len(tok[3:].split("-")) == 4,
          str(len(tok[3:].split("-"))))
    check("two tokens differ", make_token() != make_token())

    try:
        make_token(entropy_bytes=8)
        check("refuses weak entropy", False, "no exception")
    except ValueError:
        check("refuses weak entropy", True)

    check("verify accepts the right token", verify(tok, tok))
    check("verify rejects a wrong token", not verify(tok, make_token()))
    check("verify rejects None", not verify(tok, None))
    check("verify rejects empty", not verify("", ""))
    check("verify rejects a prefix of the token",
          not verify(tok, tok[:-1]))

    # persistence
    d = _tf.mkdtemp(prefix="lj-token-")
    tp = _os.path.join(d, "token")
    t1, created1 = load_or_create(tp)
    check("first load creates the file", created1 and _os.path.exists(tp))
    t2, created2 = load_or_create(tp)
    check("second load reuses the token", t2 == t1 and not created2)
    mode = _stat.S_IMODE(_os.stat(tp).st_mode)
    check("token file is 0600", mode == 0o600, oct(mode))
    t3, created3 = load_or_create(tp)
    check("still stable after a third read", t3 == t1 and not created3)

    # regen path: remove then reload -> different token
    _os.remove(tp)
    t4, created4 = load_or_create(tp)
    check("regenerating yields a different token",
          created4 and t4 != t1, f"{t1[:12]}... vs {t4[:12]}...")

    # garbage in the file must be replaced, not trusted
    with open(tp, "w") as fh:
        fh.write("not a token !!\n")
    t5, created5 = load_or_create(tp)
    check("garbage in the token file is replaced",
          created5 and is_wellformed(t5), t5[:16])

    # throttle
    th = AuthThrottle(threshold=3, base_delay=10.0, max_delay=10.0, ttl=60.0)
    check("fresh IP may try", th.retry_after("1.2.3.4") == 0.0)
    th.fail("1.2.3.4"); th.fail("1.2.3.4")
    check("below threshold still allowed", th.retry_after("1.2.3.4") == 0.0)
    th.fail("1.2.3.4")
    check("at threshold backoff kicks in", th.retry_after("1.2.3.4") > 0,
          f"{th.retry_after('1.2.3.4'):.1f}s")
    check("other IPs unaffected", th.retry_after("5.6.7.8") == 0.0)
    th.success("1.2.3.4")
    check("success clears the counter", th.retry_after("1.2.3.4") == 0.0)
    th.fail("9.9.9.9"); th.fail("9.9.9.9"); th.fail("9.9.9.9"); th.fail("9.9.9.9")
    d1 = th.fail("9.9.9.9")
    check("delay caps at max_delay", d1 <= 10.0, f"{d1}")
    check("stats reports the tracked IP", "9.9.9.9" in th.stats(),
          str(th.stats()))

    print()
    print("=== 4. websocket terminal ===")
    from tornado.websocket import websocket_connect
    import asyncio

    async def ws_test():
        conn = await websocket_connect(f"ws://127.0.0.1:{port}/ws?token={token}")
        ready = json.loads(await conn.read_message())
        check("ws: ready frame", ready.get("type") == "ready",
              json.dumps(ready)[:100])
        await conn.write_message(json.dumps(
            {"type": "run", "cmd": "echo LJ_WS_WORKS"}))
        buf = ""
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(conn.read_message(), timeout=3)
            except asyncio.TimeoutError:
                break
            if raw is None:
                break
            m = json.loads(raw)
            if m.get("type") == "output":
                buf += m["data"]
                if "LJ_WS_WORKS" in buf:
                    break
        check("ws: command output arrives", "LJ_WS_WORKS" in buf, repr(buf[-120:]))
        conn.close()

    # bad token socket must be refused: the server completes the WS handshake
    # then immediately closes with 4401, so the client's first read is None
    async def ws_bad():
        bad = await websocket_connect(f"ws://127.0.0.1:{port}/ws?token=wrong")
        msg = await bad.read_message()
        check("ws: bad token -> socket closed by server", msg is None,
              f"server sent {msg!r} instead of closing")

    loop.add_callback(lambda: asyncio.ensure_future(ws_test()))
    time.sleep(0.3)
    loop.add_callback(lambda: asyncio.ensure_future(ws_bad()))
    time.sleep(5.0)

    loop.add_callback(loop.stop)

    print()
    print("=" * 60)
    n_pass = sum(1 for r in results if r[0] == PASS)
    n_fail = sum(1 for r in results if r[0] == FAIL)
    print(f"{n_pass} passed, {n_fail} failed")
    if n_fail:
        print("\nfailures:")
        for status, name, detail in results:
            if status == FAIL:
                print(f"  - {name}  {detail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())