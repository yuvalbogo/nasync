"""Install a desktop launcher + icon so nasync appears in the app menu.

Linux only. Follows the freedesktop.org Desktop Entry and Icon Theme specs:
a ``nasync.desktop`` file under ``$XDG_DATA_HOME/applications`` and the icon
under ``.../icons/hicolor/scalable/apps``. The launcher opens ``nasync tui``
in a terminal window (``Terminal=true``).
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "nasync"

_DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Version=1.0
Name=NASync
GenericName=SAMBA NAS Sync
Comment=Sync your NAS with class
Exec={exec} tui
Icon=nasync
Terminal=true
Categories=Network;FileTransfer;
Keywords=samba;smb;cifs;nas;sync;backup;
StartupNotify=false
"""


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def _icon_source() -> Path:
    """Locate the packaged SVG icon (works for both installed and dev trees)."""
    try:
        from importlib.resources import files
        candidate = files("nasync").joinpath("assets/nasync.svg")
        if candidate.is_file():
            return Path(str(candidate))
    except Exception:
        pass
    return Path(__file__).with_name("assets") / "nasync.svg"


def _nasync_exec() -> str:
    """Absolute path to the nasync executable.

    GUI launchers often run with a minimal PATH that lacks ~/.local/bin, so we
    bake in an absolute path rather than relying on PATH resolution.
    """
    exe = shutil.which("nasync") or sys.argv[0]
    return os.path.realpath(exe)


def _refresh_caches(apps_dir: Path, data_home: Path) -> None:
    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "-f", "-t", str(data_home / "icons" / "hicolor")],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            pass  # cosmetic; the entry still works without a cache refresh


def install() -> list:
    """Create the launcher + icon. Returns the paths written."""
    system = platform.system()
    if system == "Darwin":
        raise RuntimeError(
            "Desktop install is Linux-only. On macOS, launch the UI with "
            "'nasync tui' from a terminal."
        )
    if system != "Linux":
        raise RuntimeError("Desktop install is only supported on Linux.")

    data_home = _data_home()
    apps_dir = data_home / "applications"
    icon_dir = data_home / "icons" / "hicolor" / "scalable" / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    icon_dir.mkdir(parents=True, exist_ok=True)

    src = _icon_source()
    if not src.is_file():
        raise RuntimeError(f"Icon asset not found: {src}")

    written = []

    icon_dest = icon_dir / "nasync.svg"
    shutil.copyfile(src, icon_dest)
    written.append(icon_dest)

    desktop_dest = apps_dir / "nasync.desktop"
    desktop_dest.write_text(_DESKTOP_ENTRY.format(exec=_nasync_exec()))
    os.chmod(desktop_dest, 0o755)
    written.append(desktop_dest)

    _refresh_caches(apps_dir, data_home)
    return written


def uninstall() -> list:
    """Remove the launcher + icon. Returns the paths removed."""
    data_home = _data_home()
    removed = []
    for path in (
        data_home / "applications" / "nasync.desktop",
        data_home / "icons" / "hicolor" / "scalable" / "apps" / "nasync.svg",
    ):
        if path.exists():
            path.unlink()
            removed.append(path)
    _refresh_caches(data_home / "applications", data_home)
    return removed
