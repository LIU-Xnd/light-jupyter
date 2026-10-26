"""Build the lj MSI, with an installer the user can actually use.

``pyappdist build`` produces a working installer, but its UI is
Windows' bare progress bar: it fills up and the window closes, with
nothing to click and nowhere to choose a folder.

pyappdist only grows a UI when a licence is configured, and then it
hardcodes the ``WixUI_Minimal`` set -- one page, no folder choice. The
stock ``WixUI_InstallDir`` set is the same thing plus a "Destination
Folder" page and a browse dialog, so the switch is one attribute.

pyappdist has no option for it, so the pipeline is run a stage at a
time and the .wxs is edited between ``gen-wix`` and the wix build:

    build-wheels -> fetch-runtime -> build-image -> build-launchers
    -> gen-wix -> [switch the UI set] -> wix build -> package

The wix invocation mirrors the one in pyappdist/wix/build.py, with the
extension flag it adds when a licence is configured.

Needs the WiX UI extension in the extension cache. wix looks for it
under ``<cwd>/.wix/extensions/<id>/<version>/`` -- relative to the
working directory, not the user's home -- and it is not in the WiX
install, so it is fetched from NuGet here if it is missing.

Usage:
    python tools/build_msi.py
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".appdist-build" / "windows"
DIST = ROOT / "appdist" / "windows" / "dist"

WIX_ENV = "PYAPPDIST_WIN_WIX"
UI_EXTENSION = "WixToolset.UI.wixext"
UI_EXTENSION_VERSION = "6.0.2"
UI_EXTENSION_URL = (
    "https://api.nuget.org/v3-flatcontainer/wixtoolset.ui.wixext/"
    f"{UI_EXTENSION_VERSION}/wixtoolset.ui.wixext.{UI_EXTENSION_VERSION}.nupkg"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def find_wix() -> str:
    """The wix.exe to use: the one the environment names, else the usual
    places."""
    env = os.environ.get(WIX_ENV)
    if env and Path(env).is_file():
        return env
    found = shutil.which("wix")
    if found:
        return found
    raise SystemExit(
        f"wix.exe not found. Set {WIX_ENV} to the full path of wix.exe.\n"
        "WiX v6 is what this project uses: v7 wants the OSMF EULA accepted."
    )


def ensure_ui_extension() -> None:
    """Put the UI extension in the cache wix looks in.

    wix searches for an extension under ``<cwd>/.wix/extensions/``,
    relative to the working directory, and the WiX install ships only an
    empty placeholder for it -- the real payload is a NuGet package.
    """
    cache = ROOT / ".wix" / "extensions" / UI_EXTENSION / UI_EXTENSION_VERSION
    dll = cache / "wixext6" / f"{UI_EXTENSION}.dll"
    if dll.is_file():
        log(f"  UI extension: already cached ({dll.stat().st_size} bytes)")
        return

    log(f"  UI extension: fetching {UI_EXTENSION_VERSION} from NuGet ...")
    with urllib.request.urlopen(UI_EXTENSION_URL, timeout=120) as r:
        data = r.read()
    z = zipfile.ZipFile(io.BytesIO(data))
    cache.mkdir(parents=True, exist_ok=True)
    for name in z.namelist():
        if name.startswith("wixext6/") and not name.endswith("/"):
            out = cache / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(name))
    log(f"  UI extension: cached {dll.stat().st_size} bytes")


def run(cmd: list[str], cwd: Path | None = None, attempts: int = 3) -> None:
    """Run a stage, retrying it if it failed on the network.

    The stages that fetch the runtime talk to github over TLS, and the
    handshake fails intermittently on this machine: measured over six
    identical requests, four succeeded and two ended with
    "SSL: UNEXPECTED_EOF_WHILE_READING". curl does not have the
    problem, so it is the TLS layer, not the network.

    A build that dies on the first unlucky handshake is a build that
    fails for no reason, so a failure that looks like a network error
    is retried. The output of the failed attempt is kept, so the final
    error says what really happened.
    """
    import time

    last = ""
    for attempt in range(1, attempts + 1):
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            out = (r.stdout + r.stderr).strip()
            if out:
                for line in out.splitlines():
                    log(f"    {line}")
            return

        last = (r.stdout + r.stderr)[-4000:]
        network = any(k in last for k in (
            "UNEXPECTED_EOF", "SSLError", "URLError", "urlopen error",
            "download failed", "timed out", "Temporary failure",
            "Connection reset", "RemoteDisconnected"))
        if not network or attempt == attempts:
            break
        log(f"    the network failed; retrying ({attempt}/{attempts})")
        time.sleep(3)

    sys.stderr.write(last)
    raise SystemExit(f"failed: {' '.join(cmd[:3])} ... ({r.returncode})")


def pyappdist() -> str:
    """The pyappdist entry point.

    shutil.which() misses it when the environment's Scripts directory is
    not on PATH, so look next to the running interpreter too.
    """
    exe = shutil.which("pyappdist")
    if exe:
        return exe
    # On Windows the console scripts land in <prefix>\Scripts, which is
    # not the directory the interpreter itself lives in.
    import sysconfig
    candidates = [Path(sysconfig.get_path("scripts")),
                  Path(sys.prefix) / "Scripts",
                  Path(sys.executable).parent,
                  Path(sys.executable).parent / "Scripts"]
    for directory in candidates:
        for pattern in ("pyappdist.exe", "pyappdist"):
            for p in directory.glob(pattern):
                return str(p)
    raise SystemExit(
        "pyappdist not found -- pip install pyappdist into the environment "
        "that runs this script"
    )


def main() -> int:
    wix = find_wix()
    pad = pyappdist()
    log(f"wix        : {wix}")
    log(f"pyappdist  : {pad}")

    ensure_ui_extension()

    log("")
    log("=== the pyappdist stages ===")
    for stage in ("build-wheels", "fetch-runtime", "build-image",
                  "build-launchers"):
        log(f"  {stage}")
        run([pad, stage], cwd=ROOT)

    log("  gen-wix")
    run([pad, "gen-wix"], cwd=ROOT)

    wxs = BUILD / "lj.wxs"
    if not wxs.is_file():
        raise SystemExit(f"no .wxs at {wxs}")

    log("")
    log("=== stage the files the .wxs refers to by bare name ===")
    # pyappdist's own wix build copies these next to the .wxs; running
    # the stages by hand means doing it here. The .wxs refers to them by
    # bare name, which is what the second bind path resolves.
    staged = 0
    icon = ROOT / "assets" / "lj.ico"
    if icon.is_file():
        shutil.copy2(icon, BUILD / "pyappdist_product.ico")
        log(f"  pyappdist_product.ico  ({icon.stat().st_size:,} bytes)")
        staged += 1
    lic = ROOT / "assets" / "license.rtf"
    if lic.is_file():
        shutil.copy2(lic, BUILD / "pyappdist_license.rtf")
        log(f"  pyappdist_license.rtf  ({lic.stat().st_size:,} bytes)")
        staged += 1
    if not staged:
        raise SystemExit("neither assets/lj.ico nor assets/license.rtf is there")

    log("")
    log("=== switch the UI set to the one with a folder page ===")
    sys.path.insert(0, str(ROOT / "tools"))
    from switch_ui_to_install_dir import switch
    log("  changed" if switch(wxs) else "  already WixUI_InstallDir")

    log("")
    log("=== wix build ===")
    DIST.mkdir(parents=True, exist_ok=True)
    # the version comes from the .wxs, which pyappdist filled in
    import re
    m = re.search(r'Version="([^"]+)"', wxs.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"no Version attribute in {wxs}")
    out = DIST / f"lj-{m.group(1)}.msi"
    if out.exists():
        out.unlink()

    cmd = [wix, "build",
           "-arch", "x64",
           "-ext", UI_EXTENSION,
           str(wxs),
           "-b", str(BUILD / "image"),
           "-b", str(BUILD),
           "-o", str(out)]
    run(cmd, cwd=ROOT)
    log(f"  -> {out}  ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())