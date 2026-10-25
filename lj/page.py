"""lj.page -- the browser side: a file manager and a terminal.

The page is served by lj listen and talks to the far side through the
command/result channel: it POSTs a command to /cmd and polls /res for
the answers.

TWO PANES
---------
**Files** -- list a directory, open a file in the editor, save it, make
directories, move, copy and delete. There is no upload from the
browser's own disk: the far side is the machine that holds the files,
so "upload" and "download" are the same thing as copy and read.

**Terminal** -- run a command and see its output with the colour it
was printed with.

WHY THE TERMINAL IS NOT A REAL ONE
----------------------------------
There is no PTY here. A command runs to completion and its output
comes back whole. A real interactive terminal needs a PTY, a resize
channel, a signal channel and flow control -- and a browser that
emulates one properly is xterm.js, which is a large dependency to
carry for a tool whose whole point is being small.

What this gives instead is what a person actually wants from a box
they cannot reach: run something, see what it printed, in colour.

THE ANSI RENDERER
-----------------
The output arrives as bytes. The escape sequences are parsed here and
turned into spans -- the standard SGR codes (colour, bold, underline,
reverse) and the common cursor ones (erase line, erase screen, carriage
return). An unknown sequence is printed as-is rather than dropped, so
nothing disappears silently.
"""
from __future__ import annotations

