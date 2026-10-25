"""Switch the generated WiX from the Minimal UI set to the InstallDir one.

pyappdist hardcodes ``WixUI_Minimal``: a one-page wizard with a licence
and no way to choose where the program goes. ``WixUI_InstallDir`` is the
same stock set plus a "Destination Folder" page and a browse dialog, so
the user can pick the install folder.

pyappdist has no option for this, so the .wxs it writes is edited
between ``gen-wix`` and the wix build. It is a one-line change to the
UI set id; the licence RTF and its WixVariable stay as they are.

Run it on the .wxs pyappdist wrote, in place.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MINIMAL = '<ui:WixUI Id="WixUI_Minimal" />'
INSTALLDIR = '<ui:WixUI Id="WixUI_InstallDir" />'


def switch(wxs: Path) -> bool:
    """Point the UI set at WixUI_InstallDir. True if it changed."""
    text = wxs.read_text(encoding="utf-8")
    if INSTALLDIR in text:
        return False
    if MINIMAL not in text:
        raise SystemExit(
            f"{wxs}: expected {MINIMAL!r} and found neither it nor "
            f"{INSTALLDIR!r} -- pyappdist's output shape changed"
        )
    wxs.write_text(text.replace(MINIMAL, INSTALLDIR, 1), encoding="utf-8")
    return True


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = Path(arg)
        changed = switch(p)
        print(f"{'changed' if changed else 'already'}  {p}")
