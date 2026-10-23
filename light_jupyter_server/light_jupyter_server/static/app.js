/* light_jupyter_server front-end
 *
 * Token flow: the fragment (#token=...) never reaches the server, so the page
 * reads it here, stashes it in a cookie, and hands it to /ws on connect.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const API = "";                       // same origin

let TOKEN = "";
let WS = null;
let TERM = null;              // xterm.js Terminal, null when it failed to load
let CWD = "/";
let OPEN_PATH = null;
let EDITABLE = false;         // editor is read-only until the user asks to edit
let DIRTY = false;            // unsaved edits

// ---------------------------------------------------------------- token gate

function tokenFromHash() {
  const m = /[#&]token=([^&]+)/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : "";
}

function setCookie(name, value, days) {
  const exp = new Date(Date.now() + days * 864e5).toUTCString();
  document.cookie = `${name}=${encodeURIComponent(value)}; expires=${exp}; path=/; SameSite=Lax`;
}

function api(path, opts) {
  return fetch(API + path, opts).then(async (r) => {
    if (r.status === 401) throw new Error("bad token");
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  });
}

function postJSON(path, obj) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  });
}

function gate(msg, bad) {
  $("gate-msg").textContent = msg || "";
  $("gate-msg").style.color = bad ? "var(--bad)" : "var(--dim)";
}

// ---------------------------------------------------------------- boot

function enterApp() {
  $("gate").classList.add("hidden");
  $("app").classList.remove("hidden");
  setupTerminal();
  wireXtermInput();
  setEditable(false);
  connectWS();
  refreshFiles();
  api("/api/info").then((i) => {
    $("info").textContent = `${i.user} · ${i.root} · up ${i.uptime}s`;
  }).catch(() => {});
}

function boot() {
  let t = tokenFromHash();
  if (!t) t = decodeURIComponent((document.cookie.match(/lj_token=([^;]+)/) || [])[1] || "");
  if (t) {
    TOKEN = t;
    setCookie("lj_token", t, 7);
    // clear the fragment so the token stops living in the address bar
    history.replaceState(null, "", location.pathname);
    enterApp();
  } else {
    $("gate").classList.remove("hidden");
    $("token-input").focus();
  }
}

$("gate-go").onclick = tryToken;
$("token-input").onkeydown = (e) => { if (e.key === "Enter") tryToken(); };

function tryToken() {
  const v = $("token-input").value.trim();
  if (!v) { gate("token required", true); return; }
  TOKEN = v;
  // cheap validation: /api/info answers 401 for a bad token
  fetch(API + "/api/info?token=" + encodeURIComponent(v))
    .then((r) => {
      if (r.status === 401) { gate("invalid token", true); TOKEN = ""; return; }
      setCookie("lj_token", v, 7);
      location.hash = "token=" + encodeURIComponent(v);
      enterApp();
    })
    .catch(() => gate("server unreachable", true));
}

// ---------------------------------------------------------------- websocket

function setConn(cls, text) {
  $("conn").className = "pill " + cls;
  $("conn").textContent = text;
}

function connectWS() {
  if (WS) { try { WS.close(); } catch (e) {} }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  WS = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);

  WS.onopen = () => setConn("on", "connected");
  WS.onclose = (e) => {
    setConn("off", e.code === 4401 ? "bad token" : "disconnected");
    if (e.code === 4401) {
      $("app").classList.add("hidden");
      $("gate").classList.remove("hidden");
      gate("invalid token", true);
    } else {
      setTimeout(connectWS, 2000);       // auto-retry
    }
  };
  WS.onerror = () => setConn("off", "error");
  WS.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (m.type === "output") appendTerm(m.data);
    else if (m.type === "ready") {
      $("term").dataset.sid = m.sid;
      wsSend({ type: "resize", cols: termCols(), rows: termRows() });
      wsSend({ type: "replay" });
    } else if (m.type === "exit") {
      appendTerm(`\r\n\x1b[33m[process exited: ${m.code}]\x1b[0m\r\n`);
    } else if (m.type === "error") {
      appendTerm(`\r\n\x1b[31m[${m.error}]\x1b[0m\r\n`);
    }
  };
}

// Tell the shell how big the terminal is, using the real grid dimensions.
function termCols() { return TERM ? TERM.cols : 120; }
function termRows() { return TERM ? TERM.rows : 32; }

function syncTermSize() {
  if (!TERM || !WS || WS.readyState !== WebSocket.OPEN) return;
  wsSend({ type: "resize", cols: TERM.cols, rows: TERM.rows });
}

function wsSend(obj) {
  if (WS && WS.readyState === WebSocket.OPEN) WS.send(JSON.stringify(obj));
}

/* Terminal: xterm.js, not a <pre> full of raw escape bytes.
 *
 * A <pre> cannot emulate a terminal: the shell moves the cursor backwards with
 * CSI sequences to erase what it just printed, emits SGR colour codes, and
 * treats DEL (0x7f) as backspace while BS (0x08) means something else
 * entirely. Handing that to a text node either shows literal "^[[31m" or
 * leaves stale characters behind. xterm.js implements the terminal model, so
 * backspace, colour, cursor movement and full-screen TUI apps all behave.
 */
