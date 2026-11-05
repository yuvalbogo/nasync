"""Install a desktop launcher + icon so nasync appears in the app menu.

Linux only. Follows the freedesktop.org Desktop Entry and Icon Theme specs:
a ``nasync.desktop`` file under ``$XDG_DATA_HOME/applications`` and the icon
under ``.../icons/hicolor/scalable/apps``.

The launcher opens the desktop app, and installing it is refused where GTK4 and
libadwaita aren't available — an entry in the apps list that opens nothing is
worse than no entry at all. On such a system the answer is the Flatpak, which
carries its own runtime, or the ``nasync`` command, which needs neither.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from nasync import ADW_MIN, APP_ID

# The launcher and icon are named after the application id, not "nasync".
# GNOME matches a window to its launcher by app_id, and looks the icon up by
# the entry's Icon= key, so all three have to agree or the app shows up in the
# dash with a generic gear instead of its own logo.
_LEGACY_APP_ID = "nasync"

_DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Version=1.0
Name=NASync
GenericName=SAMBA NAS Sync
Comment=Sync your NAS with class
Exec={exec}
Icon={app_id}
Terminal=false
Categories=Network;FileTransfer;
Keywords=samba;smb;cifs;nas;sync;backup;
StartupNotify=true
StartupWMClass={app_id}
"""


# What the launcher actually runs. Clicking an entry whose program can't start
# shows *nothing* — no window, no error — which is how a moved checkout and a
# Python upgrade both looked on a real machine: "the icon does nothing". This
# runs the app, and if it can't, says so in a notification, with the reason
# and the fix, and keeps the output in a log.
_WRAPPER = """#!/bin/sh
# NASync's app launcher: opens the app, or says why it can't.
# Written by `nasync install-desktop`; the next install rewrites it.
NASYNC={exec_quoted}
LOG="${{XDG_STATE_HOME:-$HOME/.local/state}}/nasync/gui.log"
mkdir -p "$(dirname "$LOG")"
started=$(date +%s)
"$NASYNC" gui "$@" >"$LOG" 2>&1
status=$?
[ "$status" -eq 0 ] && exit 0
if [ ! -x "$NASYNC" ]; then
    reason="$NASYNC is gone: the copy of NASync it pointed at was moved or deleted"
else
    reason=$(grep -v -i -e 'warning' -e '^[[:space:]]*$' "$LOG" | tail -n 1)
    [ -n "$reason" ] || reason="it exited with status $status"
fi
title="NASync couldn't start"
[ $(( $(date +%s) - started )) -gt 15 ] && title="NASync closed unexpectedly"
body="$reason. Reinstall it with 'make install-user' in the NASync folder. Details: $LOG"
if command -v notify-send >/dev/null 2>&1; then
    notify-send -a NASync -i {app_id} "$title" "$body"
else
    gdbus call --session --dest org.freedesktop.Notifications \\
        --object-path /org/freedesktop/Notifications \\
        --method org.freedesktop.Notifications.Notify \\
        NASync 0 {app_id} "$title" "$body" '[]' '{{}}' 0 >/dev/null 2>&1
fi
exit "$status"
"""


def adw_version() -> tuple:
    """Runtime (major, minor) of libadwaita, or None if it isn't importable."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
    except (ImportError, ValueError):
        # ValueError is what gi.require_version raises for a missing typelib.
        return None
    return (Adw.get_major_version(), Adw.get_minor_version())


def gui_available() -> bool:
    """True when this interpreter can reach a libadwaita new enough for the UI."""
    version = adw_version()
    return version is not None and version >= ADW_MIN


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
    # The package root, not this module's directory: launcher.py moved into
    # nasync/system/ in 0.15.0 and the assets stayed at nasync/assets/.
    return Path(__file__).parent.parent / "assets" / "nasync.svg"


def _nasync_exec() -> str:
    """Absolute path to the nasync executable.

    GUI launchers often run with a minimal PATH that lacks ~/.local/bin, so we
    bake in an absolute path rather than relying on PATH resolution.

    The executable running *this* command wins over whatever PATH resolves
    "nasync" to: they are frequently different installs (a venv here, pipx or
    ~/.local/bin there), and only the running one is the interpreter we just
    probed for GTK. Baking in the other would point the launcher at a build
    that may have no desktop app at all.
    """
    pinned = os.environ.get("NASYNC_EXEC", "")
    if os.path.isabs(pinned) and os.access(pinned, os.X_OK):
        # A stable install (`make install-user`) names its own path, which
        # survives the next install — the resolved one is replaced by it.
        return pinned
    candidate = sys.argv[0]
    if candidate and os.path.basename(candidate) == "nasync" and os.path.isfile(candidate):
        return os.path.realpath(candidate)
    return os.path.realpath(shutil.which("nasync") or candidate)


def exec_problem(path: str):
    """Why `path` can't be run as NASync, as a phrase — or None if it can.

    Two ways an installed launcher or service goes stale without anything
    saying so, both seen on one machine on one day: the checkout it points at
    was **moved** (the file is gone), and the distribution **upgraded Python**
    under a virtualenv (the file is there, but its packages are not, so it
    dies on import). Only running it catches the second, so it is run.
    """
    if not path:
        return None
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return f"it points at {path}, which no longer exists"
    try:
        result = subprocess.run([path, "--version"], capture_output=True,
                                text=True, timeout=30)
    except Exception as exc:                     # noqa: BLE001 — reported
        return f"{path} could not be started ({exc})"
    if result.returncode != 0:
        last = (result.stderr or result.stdout or "").strip().splitlines()
        reason = last[-1] if last else f"exit status {result.returncode}"
        return f"{path} fails to start ({reason})"
    return None


def desktop_entry() -> Path:
    return _data_home() / "applications" / f"{APP_ID}.desktop"


def wrapper() -> Path:
    """The script the launcher runs (see `_WRAPPER`)."""
    return _data_home() / "nasync" / "nasync-gui"


def entry_exec(text: str):
    """The program an installed .desktop file or systemd unit would run."""
    import shlex
    for line in text.splitlines():
        for key in ("Exec=", "ExecStart=", "NASYNC="):
            if line.startswith(key):
                try:
                    parts = shlex.split(line[len(key):])
                except ValueError:
                    return None
                return parts[0] if parts else None
    return None


def problem():
    """Why the installed launcher wouldn't open the app, or None."""
    entry = desktop_entry()
    if not entry.is_file():
        return None
    try:
        program = entry_exec(entry.read_text())
        if program and os.path.basename(program) == "nasync-gui":
            # Our wrapper: what matters is the program inside it.
            if not os.path.isfile(program):
                return f"it points at {program}, which no longer exists"
            program = entry_exec(Path(program).read_text())
        return exec_problem(program)
    except OSError:
        return None


