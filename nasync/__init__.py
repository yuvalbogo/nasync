"""NASync — sync your local folders to a SAMBA home NAS, with class.

Reading order, if you are learning this codebase:

    core/       the engine — config, transport, syncing, automation
    system/     how it fits the machine: portals, systemd, keyring, notifications
    gui/        the desktop app (GTK4 + libadwaita)
    cli.py      the command line, and the reference for every feature
    format.py   presentation helpers both front-ends share

``core`` is where the actual work happens and depends on nothing above it.
``system`` is the sandbox surface — the part that decides what the Flatpak
manifest has to ask for. ``cli`` and ``gui`` are two callers of the same
engine: **a feature is not done until it exists in both.**

Tests live in ``tests/`` at the repository root, one file per module.
"""

# The single source of truth for the project version. `pyproject.toml` reads
# this attribute via `[tool.setuptools.dynamic]`, and the CLI's --version flag
# imports it directly, so this is the only line to touch when releasing.
__version__ = "0.17.0"

# The application's identity, shared by everything that has to agree on it: the
# .desktop file name, the icon name, GLib.set_prgname(), and the desktop-entry
# hint on notifications. It lives here rather than in `nasync.gui` because the
# headless paths need it too — `nasync auto` notifies without importing GTK.
APP_ID = "io.github.YuvalBogo.NASync"

# Everything the desktop app uses — Adw.Dialog, Adw.AlertDialog,
# Adw.AboutDialog — landed in libadwaita 1.5. Older runtimes (notably Ubuntu
# 22.04's 1.1) would fail with an unhelpful AttributeError deep inside a widget,
# so both the GUI and the launcher check up front. It lives here rather than in
# `nasync.gui` so that `system/launcher.py` can check it without importing the
# GUI package, which would be a backwards dependency.
ADW_MIN = (1, 5)
