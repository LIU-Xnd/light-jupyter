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

# The InstallDir set's "Destination Folder" page binds a PathEdit control
# to the WIXUI_INSTALLDIR property, and the set does not define that
# property -- the package has to. Without it the installer stops with
# "Error 2819: Control Folder on dialog InstallDirDlg needs a property
# linked to it" (wixtoolset/issues#818).
#
# The value is the directory's Id, not a reference to it: the property
# then holds the resolved path, and the page writes the user's choice
# back into it. A value like "[INSTALLFOLDER]" is a property reference,
# which the installer warns about (WIX1077) and does not resolve.
INSTALLDIR_PROPERTY = (
    '    <Property Id="WIXUI_INSTALLDIR" Value="INSTALLFOLDER" />\n'
)


def switch(wxs: Path) -> bool:
    """Point the UI set at WixUI_InstallDir, and give it the property
    its folder page needs. True if anything changed."""
    text = wxs.read_text(encoding="utf-8")
    changed = False

    if INSTALLDIR not in text:
        if MINIMAL not in text:
            raise SystemExit(
                f"{wxs}: expected {MINIMAL!r} and found neither it nor "
                f"{INSTALLDIR!r} -- pyappdist's output shape changed"
            )
        text = text.replace(MINIMAL, INSTALLDIR, 1)
        changed = True

    if 'Id="WIXUI_INSTALLDIR"' not in text:
        # put it next to the UI declaration, inside <Package>
        anchor = f"    {INSTALLDIR}\n"
        if anchor not in text:
            raise SystemExit(f"{wxs}: cannot find where to put the property")
        text = text.replace(anchor, anchor + INSTALLDIR_PROPERTY, 1)
        changed = True

    if changed:
        wxs.write_text(text, encoding="utf-8")
    return changed


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = Path(arg)
        changed = switch(p)
        print(f"{'changed' if changed else 'already'}  {p}")