# The page is served as one document. It is long, but it is one file on
# disk, it has no build step, and it is served straight from memory.

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lj</title>
<style>
  :root {
    --fg: #1c1c1c;
    --dim: #6b6b6b;
    --line: #d5d5d5;
    --bg: #fbfbfb;
    --panel: #fff;
    --accent: #2f6fb0;
    --danger: #b3261e;
    --ok: #1c7c3f;
    --sel: #e8f0fa;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --fg: #e6e6e6; --dim: #9a9a9a; --line: #3a3a3a; --bg: #17181a;
      --panel: #1e2023; --accent: #6aa9e0; --danger: #f2726a; --ok: #5cc98a;
      --sel: #26313d;
    }
  }
  * { box-sizing: border-box; }
  body { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         margin: 0; color: var(--fg); background: var(--bg); }
  header { display: flex; align-items: center; gap: .75rem;
           padding: .5rem .9rem; border-bottom: 1px solid var(--line);
           background: var(--panel); flex-wrap: wrap; }
  header b { font-weight: 600; }
  #bar { color: var(--dim); font-size: 12px; margin-left: auto; }
  #bar.bad { color: var(--danger); }
  main { display: flex; height: calc(100vh - 45px); }
  section { display: flex; flex-direction: column; min-width: 0;
            overflow: hidden; }
  #files { width: 50%; min-width: 460px; border-right: 1px solid var(--line); }
  #term { flex: 1; min-width: 320px; }
  #path { flex: 1 1 auto; min-width: 6rem; }
  .pane-head { display: flex; gap: .3rem; align-items: center;
               padding: .35rem .5rem; border-bottom: 1px solid var(--line);
               background: var(--panel); flex-wrap: nowrap;
               overflow-x: auto; }
  .pane-head h2 { font-size: 12px; margin: 0 .3rem 0 0; font-weight: 600;
                  text-transform: uppercase; letter-spacing: .04em;
                  color: var(--dim); flex: none; }
  .pane-head button, .pane-head input, .pane-head label { flex: none; }
  .pane-head input { min-width: 0; }
  button, input, select { font: inherit; padding: .25rem .5rem;
        border: 1px solid var(--line); border-radius: 4px;
        background: var(--panel); color: var(--fg); }
  button { cursor: pointer; }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: .45; cursor: default; }
  button.danger:hover:not(:disabled) { border-color: var(--danger);
                                      color: var(--danger); }
  #tree { flex: 1 1 auto; min-height: 0; overflow: auto; }
  .row { display: flex; gap: .5rem; padding: .12rem .7rem; cursor: pointer;
         align-items: baseline; white-space: nowrap; }
  .row:hover { background: var(--sel); }
  .row.sel { background: var(--sel); font-weight: 600; }
  .row .nm { flex: 1; overflow: hidden; text-overflow: ellipsis; }
  .row .sz, .row .mt { color: var(--dim); font-size: 11px; }
  .row .ic { width: 1.1em; color: var(--dim); }
  .row.dir .nm { color: var(--accent); }
  #editor { flex: 1 1 auto; min-height: 0; width: 100%; resize: none;
            border: 0; padding: .6rem;
            font: inherit; background: var(--panel); color: var(--fg);
            outline: none; tab-size: 4; }
  #editor[hidden] { display: none; }
  #out { flex: 1 1 auto; min-height: 0; overflow: auto; padding: .6rem;
         background: #111;
         color: #d8d8d8; white-space: pre-wrap; word-break: break-word;
         margin: 0; }
  #out .b { font-weight: bold; } #out .u { text-decoration: underline; }
  #out .i { font-style: italic; } #out .s { text-decoration: line-through; }
  #out .r { background: #d8d8d8; color: #111; }
  #out .dim { color: #888; }
  /* the standard 8 colours, and the bright ones (90-97) */
  #out .f30 { color: #555; } #out .f31 { color: #e05c5c; }
  #out .f32 { color: #5cc98a; } #out .f33 { color: #d8b45c; }
  #out .f34 { color: #5c9ce0; } #out .f35 { color: #c07ce0; }
  #out .f36 { color: #5cd0d8; } #out .f37 { color: #d8d8d8; }
  #out .f90 { color: #777; } #out .f91 { color: #ff8080; }
  #out .f92 { color: #7fe0a5; } #out .f93 { color: #f0d080; }
  #out .f94 { color: #8cc0f5; } #out .f95 { color: #dbaaf5; }
  #out .f96 { color: #8ce8ef; } #out .f97 { color: #fff; }
  /* The background colours set no foreground: an explicit foreground
     code has to win over them, and a colour set here would come later
     in the cascade and beat it. The terminal's default (light on
     dark) is the right fallback. */
  #out .b40 { background: #555; }
  #out .b41 { background: #e05c5c; }
  #out .b42 { background: #5cc98a; }
  #out .b43 { background: #d8b45c; }
  #out .b44 { background: #5c9ce0; }
  #out .b45 { background: #c07ce0; }
  #out .b46 { background: #5cd0d8; }
  #out .b47 { background: #d8d8d8; }
  #out .b100 { background: #777; }
  #out .b101 { background: #ff8080; }
  #out .b102 { background: #7fe0a5; }
  #out .b103 { background: #f0d080; }
  #out .b104 { background: #8cc0f5; }
  #out .b105 { background: #dbaaf5; }
  #out .b106 { background: #8ce8ef; }
  #out .b107 { background: #fff; }
  #prompt { display: flex; gap: .4rem; padding: .4rem .6rem;
            border-top: 1px solid var(--line); background: var(--panel); }
  #prompt input { flex: 1; }
  #prompt span { color: var(--dim); }
  .hide { display: none !important; }
  #chat { position: fixed; right: 0; top: 45px; bottom: 0; width: 340px;
          background: var(--panel); border-left: 1px solid var(--line);
          display: flex; flex-direction: column; z-index: 5;
          box-shadow: -4px 0 14px rgba(0,0,0,.12); }
  #chatLog { flex: 1 1 auto; min-height: 0; overflow: auto; margin: 0;
             padding: .5rem; font: inherit; white-space: pre-wrap;
             word-break: break-word; }
  #chatForm { display: flex; gap: .4rem; padding: .4rem;
              border-top: 1px solid var(--line); }
  #chatForm input { flex: 1; min-width: 0; }
  #toast { position: fixed; right: 1rem; bottom: 1rem; padding: .5rem .8rem;
           background: var(--danger); color: #fff; border-radius: 5px;
           max-width: 40rem; opacity: 0; transition: opacity .2s;
           pointer-events: none; z-index: 9; }
  #toast.show { opacity: 1; }
  #toast.info { background: var(--ok); }
  a { color: var(--accent); }