function setupTerminal() {
  if (!window.Terminal) {
    termUnavailable();
    return;
  }
  TERM = new window.Terminal({
    convertEol: true,
    cursorBlink: true,
    fontSize: 13,
    fontFamily: 'ui-monospace, "Cascadia Mono", "JetBrains Mono", Consolas, monospace',
    scrollback: 10000,
    theme: {
      background: "#171a21",
      foreground: "#d8dee9",
      cursor: "#6aa9ff",
      selectionBackground: "#2a2f3a",
    },
  });
  TERM.open($("term"));
  TERM.focus();
}

function termUnavailable() {
  const el = $("term");
  el.classList.add("term-fallback");
  el.textContent = "xterm.js failed to load — terminal degraded " +
                   "(no colour, no cursor control). Reload after the file is in place.";
  $("keys").classList.remove("hidden");   // fall back to the key buttons
}

function appendTerm(text) {
  if (TERM) { TERM.write(text); return; }
  const el = $("term");
  el.textContent += text;
  el.scrollTop = el.scrollHeight;
}

$("term").addEventListener("click", () => TERM && TERM.focus());
$("clear").onclick = () => TERM ? TERM.clear() : ($("term").textContent = "");

/* Keystrokes.
 *
 * xterm.js owns the keyboard entirely: it already handles DEL (0x7f) vs BS
 * (0x08), Ctrl-combinations, IME composition and clipboard paste in the way a
 * real terminal expects. Forwarding raw keydown events on top of that would
 * double every character. So when xterm is present we hook its onData -- the
 * single stream of "bytes the terminal wants to send" -- and skip the manual
 * keydown path below. The on-screen key buttons still work either way.
 */
function wireXtermInput() {
  if (!TERM) return false;
  TERM.onData((data) => wsSend({ type: "input", data }));
  return true;
}

// Fallback for when xterm.js is missing: translate keydown by hand.
document.addEventListener("keydown", (e) => {
  if (TERM) return;                       // xterm handles it
  const t = e.target;
  if (t === $("editor") || t === $("token-input")) return;
  if (t !== $("term") && t !== document.body && !t.classList.contains("term")) return;

  let data = null;
  if (e.ctrlKey && e.key.length === 1)
    data = String.fromCharCode(e.key.toUpperCase().charCodeAt(0) - 64);
  else if (e.altKey && e.key.length === 1) data = "\x1b" + e.key;
  else if (e.key === "Enter") data = "\r";
  else if (e.key === "Backspace") data = "\x7f";
  else if (e.key === "Tab") data = "\t";
  else if (e.key === "Escape") data = "\x1b";
  else if (e.key === "ArrowUp") data = "\x1b[A";
  else if (e.key === "ArrowDown") data = "\x1b[B";
  else if (e.key === "ArrowRight") data = "\x1b[C";
  else if (e.key === "ArrowLeft") data = "\x1b[D";
  else if (e.key === "PageUp") data = "\x1b[5~";
  else if (e.key === "PageDown") data = "\x1b[6~";
  else if (e.key === "Home") data = "\x1b[H";
  else if (e.key === "End") data = "\x1b[F";
  else if (e.key === "Delete") data = "\x1b[3~";
  else if (e.key.length === 1) data = e.key;

  if (data !== null) { e.preventDefault(); wsSend({ type: "input", data }); }
});

document.querySelectorAll("#keys button").forEach((b) => {
  b.onclick = () => wsSend({ type: "input", data: b.dataset.k });
});

$("clear").onclick = () => { $("term").textContent = ""; };

// ---------------------------------------------------------------- files

async function refreshFiles(path) {
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await api("/api/list" + q);
    CWD = path || "/";
    $("cwd").textContent = CWD;
    const list = $("listing");
    list.innerHTML = "";
    data.entries.forEach((e) => list.appendChild(rowFor(e)));
  } catch (err) {
    $("cwd").textContent = "error: " + err.message;
  }
}

function rowFor(e) {
  const row = document.createElement("div");
  row.className = "row" + (e.dir ? " dir" : "");
  if (e.path === OPEN_PATH) row.classList.add("sel");

  const ico = document.createElement("span");
  ico.className = "ico";
  ico.textContent = e.dir ? "▸" : (e.symlink ? "↪" : "·");

  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = e.name;

  const sz = document.createElement("span");
  sz.className = "sz";
  sz.textContent = e.dir ? "" : fmtSize(e.size);

  row.append(ico, nm, sz);
  row.onclick = () => (e.dir ? refreshFiles(e.path) : openFile(e.path));
  row.oncontextmenu = (ev) => { ev.preventDefault(); fileMenu(ev, e); };
  return row;
}

function fmtSize(n) {
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(1) + "K";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + "M";
  return (n / 1073741824).toFixed(1) + "G";
}

