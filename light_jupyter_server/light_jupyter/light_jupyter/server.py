"""HTTP + WebSocket server for light_jupyter.

Stack: Tornado (WebSocket + static files). Tornado is already in use on this
box (port 8895 runs TornadoServer/6.5.2), so it is a natural fit and needs no
compilation.

Token gate
----------
The URL is  http://host:port/#token=<TOKEN>  - the fragment never reaches the
server, so the page must hand it over itself. Flow:

  1. GET /              -> always 200, serves the shell HTML
  2. JS reads location.hash, stores the token in a cookie, opens
     /ws?token=...     -> server compares against LIGHT_JUPYTER_TOKEN
  3. Every /ws upgrade and every /api/* call re-checks the cookie.

A wrong token gets a 401 on the API and a close(4401) on the socket, which the
client renders as an explicit "bad token" screen rather than a blank page.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Optional

import tornado.web
import tornado.websocket
import tornado.gen

from .backend import FileSystem, Session
from .tokens import AuthThrottle, verify as verify_token

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


def make_token():
    """Backwards-compatible shim; the implementation lives in tokens.py."""
    from .tokens import make_token as _make
    return _make()


# ---------------------------------------------------------------- auth mixin

class TokenMixin:
    """Cookie/token gate shared by the API and the websocket.

    A 256-bit secret is only half a control -- the gate needs an attempt
    limit too, so failures are throttled per client IP with exponential
    backoff and a success clears the counter.
    """

    def client_ip(self) -> str:
        # X-Forwarded-For is only honoured when the server sits behind a proxy
        # we control; by default use the transport address, which cannot be
        # spoofed by a header.
        if self.application.settings.get("trust_proxy"):
            fwd = self.request.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.request.remote_ip or "unknown"

    def get_token(self) -> Optional[str]:
        expected = self.application.settings["token"]
        # 1. explicit query param (websocket connect)
        q = self.get_argument("token", None)
        if verify_token(expected, q):
            return q
        # 2. cookie (set by the page after reading the fragment)
        c = self.get_cookie("lj_token", None)
        if verify_token(expected, c):
            return c
        return None

    def require_token(self) -> bool:
        throttle: AuthThrottle = self.application.throttle
        ip = self.client_ip()

        wait = throttle.retry_after(ip)
        if wait > 0:
            self.set_status(429)
            self.set_header("Retry-After", str(int(wait) + 1))
            self.finish({"error": "too many failed attempts; retry later"})
            return False

        if self.get_token():
            throttle.success(ip)
            return True

        delay = throttle.fail(ip)
        self.set_status(401)
        self.set_header("X-Throttle-Delay", f"{delay:.1f}")
        self.finish({"error": "invalid or missing token"})
        return False


# ---------------------------------------------------------------- API

class ListHandler(TokenMixin, tornado.web.RequestHandler):
    def get(self):
        if not self.require_token():
            return
        fs: FileSystem = self.application.fs
        try:
            entries = fs.listdir(self.get_argument("path", "/"))
        except ValueError as e:
            self.set_status(400)
            self.finish({"error": str(e)})
            return
        self.finish({"entries": entries, "root": fs.root})


class ReadHandler(TokenMixin, tornado.web.RequestHandler):
    def get(self):
        if not self.require_token():
            return
        path = self.get_argument("path", "")
        try:
            text = self.application.fs.read_text(path)
        except Exception as e:
            self.set_status(400)
            self.finish({"error": f"{type(e).__name__}: {e}"})
            return
        self.finish({"path": path, "text": text})


class WriteHandler(TokenMixin, tornado.web.RequestHandler):
    def post(self):
        if not self.require_token():
            return
        body = json.loads(self.request.body or b"{}")
        path = body.get("path", "")
        text = body.get("text", "")
        try:
            n = self.application.fs.write_text(path, text)
        except Exception as e:
            self.set_status(400)
            self.finish({"error": f"{type(e).__name__}: {e}"})
            return
        self.finish({"ok": True, "bytes": n})


class MutateHandler(TokenMixin, tornado.web.RequestHandler):
    """mkdir / rename / delete / upload / download."""

    def post(self):
        if not self.require_token():
            return
        body = json.loads(self.request.body or b"{}")
        op = body.get("op", "")
        fs: FileSystem = self.application.fs
        try:
            if op == "mkdir":
                fs.mkdir(body["path"])
                self.finish({"ok": True})
            elif op == "rename":
                fs.rename(body["src"], body["dst"])
                self.finish({"ok": True})
            elif op == "delete":
                fs.delete(body["path"], recursive=bool(body.get("recursive")))
                self.finish({"ok": True})
            elif op == "upload":
                import base64
                data = base64.b64decode(body["data"])
                n = fs.upload_bytes(body["path"], data)
                self.finish({"ok": True, "bytes": n})
            elif op == "download":
                raw = fs.download_bytes(body["path"])
                import base64
                self.finish({"ok": True,
                             "name": os.path.basename(body["path"]),
                             "data": base64.b64encode(raw).decode()})
            else:
                self.set_status(400)
                self.finish({"error": f"unknown op {op!r}"})
        except Exception as e:
            self.set_status(400)
            self.finish({"error": f"{type(e).__name__}: {e}"})


class InfoHandler(TokenMixin, tornado.web.RequestHandler):
    def get(self):
        if not self.require_token():
            return
        s = self.application
        self.finish({
            "user": s.current_user,
            "root": s.fs.root,
            "sessions": len(s.sessions),
            "started": s.started,
            "uptime": round(time.time() - s.started, 1),
            "version": __import__("light_jupyter").__version__,
        })


# ---------------------------------------------------------------- websocket

class TerminalWS(TokenMixin, tornado.websocket.WebSocketHandler):
    """One websocket == one shell session.

    Client -> server messages:
      {"type":"input","data":"..."}      keystrokes
      {"type":"resize","cols":120,"rows":32}
      {"type":"replay"}                  ask for scrollback
    Server -> client:
      {"type":"output","data":"..."}     raw pty bytes (already decoded)
      {"type":"exit","code":N}
    """

    def check_origin(self, origin):
        # token in the query is the gate; same-origin check is not meaningful
        # for a LAN tool with varying hostnames
        return True

    def open(self):
        # initialise BEFORE any token check: Tornado can deliver on_close
        # without ever calling open() (e.g. the handshake is rejected), and
        # on_close reads these attributes.
        self.session = None
        self.pump_task = None

        if not self.get_token():
            self.close(4401, "invalid token")
            return
        app = self.application
        try:
            self.session = Session(user=app.current_user, cwd=app.fs.root)
        except Exception as e:
            self.write_message(json.dumps(
                {"type": "error", "error": f"cannot start shell: {e}"}))
            self.close(4500, "shell failed")
            return
        app.sessions[self.session.id] = self.session
        self.write_message(json.dumps({
            "type": "ready",
            "sid": self.session.id,
            "cols": self.session.cols,
            "rows": self.session.rows,
        }))
        self.pump_task = asyncio.ensure_future(self._pump())

    async def _pump(self):
        """PTY reader -> websocket. Runs on the event loop so it never blocks."""
        sess: Session = self.session
        loop = asyncio.get_event_loop()
        while sess and sess.alive() and not self.ws_connection is None:
            data = await loop.run_in_executor(None, sess.read, 65536, 0.08)
            if data:
                sess.append(data.decode("utf-8", "replace"))
                try:
                    self.write_message(json.dumps(
                        {"type": "output", "data": data.decode("utf-8", "replace")}))
                except tornado.websocket.WebSocketClosedError:
                    break
            await asyncio.sleep(0.01)
        code = sess.proc.poll() if sess else None
        try:
            self.write_message(json.dumps({"type": "exit", "code": code}))
        except tornado.websocket.WebSocketClosedError:
            pass
        self.close()

    def on_message(self, message):
        if self.session is None:
            return
        try:
            msg = json.loads(message)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "input":
            self.session.write(msg.get("data", ""))
        elif kind == "resize":
            self.session.resize(int(msg.get("cols", 120)),
                                int(msg.get("rows", 32)))
        elif kind == "replay":
            self.write_message(json.dumps(
                {"type": "output", "data": self.session.scrollback()}))
        elif kind == "run":
            self.session.write(msg.get("cmd", "") + "\n")

    def on_close(self):
        if self.pump_task:
            self.pump_task.cancel()
        if self.session:
            self.session.close()
            self.application.sessions.pop(self.session.id, None)


# ---------------------------------------------------------------- pages

class IndexHandler(tornado.web.RequestHandler):
    """Always serves the shell; the token is validated client-side over /ws."""

    def get(self):
        self.render("index.html", version=__import__("light_jupyter").__version__)


def make_app(token: str, root: str, user: str, port: int,
             trust_proxy: bool = False,
             throttle_threshold: int = 5) -> tornado.web.Application:
    fs = FileSystem(root)
    app = tornado.web.Application(
        [
            (r"/", IndexHandler),
            (r"/ws", TerminalWS),
            (r"/api/info", InfoHandler),
            (r"/api/list", ListHandler),
            (r"/api/read", ReadHandler),
            (r"/api/write", WriteHandler),
            (r"/api/fs", MutateHandler),
            (r"/static/(.*)", tornado.web.StaticFileHandler,
             {"path": STATIC, "default_filename": "app.js"}),
        ],
        template_path=STATIC,
        static_path=STATIC,
        debug=False,
    )
    app.settings["token"] = token
    app.settings["trust_proxy"] = trust_proxy
    app.fs = fs
    app.sessions = {}
    app.current_user = user
    app.started = time.time()
    app.port = port
    app.throttle = AuthThrottle(threshold=throttle_threshold)
    return app