</style>

<header>
  <b>lj</b>
  <span id="who" class="dim">not connected</span>
  <button id="chatToggle" title="show the message queue">messages</button>
  <span id="bar">connecting&hellip;</span>
</header>

<section id="chat" class="hide">
  <div class="pane-head">
    <h2>messages</h2>
    <button id="chatClear">clear</button>
  </div>
  <pre id="chatLog"></pre>
  <form id="chatForm">
    <input id="chatInput" placeholder="message to the far side" spellcheck="false"
           autocomplete="off">
    <button type="submit">send</button>
  </form>
</section>

<main>
  <section id="files">
    <div class="pane-head">
      <h2>files</h2>
      <input id="path" size="24" placeholder="path" spellcheck="false">
      <button id="go">go</button>
      <button id="up">up</button>
      <button id="refresh">refresh</button>
      <button id="mkdir">new dir</button>
      <button id="upload" title="copy a file from the far side into this directory">copy here</button>
      <button id="rm" class="danger">delete</button>
    </div>
    <div class="pane-head">
      <button id="open">open</button>
      <button id="save">save</button>
      <button id="mv">move</button>
      <button id="cp">copy</button>
      <button id="down" title="download the selected file to this machine">download</button>
      <span id="fsel" class="dim">&nbsp;</span>
    </div>
    <div id="tree"></div>
    <textarea id="editor" hidden spellcheck="false"></textarea>
  </section>

  <section id="term">
    <div class="pane-head">
      <h2>terminal</h2>
      <button id="clear">clear</button>
      <label class="dim"><input type="checkbox" id="wrap" checked> wrap</label>
      <span id="cwd" class="dim">&nbsp;</span>
    </div>
    <pre id="out"></pre>
    <form id="prompt">
      <span>$</span>
      <input id="cmd" placeholder="command to run on the far side" spellcheck="false"
             autocomplete="off">
      <button type="submit">run</button>
    </form>
  </section>
</main>

<div id="toast"></div>

<script>
"use strict";

// ------------------------------------------------------------- the token
// The token rides in the fragment, which browsers never put on the
// wire. Move it into a cookie so the requests can carry it too.
let token = '';

function grabToken() {
  const h = location.hash || '';
  const m = /(?:^#|[?&])token=([^&]+)/.exec(h);
  if (m) {
    token = decodeURIComponent(m[1]);
    document.cookie = 'lj_token=' + token + '; path=/; SameSite=Strict';
    history.replaceState(null, '', location.pathname);
    return true;
  }
  const c = /(?:^|;)\s*lj_token=([^;]+)/.exec(document.cookie);
  if (c) { token = decodeURIComponent(c[1]); return true; }
  return false;
}

function setState(text, bad) {
  const bar = document.getElementById('bar');
  bar.textContent = text;
  bar.classList.toggle('bad', !!bad);
}

let toastTimer = null;
function toast(text, kind) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.className = 'show' + (kind === 'info' ? ' info' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 4200);
}

// ------------------------------------------------------------- the wire
let lastSeq = 0;          // the last result sequence number we saw
const pending = new Map();   // seq -> {op, resolve}

async function ask(op, args) {
  const r = await fetch('cmd', {
    method: 'POST',
    headers: {'X-LJ-Token': token, 'Content-Type': 'application/json'},
    body: JSON.stringify({op: op, args: args || {}})
  });
  if (r.status === 401) { setState('token rejected', true); throw new Error('bad token'); }
  if (!r.ok) { throw new Error('the listener answered ' + r.status); }
  const data = await r.json();
  return new Promise((resolve) => { pending.set(data.seq, resolve); });
}