def repair() -> bool:
    """Point a broken launcher at the program running now. True if it did.

    Only ever from a working NASync — this code is running, so the program
    running it opens the app — and never inside a Flatpak, whose launcher is
    the sandbox's to manage.
    """
    from nasync.system.portal import in_flatpak
    if in_flatpak() or problem() is None:
        return False
    # Never trade one broken pointer for another: started some way other
    # than through a `nasync` executable, the fallback is whatever PATH says,
    # which may be the very install that broke.
    if exec_problem(_nasync_exec()) is not None:
        return False
    install(gui=True)
    return True


def _refresh_caches(apps_dir: Path, data_home: Path) -> None:
    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "-f", "-t", str(data_home / "icons" / "hicolor")],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            pass  # cosmetic; the entry still works without a cache refresh


def install(gui: bool = None) -> list:
    """Create the launcher + icon. Returns the paths written.

    `gui` forces the check to be skipped; by default this probes for GTK4 and
    libadwaita and refuses when they're missing, because the launcher's whole
    purpose is to open the desktop app from the apps list. An entry that opens
    a terminal instead is not the thing anyone clicked for.
    """
    system = platform.system()
    if system == "Darwin":
        raise RuntimeError(
            "Desktop install is Linux-only. On macOS, use the 'nasync' "
            "command from a terminal."
        )
    if system != "Linux":
        raise RuntimeError("Desktop install is only supported on Linux.")

    if gui is None:
        gui = gui_available()
    if not gui:
        raise RuntimeError(
            f"The desktop app needs GTK4 and libadwaita "
            f"{ADW_MIN[0]}.{ADW_MIN[1]} or newer, which this system doesn't "
            f"have, so a launcher would open nothing. Install the Flatpak, "
            f"which carries its own runtime — or keep using the 'nasync' "
            f"command, which needs none of this."
        )

    data_home = _data_home()
    apps_dir = data_home / "applications"
    icon_dir = data_home / "icons" / "hicolor" / "scalable" / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    icon_dir.mkdir(parents=True, exist_ok=True)

    src = _icon_source()
    if not src.is_file():
        raise RuntimeError(f"Icon asset not found: {src}")

    written = []

    icon_dest = icon_dir / f"{APP_ID}.svg"
    shutil.copyfile(src, icon_dest)
    written.append(icon_dest)

    import shlex
    script = wrapper()
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_WRAPPER.format(exec_quoted=shlex.quote(_nasync_exec()),
                                      app_id=APP_ID))
    os.chmod(script, 0o755)
    written.append(script)

    desktop_dest = apps_dir / f"{APP_ID}.desktop"
    desktop_dest.write_text(_DESKTOP_ENTRY.format(
        exec=_desktop_quote(str(script)),
        app_id=APP_ID,
    ))
    os.chmod(desktop_dest, 0o644)
    written.append(desktop_dest)

    # Releases before 0.3.0 installed these under the bare name "nasync";
    # leaving them behind would show a second, stale entry in the app menu.
    _remove_legacy(data_home)

    _refresh_caches(apps_dir, data_home)
    return written


def _desktop_quote(path: str) -> str:
    """A path as a Desktop Entry Exec= argument (quoted only if it must be)."""
    if not any(c in path for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return path
    escaped = path.replace("\\", "\\\\").replace('"', '\\"') \
        .replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def _legacy_paths(data_home: Path) -> list:
    return [
        data_home / "applications" / f"{_LEGACY_APP_ID}.desktop",
        data_home / "icons" / "hicolor" / "scalable" / "apps" / f"{_LEGACY_APP_ID}.svg",
    ]


def _remove_legacy(data_home: Path) -> list:
    removed = []
    for path in _legacy_paths(data_home):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def uninstall() -> list:
    """Remove the launcher + icon. Returns the paths removed.

    Covers the pre-0.3.0 "nasync"-named files too, so upgrading and then
    uninstalling doesn't strand the old entry in the app menu.
    """
    data_home = _data_home()
    removed = []
    for path in [
        data_home / "applications" / f"{APP_ID}.desktop",
        data_home / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg",
        wrapper(),
    ] + _legacy_paths(data_home):
        if path.exists():
            path.unlink()
            removed.append(path)
    _refresh_caches(data_home / "applications", data_home)
    return removed
