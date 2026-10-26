"""A window around the lj listener.

The listener already does everything: it holds the port, serves the page,
hands out the work and prints a banner. This is the same server with a
face -- a start/stop button, the address to hand to the far side, and the
log.

It is a VIEW, not a second implementation. The HTTP server and the token
handling are lj.listen's, imported and driven here. If the two ever
disagreed about what a request means, the page and the window would show
different things -- so there is only one copy.

Nothing here is required to use lj: the command line still works, and
this file is not imported by it.
"""
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from tkinter import messagebox, ttk

# Where the icon assets may be: a checkout has them at the repository
# root, an installed copy has them beside the package.
_ASSET_DIRS = []
for _base in (os.path.dirname(os.path.abspath(__file__)),
              os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    _d = os.path.join(_base, "assets")
    if os.path.isdir(_d):
        _ASSET_DIRS.append(_d)

# The package has to be importable however lj was started: from a
# checkout (the parent of this file), or installed (already importable).
# Try the normal import first -- an installed copy needs no help -- and
# fall back to the checkout layout.
try:
    import lj.listen as L
except ImportError:                      # running from a source tree
    _PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    import lj.listen as L

APP_TITLE = "lj listener"


class ListenerThread(threading.Thread):
    """Runs lj.listen's server and reports what it does.

    The server blocks in serve_forever(), so it gets its own thread and
    everything else is told about it through a queue. The window's own
    after() loop drains that queue, which keeps all the tkinter calls on
    the main thread -- touching widgets from another thread is the usual
    way a tkinter program crashes at random.
    """

    def __init__(self, host, port, token_file, events=None):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.token_file = token_file
        self.events = events if events is not None else queue.Queue()
        self.srv = None
        self.error = None
        self.token = ""
        self._stopping = False

    def run(self):
        try:
            if self._stopping:
                return                  # asked to stop before we began
            self.token, note = L.load_or_create_token(self.token_file)
            L.Handler.token = self.token

            srv = L._bind_or_explain(self.host, self.port)
            if self._stopping:
                # stopped while we were binding
                srv.shutdown()
                srv.server_close()
                return
            self.srv = srv
            srv.daemon_threads = True

            # The same start-up proof the command line does: make sure
            # the port is really ours before telling anyone to use it.
            # The server has to be SERVING first -- nothing answers on
            # the port until serve_forever() runs, so probing before that
            # would always fail. So serve on a thread, probe, and if the
            # probe fails, take the server down again.
            t = threading.Thread(target=srv.serve_forever,
                                 kwargs={"poll_interval": 0.3}, daemon=True)
            t.start()

            if not L._serves_our_page(self.host, self.port):
                srv.shutdown()
                srv.server_close()
                self.error = ("another process is answering on this port")
                self.events.put(("error", self.error))
                return

            self.events.put(("started", (srv, self.token, note)))
            # the thread above owns serve_forever; this one just waits
            while t.is_alive():
                t.join(0.5)
        except SystemExit as e:
            self.error = str(e)
            self.events.put(("error", str(e)))
        except Exception as e:                       # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self.events.put(("error", self.error))

    def stop(self, wait: float = 5.0) -> bool:
        """Ask the server to stop, and wait until the port is free.

        If the worker has not got as far as binding yet, srv is None and
        there is nothing to shut down -- but the worker may be about to
        bind, so it is also told to give up.

        The wait matters. server_close() releases the socket, but the
        OS can hold the port a moment longer, and SO_REUSEADDR lets a
        new server bind it while the old one is still finishing. Then
        the old server keeps answering and the new one never gets a
        request, which looks like the start silently failed.

        Returns True if the port is free when this returns.
        """
        self._stopping = True
        srv = self.srv
        if srv is not None:
            try:
                srv.shutdown()          # returns once serve_forever() stopped
                srv.server_close()      # release the listening socket
            except Exception:
                pass
        return self._wait_port_free(wait)

    def _wait_port_free(self, timeout: float = 5.0) -> bool:
        """Wait until nothing is listening on our port any more."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = socket.socket()
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.host, self.port))
                s.close()
                return True
            except OSError:
                s.close()
                time.sleep(0.05)
        return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x560")
        self.minsize(640, 460)

        self.events = queue.Queue()
        self.worker = None
        self.token = ""
        self.address = ""

        self._build()
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ layout

    def _set_icon(self):
        """Put the app icon on the window and the taskbar.

        Tk wants a GIF or PPM; PNG works from Tk 8.6, which is what ships
        with Python 3.7 and later. If the file is missing -- an installed
        copy that does not ship the assets -- the window just gets the
        default icon, which is not worth an error.
        """
        for name in ("lj.ico", "icon-256.png", "icon-64.png"):
            for base in (_ASSET_DIRS):
                path = os.path.join(base, name)
                if not os.path.exists(path):
                    continue
                try:
                    if name.endswith(".ico"):
                        self.iconbitmap(path)
                    else:
                        img = tk.PhotoImage(file=path)
                        self.iconphoto(True, img)
                        self._icon_img = img        # keep a reference
                    return
                except Exception:
                    continue

    def _build(self):
        pad = 10
        outer = ttk.Frame(self, padding=pad)
        outer.pack(fill="both", expand=True)
        self._set_icon()

        # -- settings -------------------------------------------------
        top = ttk.LabelFrame(outer, text="Listener", padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Port").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value="10000")
        ttk.Entry(top, textvariable=self.port_var, width=8).grid(
            row=0, column=1, sticky="w", padx=(4, 12))

        ttk.Label(top, text="Bind").grid(row=0, column=2, sticky="w")
        self.host_var = tk.StringVar(value="0.0.0.0")
        ttk.Combobox(top, textvariable=self.host_var, width=12,
                     values=("0.0.0.0", "127.0.0.1")).grid(
            row=0, column=3, sticky="w", padx=(4, 12))

        self.btn = ttk.Button(top, text="Start", command=self._toggle)
        self.btn.grid(row=0, column=4, sticky="e", padx=(12, 0))
        top.columnconfigure(4, weight=1)

        # -- the address to hand over --------------------------------
        share = ttk.LabelFrame(outer, text="Hand this to the sender",
                               padding=8)
        share.pack(fill="x", pady=(pad, 0))

        self.url_var = tk.StringVar(value="not running")
        ttk.Entry(share, textvariable=self.url_var, state="readonly").pack(
            fill="x")

        row = ttk.Frame(share)
        row.pack(fill="x", pady=(6, 0))
        self.copy_btn = ttk.Button(row, text="Copy URL",
                                   command=self._copy_url, state="disabled")
        self.copy_btn.pack(side="left")
        self.open_btn = ttk.Button(row, text="Open page",
                                   command=self._open_page, state="disabled")
        self.open_btn.pack(side="left", padx=6)
        ttk.Button(row, text="New token",
                   command=self._new_token).pack(side="left")

        # -- log -----------------------------------------------------
        mid = ttk.Frame(outer)
        mid.pack(fill="both", expand=True, pady=(pad, 0))

        self.log = tk.Text(mid, wrap="word", height=14, state="disabled",
                           font=("Consolas", 9))
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ------------------------------------------------------------ events

    def _drain(self):
        """Move whatever the server thread said onto the screen."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "started":
                    self._started(*payload)
                elif kind == "error":
                    self._failed(payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _started(self, srv, token, note):
        self.token = token
        self._set_button("Stop")
        self.copy_btn.configure(state="normal")
        self.open_btn.configure(state="normal")

        host = self.host_var.get()
        # The address to put in the URL is the one the browser can
        # actually reach. When the listener is on every interface
        # ("0.0.0.0", the default), that is this machine's own address
        # on the path out -- not 127.0.0.1, which only works on this
        # machine, and the URL is the one a person pastes into a
        # browser that is somewhere else.
        if host in ("0.0.0.0", "::", ""):
            shown = L.local_addr()
        else:
            shown = host
        self.address = f"{shown}:{self.port_var.get()}"
        self.url_var.set(f"http://{self.address}/#token={token}")

        self._append(f"listening on {host}:{self.port_var.get()}  ({note})")
        self._append("the sender needs that URL and the token")

    def _failed(self, why):
        self._set_button("Start")
        self.copy_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.url_var.set("not running")
        self._append(f"FAILED: {why}")
        messagebox.showerror(APP_TITLE, why)

    def _set_button(self, text: str, enabled: bool = True) -> None:
        """Set the button's label AND whether it can be clicked.

        These have to be set together. The button is disabled while the
        listener starts, and setting only the text afterwards leaves it
        greyed out: it says "Stop" and cannot be clicked, which reads as
        a broken button rather than as a disabled one.
        """
        self.btn.configure(text=text)
        # configure(state=...) is the one that works both ways. The ttk
        # state() call sets the flag but does not clear a "disabled" that
        # configure() put there, so the button stayed greyed out.
        self.btn.configure(state="normal" if enabled else "disabled")

    def _toggle(self):
        if self.worker and self.worker.is_alive():
            self._stop()
        else:
            self._start()

    def _start(self):
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror(APP_TITLE, "the port has to be a number")
            return
        if not (1 <= port <= 65535):
            messagebox.showerror(APP_TITLE, "the port has to be 1-65535")
            return

        # A previous listener may still be holding the port. The stop is
        # asynchronous, so a Start that comes straight after it would
        # create a worker that cannot bind -- and the window would say
        # "Stop" with nothing behind it. Wait for the old one to be
        # gone and the port to be free first.
        if self.worker is not None and self.worker.is_alive():
            self._append("waiting for the previous listener to stop ...")
            self.worker.stop()
            deadline = time.time() + 15
            while time.time() < deadline and self.worker.is_alive():
                time.sleep(0.1)
            if self.worker.is_alive():
                self._append("the previous listener is still running")
                self._set_button("Start")
                return
            self._wait_port_free()

        self._append(f"starting on {self.host_var.get()}:{port} ...")
        # The button is NOT disabled while starting. Disabling it here
        # and re-enabling it from the worker's "started" event loses the
        # race when the two happen in the same event-loop turn: the
        # disable is applied last and the button is left greyed out,
        # saying "Stop" and refusing to be clicked. That is exactly what
        # a user reported.
        #
        # Instead the button stays live and _toggle is idempotent:
        # clicking Stop while the listener is still starting stops it.
        self._set_button("Stop")
        self.worker = ListenerThread(self.host_var.get(), port,
                                     L.TOKEN_FILE, self.events)
        self.worker.start()

    def _stop(self):
        self._append("stopping ...")
        was_running = bool(self.worker and self.worker.is_alive())
        if self.worker:
            # stop() closes the socket and waits for the port to be
            # free. The wait matters: the OS can hold the port a
            # moment after the socket is closed, and SO_REUSEADDR lets
            # a new server bind it while the old one is still
            # finishing. Then the old server keeps answering and the
            # new one never gets a request, which looks like the start
            # silently failed.
            freed = self.worker.stop()
            if not freed:
                self._append(f"port {self.port_var.get()} is still held "
                             f"by something")
        self._set_button("Start")
        self.copy_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.url_var.set("not running")
        # Say what happened. Without this the log stops at "stopping ..."
        # and the user cannot tell a finished stop from a hung one.
        if was_running:
            self._append("stopped")
        else:
            self._append("nothing was running")

    def _wait_port_free(self, timeout: float = 10.0) -> bool:
        """Wait until nothing is listening on our port any more.

        Returns False if the port is still held when the time runs out.

        The bind is on the ADDRESS the server was told to use, not on
        loopback: a server bound to "0.0.0.0" is not visible as a
        loopback bind, so waiting on 127.0.0.1 would report the port
        as free while it is still held.
        """
        port = int(self.port_var.get())
        host = self.host_var.get() or "0.0.0.0"
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = socket.socket()
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                s.close()
                return True
            except OSError:
                s.close()
                time.sleep(0.1)
        self._append(f"port {port} is still held by something")
        return False

    def _new_token(self):
        try:
            token, note = L.load_or_create_token(L.TOKEN_FILE, regen=True)
        except Exception as e:                       # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.token = token
        L.Handler.token = token
        self._append(f"new token: {token}  (the old URL stops working)")
        if self.worker and self.worker.is_alive():
            self.url_var.set(
                f"http://{self.address}/#token={token}")

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.url_var.get())
        self._append("copied the URL to the clipboard")

    def _open_page(self):
        webbrowser.open(self.url_var.get())

    def _append(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        # see("end") puts the CURSOR at the end, which is one past the
        # last character -- the last line can end up just below the
        # visible area. see("end-1c") scrolls to the last character
        # instead, so the newest line is actually on screen.
        self.log.see("end-1c")
        self.log.configure(state="disabled")

    def _on_close(self):
        """The window is closing: take the server down with it.

        The server is a thread in this process, so the port would go
        when the process does -- but the window can close without the
        process exiting (the mainloop returns, or an exception unwinds
        past it), and a server left running in that gap keeps the port
        and answers the NEXT launch with the OLD token. That looks
        exactly like the new one failing, so the port is released
        HERE rather than being left to the process.

        The wait is inside stop(): the OS can hold the port a moment
        after the socket is closed, and a new server that binds it
        during that window keeps getting the old server's answers.
        """
        if self.worker:
            self.worker.stop()
        self.destroy()


def main(argv=None) -> int:
    """Run the window.

    The dispatcher calls every subcommand's main() with the remaining
    arguments, so this has to accept them even though the window has no
    options -- a signature of main() with no parameters makes `lj gui`
    fail with a TypeError the moment it is run from an installed copy.

    The server is stopped in a finally, so it goes when the mainloop
    ends for ANY reason -- the window was closed, the user pressed
    ctrl-c, or a callback raised. Without that the listener can outlive
    the window inside a long-lived process, holding the port and
    answering the next launch with the old token.
    """
    if argv:
        print("lj gui takes no options", file=sys.stderr)
        return 2
    app = App()
    try:
        app.mainloop()
    finally:
        # _on_close already did this for the normal path; doing it again
        # is harmless and covers the paths that never reached it.
        try:
            if app.worker:
                app.worker.stop()
        except Exception:
            pass
        try:
            app.destroy()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