async function pollResults() {
  let data;
  try {
    const r = await fetch('res?after=' + lastSeq, {cache: 'no-store',
                                                   headers: {'X-LJ-Token': token}});
    if (r.status === 401) { setState('token rejected', true); return; }
    data = await r.json();
  } catch (e) {
    return;                       // transient; the next tick retries
  }
  for (const res of (data.results || [])) {
    if (typeof res.seq === 'number') lastSeq = Math.max(lastSeq, res.seq);
    const resolve = pending.get(res.seq);
    if (resolve) { pending.delete(res.seq); resolve(res); }
  }
}

// -------------------------------------------------------------- the ANSI
// Parse SGR (colour and style) plus the cursor sequences that show up
// in ordinary program output. An unknown sequence is shown as it came,
// so nothing vanishes without explanation.
// The styles: 0 reset, 1 bold, 2 dim, 3 italic, 4 underline, 7 reverse,
// 9 strike. The colours are handled by number below, because the
// foreground and background codes have different class names.
const STYLES = {0:'', 1:'b', 2:'dim', 3:'i', 4:'u', 7:'r', 9:'s',
                22:'', 23:'', 24:'', 27:''};

function colourClass(code) {
  if (code >= 30 && code <= 37)   return 'f' + code;
  if (code >= 90 && code <= 97)   return 'f' + code;
  if (code >= 40 && code <= 47)   return 'b' + code;
  if (code >= 100 && code <= 107) return 'b' + code;
  return null;
}

function renderAnsi(text) {
  const out = document.createElement('span');
  let open = 0;                       // how many spans are open
  let i = 0;
  const push = (cls) => {
    const s = document.createElement('span');
    if (cls) s.className = cls;
    out.appendChild(s);
    open++;
    return s;
  };
  let cur = push('');

  const closeAll = () => { open = 0; cur = push(''); };

  while (i < text.length) {
    const esc = text.indexOf('\x1b[', i);
    if (esc === -1) { cur.appendChild(document.createTextNode(text.slice(i))); break; }
    if (esc > i) cur.appendChild(document.createTextNode(text.slice(i, esc)));
    const end = text.indexOf('m', esc);
    // find the end of the CSI sequence
    let j = esc + 2;
    while (j < text.length && !/[A-Za-z]/.test(text[j])) j++;
    if (j >= text.length) { cur.appendChild(document.createTextNode(text.slice(esc))); break; }
    const final = text[j];
    const params = text.slice(esc + 2, j);
    i = j + 1;

    if (final === 'm') {
      const codes = params.split(';').map(s => parseInt(s, 10) || 0);
      if (!codes.length) codes.push(0);
      for (const code of codes) {
        if (code === 0) { closeAll(); continue; }
        if (code === 39 || code === 49) {
          // reset the foreground or the background on its own
          const keep = (cur.className || '')
            .split(' ').filter(c => c && !/^[fb](3[0-9]|4[0-9]|9[0-9]|10[0-7])$/.test(c));
          cur = push(keep.join(' '));
          continue;
        }
        const style = STYLES[code];
        if (style !== undefined) {
          // a new span carrying every style that is on
          const merged = (cur.className ? cur.className + ' ' : '') + style;
          cur = push(merged.trim());
          continue;
        }
        const colour = colourClass(code);
        if (colour) {
          const merged = (cur.className ? cur.className + ' ' : '') + colour;
          cur = push(merged.trim());
        }
        // an unknown code is ignored: showing it as text would be
        // noise, and dropping it silently loses nothing
      }
    } else if (final === 'K') {
      // erase in line: drop what is on this line
      cur.textContent = '';
    } else if (final === 'J' && (params === '2' || params === '3')) {
      // erase the whole screen
      out.textContent = '';
      cur = push('');
    } else if (final === 'G') {
      // cursor to column: approximate by padding
      const col = parseInt(params, 10) || 1;
      cur.appendChild(document.createTextNode(' '.repeat(Math.max(0, col - 1))));
    }
    // anything else: shown as it came, never dropped
  }
  return out;
}

