"""GTK4 + libadwaita front-end for NASync.

The flagship front-end, and a sibling of the CLI: both sit on the same building
blocks — `Config` for persistence, `SAMBAClient` for connection checks, and
`Syncer` for transfers. Blocking work runs on worker threads and is marshalled
back onto the GTK main loop with `GLib.idle_add`.

Where libadwaita is too old for this, the CLI is the answer, and the Flatpak —
which carries its own runtime — is what makes the desktop app reachable there.
"""

# Both re-exported: the GUI is the main user of each, and callers have always
# imported them from here. They are defined in `nasync/__init__.py` so that
# `system/launcher.py` can check the libadwaita floor without importing this
# package — a launcher that needs the GUI package to decide whether the GUI can
# run would be a circle.
from nasync import ADW_MIN, APP_ID  # noqa: F401


def run(argv=None) -> int:
    """Launch the GTK application. Raises ImportError if PyGObject is absent."""
    from gi.repository import GLib

    # GNOME matches a window to its .desktop file by the Wayland app_id, which
    # GTK takes from the program name — by default the basename of argv[0]
    # ("nasync"), *not* the application id. Without this the shell finds no
    # matching launcher and falls back to a generic icon in the dash and
    # alt-tab. The desktop entry is installed as "<APP_ID>.desktop" to match.
    GLib.set_prgname(APP_ID)

    from nasync.gui.app import NasyncApplication

    return NasyncApplication().run(argv or [])