/* Editor: read-only until the user explicitly asks to edit.
 *
 * Opening a file must never be able to damage it by accident -- a stray
 * keystroke while browsing would be written straight back on save. So the
 * textarea carries the readonly attribute, save stays disabled until edit mode
 * is on, and closing with unsaved changes asks first.
 */
function setEditable(on) {
  EDITABLE = on && !!OPEN_PATH;
  const ta = $("editor");
  ta.readOnly = !EDITABLE;
  $("save").disabled = !EDITABLE;
  const pill = $("editor-mode");
  pill.textContent = EDITABLE ? "editing" : "read-only";
  pill.className = "pill " + (EDITABLE ? "on" : "readonly");
  $("edit-toggle").textContent = EDITABLE ? "🔒 done" : "✎ edit";
  if (EDITABLE) ta.focus();
}

$("edit-toggle").onclick = () => {
  if (!OPEN_PATH) { alert("open a file first"); return; }
  if (!EDITABLE && DIRTY) {
    if (!confirm("discard unsaved edits and keep viewing read-only?")) return;
  }
  if (EDITABLE && DIRTY) {
    if (!confirm("leave edit mode with unsaved edits? save first if you want them.")) return;
    DIRTY = false;
  }
  setEditable(!EDITABLE);
};

$("editor").addEventListener("input", () => { DIRTY = EDITABLE; });

$("close-file").onclick = () => {
  if (DIRTY && !confirm("discard unsaved edits and close?")) return;
  OPEN_PATH = null;
  DIRTY = false;
  setEditable(false);
  $("editor").value = "";
  $("open-name").textContent = "(no file)";
  refreshFiles(CWD);
};

async function openFile(path) {
  try {
    const data = await api("/api/read?path=" + encodeURIComponent(path));
    if (DIRTY && !confirm("discard unsaved edits?")) return;
    OPEN_PATH = path;
    DIRTY = false;
    $("editor").value = data.text;
    $("open-name").textContent = path;
    // always land in read-only: opening is a viewing action
    setEditable(false);
    refreshFiles(CWD);
  } catch (err) {
    $("open-name").textContent = "cannot open: " + err.message;
  }
}

async function fileMenu(ev, e) {
  const act = prompt(
    `${e.path}\n\n` +
    `[d] rename   [x] delete   [y] copy path`,
    "d"
  );
  if (!act) return;
  if (act === "d") {
    const nn = prompt("new name:", e.name);
    if (!nn) return;
    const dst = CWD === "/" ? "/" + nn : CWD + "/" + nn;
    await postJSON("/api/fs", { op: "rename", src: e.path, dst }).catch((x) => alert(x.message));
  } else if (act === "x") {
    if (!confirm(`delete ${e.path}?`)) return;
    await postJSON("/api/fs", { op: "delete", path: e.path, recursive: e.dir })
      .catch((x) => alert(x.message));
  } else if (act === "y") {
    navigator.clipboard && navigator.clipboard.writeText(e.path);
  }
  refreshFiles(CWD);
}

$("up").onclick = () => {
  if (CWD === "/") return refreshFiles("/");
  const parent = CWD.replace(/\/[^/]*$/, "") || "/";
  refreshFiles(parent);
};
$("refresh").onclick = () => refreshFiles(CWD);

$("mkdir").onclick = async () => {
  const name = prompt("new directory name:");
  if (!name) return;
  await postJSON("/api/fs", { op: "mkdir", path: CWD === "/" ? "/" + name : CWD + "/" + name })
    .catch((e) => alert(e.message));
  refreshFiles(CWD);
};

$("upload").onclick = () => $("filepick").click();
$("filepick").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const buf = await f.arrayBuffer();
  let bin = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 8192) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
  }
  const dest = CWD === "/" ? "/" + f.name : CWD + "/" + f.name;
  try {
    await postJSON("/api/fs", { op: "upload", path: dest, data: btoa(bin) });
    refreshFiles(CWD);
  } catch (err) { alert(err.message); }
  e.target.value = "";
};

$("save").onclick = async () => {
  if (!OPEN_PATH) return alert("no file open");
  if (!EDITABLE) return alert("read-only — press ✎ edit first");
  try {
    await postJSON("/api/write", { path: OPEN_PATH, text: $("editor").value });
    DIRTY = false;
    refreshFiles(CWD);
  } catch (err) { alert(err.message); }
};

$("download").onclick = async () => {
  if (!OPEN_PATH) return alert("no file open");
  try {
    const r = await postJSON("/api/fs", { op: "download", path: OPEN_PATH });
    const bin = atob(r.data);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([arr], { type: "application/octet-stream" }));
    const a = document.createElement("a");
    a.href = url; a.download = r.name; a.click();
    URL.revokeObjectURL(url);
  } catch (err) { alert(err.message); }
};

// Ctrl+S saves
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    if (EDITABLE) $("save").click();
    else alert("read-only — press ✎ edit first");
  }
});

// keep the shell's idea of the window size in step with the real grid
window.addEventListener("resize", () => {
  if (TERM && TERM.fit) { try { TERM.fit(); } catch (err) {} }
  syncTermSize();
});

boot();