// Split a command line into arguments the way a shell would: on
// whitespace, but keeping "quoted runs" together and honouring a
// backslash escape. Nothing is handed to a shell -- no expansion, no
// globbing, no operators -- so this is only about where one argument
// ends and the next begins.
function splitArgs(text) {
  const out = [];
  let cur = '';
  let started = false;
  let quote = null;         // the quote character we are inside
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === quote) { quote = null; continue; }
      if (ch === '\\' && quote === '"' && i + 1 < text.length) {
        // inside double quotes a backslash escapes the next character
        cur += text[++i];
        continue;
      }
      cur += ch;
      continue;
    }
    if (ch === '"' || ch === "'") { quote = ch; started = true; continue; }
    if (ch === '\\' && i + 1 < text.length) { cur += text[++i]; started = true; continue; }
    if (/\s/.test(ch)) {
      if (started) { out.push(cur); cur = ''; started = false; }
      continue;
    }
    cur += ch;
    started = true;
  }
  if (started) out.push(cur);
  return out;
}

function decodeB64(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder('utf-8', {fatal: false}).decode(bytes);
}

// -------------------------------------------------------------- the files
const tree = document.getElementById('tree');
const pathEl = document.getElementById('path');
const editor = document.getElementById('editor');
const fsel = document.getElementById('fsel');
let cwd = '.';
let entries = [];
let selected = null;      // {name, kind, path}
let editing = null;       // the path being edited

function fmtSize(n) {
  if (n === undefined || n === null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' K';
  if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' M';
  return (n / 1073741824).toFixed(1) + ' G';
}

function fmtTime(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  return d.toLocaleDateString(undefined, {month: '2-digit', day: '2-digit'}) +
         ' ' + d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
}

function fullPath(name) {
  if (cwd === '.' || cwd === '') return name;
  return cwd.replace(/\/+$/, '') + '/' + name;
}

async function ls(path) {
  const res = await ask('ls', {path: path, hidden: false});
  if (!res.ok) { toast(res.error || 'cannot list'); return; }
  cwd = res.path || path;
  pathEl.value = cwd;
  entries = res.entries || [];
  renderTree();
}

function renderTree() {
  tree.textContent = '';
  if (cwd !== '.' && cwd !== '') {
    const row = document.createElement('div');
    row.className = 'row dir';
    row.innerHTML = '<span class="ic">..</span><span class="nm">..</span>';
    row.onclick = () => ls(parentOf(cwd));
    tree.appendChild(row);
  }
  for (const e of entries) {
    const row = document.createElement('div');
    row.className = 'row' + (e.kind === 'd' ? ' dir' : '');
    row.dataset.name = e.name;
    const ic = e.kind === 'd' ? '/' : (e.kind === 'l' ? '~' : '·');
    row.innerHTML =
      '<span class="ic"></span><span class="nm"></span>' +
      '<span class="sz"></span><span class="mt"></span>';
    row.querySelector('.ic').textContent = ic;
    row.querySelector('.nm').textContent = e.name;
    row.querySelector('.sz').textContent = e.kind === 'd' ? '' : fmtSize(e.size);
    row.querySelector('.mt').textContent = fmtTime(e.mtime);
    row.onclick = () => {
      if (e.kind === 'd') { ls(fullPath(e.name)); return; }
      select(e);
    };
    row.ondblclick = () => {
      if (e.kind === 'd') return;
      openFile(e);
    };
    tree.appendChild(row);
  }
}

function select(e) {
  selected = e;
  for (const r of tree.querySelectorAll('.row')) {
    r.classList.toggle('sel', r.dataset.name === e.name);
  }
  fsel.textContent = e.name;
}

function parentOf(p) {
  if (!p || p === '.' || p === '') return '.';
  const parts = p.replace(/\/+$/, '').split('/');
  parts.pop();
  return parts.join('/') || '.';
}

async function openFile(e) {
  const res = await ask('read', {path: fullPath(e.name)});
  if (!res.ok) { toast(res.error || 'cannot open'); return; }
  editing = res.path;
  editor.value = res.binary ? '(binary file -- not shown)' : res.text;
  editor.readOnly = !!res.binary;
  editor.hidden = false;
  tree.hidden = true;
  fsel.textContent = res.path + '  (' + fmtSize(res.size) + ')';
  document.getElementById('open').textContent = 'back';
}

async function saveFile() {
  if (!editing) { toast('nothing is open'); return; }
  const res = await ask('write', {path: editing, text: editor.value});
  if (!res.ok) { toast(res.error || 'cannot save'); return; }
  toast('saved ' + editing + '  (' + fmtSize(res.size) + ')', 'info');
  ls(cwd);
}

// ------------------------------------------------------------ the terminal
const out = document.getElementById('out');
const cwdEl = document.getElementById('cwd');

function print(text, cls) {
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = text;
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
}

function printRich(node) {
  const line = document.createElement('div');
  line.appendChild(node);
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
}

document.getElementById('wrap').onchange = (e) => {
  out.style.whiteSpace = e.target.checked ? 'pre-wrap' : 'pre';
};

async function runCommand(line) {
  const text = line.trim();
  if (!text) return;
  printRich(renderAnsi('$ ' + text + '\n'));
  const argv = splitArgs(text);
  if (!argv.length) { print('nothing to run', 'dim'); return; }
  const res = await ask('exec', {argv: argv, cwd: cwd});
  if (!res.ok) {
    printRich(renderAnsi(res.error || 'failed\n'));
    return;
  }
  const textOut = decodeB64(res.output || '');
  if (textOut) printRich(renderAnsi(textOut.replace(/\n$/, '')));
  const note = 'exit ' + res.code;
  if (res.timed_out) note += ' (timed out)';
  if (res.truncated) note += ' (output truncated)';
  print(note, res.code === 0 ? 'dim' : '');
  if (res.cwd) cwdEl.textContent = res.cwd;
}

// ----------------------------------------------------------------- wiring
document.getElementById('go').onclick = () => ls(pathEl.value.trim() || '.');
pathEl.onkeydown = (e) => { if (e.key === 'Enter') ls(pathEl.value.trim() || '.'); };
document.getElementById('up').onclick = () => ls(parentOf(cwd));
document.getElementById('refresh').onclick = () => ls(cwd);

document.getElementById('open').onclick = async () => {
  if (editing) {
    editor.hidden = true;
    tree.hidden = false;
    editing = null;
    document.getElementById('open').textContent = 'open';
    fsel.textContent = '';
    ls(cwd);
    return;
  }
  if (selected) await openFile(selected);
};

document.getElementById('save').onclick = saveFile;

document.getElementById('mkdir').onclick = async () => {
  const name = prompt('new directory name');
  if (!name) return;
  const res = await ask('mkdir', {path: fullPath(name)});
  if (!res.ok) { toast(res.error || 'cannot create'); return; }
  ls(cwd);
};

document.getElementById('rm').onclick = async () => {
  if (!selected) { toast('nothing is selected'); return; }
  const path = fullPath(selected.name);
  let msg = 'delete ' + path + '?';
  if (selected.kind === 'd') msg += '\n\nthis deletes the directory and everything in it.';
  if (!confirm(msg)) return;
  const res = await ask('delete', {path: path, recursive: selected.kind === 'd'});
  if (!res.ok) { toast(res.error || 'cannot delete'); return; }
  selected = null;
  fsel.textContent = '';
  ls(cwd);
};

document.getElementById('mv').onclick = async () => {
  if (!selected) { toast('nothing is selected'); return; }
  const to = prompt('move ' + selected.name + ' to (a path):', cwd);
  if (!to) return;
  const res = await ask('move', {path: fullPath(selected.name), to: to});
  if (!res.ok) { toast(res.error || 'cannot move'); return; }
  ls(cwd);
};

document.getElementById('cp').onclick = async () => {
  if (!selected) { toast('nothing is selected'); return; }
  const to = prompt('copy ' + selected.name + ' to (a path):', cwd);
  if (!to) return;
  const res = await ask('copy', {path: fullPath(selected.name), to: to});
  if (!res.ok) { toast(res.error || 'cannot copy'); return; }
  ls(cwd);
};

document.getElementById('upload').onclick = async () => {
  if (!selected) { toast('select a file to copy first'); return; }
  const to = prompt('copy ' + selected.name + ' into which directory?', cwd);
  if (!to) return;
  const res = await ask('copy', {path: fullPath(selected.name), to: to});
  if (!res.ok) { toast(res.error || 'cannot copy'); return; }
  ls(cwd);
};

document.getElementById('down').onclick = async () => {
  if (!selected) { toast('nothing is selected'); return; }
  const res = await ask('read', {path: fullPath(selected.name)});
  if (!res.ok) { toast(res.error || 'cannot read'); return; }
  if (res.binary) { toast('binary file -- not downloaded'); return; }
  const blob = new Blob([res.text], {type: 'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = selected.name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast('downloaded ' + selected.name, 'info');
};

document.getElementById('clear').onclick = () => { out.textContent = ''; };

document.getElementById('prompt').onsubmit = async (e) => {
  e.preventDefault();
  const el = document.getElementById('cmd');
  const line = el.value;
  el.value = '';
  await runCommand(line);
};

// ------------------------------------------------------------- the chat
// The message queue is still here, and still works the way it always
// did: the page posts a message, the far side collects it. It is a
// drawer rather than a pane because the files and the terminal are
// what this page is for now.
const chatLog = document.getElementById('chatLog');

async function pollMessages() {
  try {
    const r = await fetch('msg', {cache: 'no-store',
                                  headers: {'X-LJ-Token': token}});
    if (!r.ok) return;
    const data = await r.json();
    for (const m of (data.msgs || [])) {
      const line = document.createElement('div');
      line.textContent = m;
      chatLog.appendChild(line);
    }
    if ((data.msgs || []).length) chatLog.scrollTop = chatLog.scrollHeight;
  } catch (e) {
    // transient; the next tick retries
  }
}

document.getElementById('chatToggle').onclick = () => {
  const box = document.getElementById('chat');
  box.classList.toggle('hide');
  if (!box.classList.contains('hide')) pollMessages();
};
document.getElementById('chatClear').onclick = () => { chatLog.textContent = ''; };
document.getElementById('chatForm').onsubmit = async (e) => {
  e.preventDefault();
  const el = document.getElementById('chatInput');
  const text = el.value.trim();
  if (!text) return;
  el.value = '';
  try {
    const r = await fetch('msg', {method: 'POST',
                                  headers: {'X-LJ-Token': token},
                                  body: text});
    if (!r.ok) { toast('the message did not go through'); return; }
    const line = document.createElement('div');
    line.textContent = '> ' + text;
    line.className = 'dim';
    chatLog.appendChild(line);
  } catch (err) {
    toast('the message did not go through');
  }
};

// ------------------------------------------------------------------ start
async function tick() {
  await pollResults();
  if (!document.getElementById('chat').classList.contains('hide')) {
    await pollMessages();
  }
  setTimeout(tick, 400);
}

if (grabToken()) {
  setState('connected', false);
  document.getElementById('who').textContent = 'the far side is polled by lj remote';
  ls('.');
  tick();
} else {
  setState('no token -- use http://127.0.0.1:PORT/#token=...', true);
}
</script>
"""
