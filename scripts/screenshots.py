#!/usr/bin/env python3
"""Regenerate the images in `screenshots/`.

The README and (eventually) the Flathub appstream metadata both need pictures
of NASync that look like a real session, not an empty first-run window. Taking
them by hand means remembering to fake the same demo NAS every time and being
on a machine where a screenshot tool can grab a GTK window at all — this
machine is Wayland with no `grim`/`gnome-screenshot`, so an external grabber
isn't an option.

So both front-ends are captured from inside the process instead:

* CLI — commands run against a throwaway config and their output is replayed
  through Rich, which writes a terminal SVG.
* GUI — the window is rendered to a texture with GSK and saved as PNG. That is
  the real widget tree at the real scale, just drawn to an offscreen buffer
  rather than to the compositor.

Everything runs against a temporary XDG_CONFIG_HOME seeded with a demo NAS, so
the real servers in ~/.config/nasync are never touched or shown.

Usage:  .venv/bin/python scripts/screenshots.py [cli|gui|all]
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "screenshots"
NASYNC = REPO / ".venv" / "bin" / "nasync"

# A plausible home-NAS setup: two servers so the server tabs have something to
# show, and pairs whose names read like folders people actually sync.
#
# Every value here is invented. These images end up in the README and in the
# Flathub listing, so nothing about the machine that generated them may appear:
# no real username, no real home-directory layout, no address off an actual
# network. `DEMO_USER` is the one place to change if that ever needs revisiting.
# A placeholder name rather than "user", which renders as "user user"
# next to the username label. Obviously not a real person.
DEMO_USER = "alex"
DEMO_HOME = f"/home/{DEMO_USER}"

DEMO_CONFIG = {
    "servers": [
        {"name": "attic-nas", "ip": "192.168.1.42", "username": DEMO_USER,
         "share": "media", "port": 445, "encrypt": True},
        {"name": "office-backup", "ip": "192.168.1.57", "username": DEMO_USER,
         "share": "backups", "port": 445},
    ],
    "active_server": "attic-nas",
    "pairs_by_server": {
        "attic-nas": [
            {"name": "photos", "local": f"{DEMO_HOME}/Pictures",
             "server": "photos"},
            {"name": "music", "local": f"{DEMO_HOME}/Music", "server": "music"},
            {"name": "documents", "local": f"{DEMO_HOME}/Documents",
             "server": "documents"},
        ],
        "office-backup": [
            {"name": "projects", "local": f"{DEMO_HOME}/Projects",
             "server": "projects"},
        ],
    },
    "sync_mode": "mirror",
    "automation": {
        "live_sync": True,
        "scheduled_sync": True,
        "scheduled_time": "02:00",
        "interval_minutes": 30,
        "background": True,
    },
    "notifications": {"default": True},
}


def demo_home(tmp: Path) -> dict:
    """Write the demo config and return the env that points nasync at it."""
    cfg = tmp / "nasync"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text(json.dumps(DEMO_CONFIG, indent=2))
    # A password has to exist or the CLI reports the server as unconfigured.
    creds = cfg / ".creds"
    creds.write_text(json.dumps({"attic-nas": "demo", "office-backup": "demo"}))
    creds.chmod(0o600)
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp)
    # Belt and braces: nothing should interpolate $HOME into output, but if
    # anything ever does, it must not be the real one.
    env["HOME"] = str(tmp / "home")
    (tmp / "home").mkdir(parents=True, exist_ok=True)
    return env


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

# (filename, title shown in the SVG's window chrome, argv)
CLI_SHOTS = [
    ("cli-help.svg", "nasync --help", ["--help"]),
    ("cli-status.svg", "nasync status", ["status"]),
    ("cli-list.svg", "nasync list --all", ["list", "--all"]),
    ("cli-servers.svg", "nasync servers", ["servers"]),
]


def shoot_cli(env) -> None:
    from rich.console import Console
    from rich.text import Text

    for filename, title, argv in CLI_SHOTS:
        proc = subprocess.run(
            [str(NASYNC), *argv],
            env={**env, "COLUMNS": "100", "TERM": "xterm-256color",
                 "FORCE_COLOR": "1"},
            capture_output=True, text=True,
        )
        body = (proc.stdout + proc.stderr).rstrip("\n")
        # `record=True` keeps the segments so `save_svg` can redraw them; the
        # width matches COLUMNS above so nothing re-wraps differently here.
        console = Console(record=True, width=100, file=open(os.devnull, "w"),
                          force_terminal=True, color_system="truecolor")
        console.print(Text.from_ansi(f"$ nasync {' '.join(argv)}\n"))
        console.print(Text.from_ansi(body))
        console.save_svg(str(OUT / filename), title=title)
        print(f"wrote screenshots/{filename}")


# --------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------- #

GUI_SCRIPT = r'''
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Gsk

sys.path.insert(0, %(repo)r)
from nasync.gui.window import NasyncWindow

OUT = %(out)r

STORAGE_SERVER = %(storage)r

# Mirrors DEMO_CONFIG's active server; invented, like everything else here.
DEMO_SERVER = {
    "name": "attic-nas", "ip": "192.168.1.42", "username": %(user)r,
    "password": "", "share": "media", "port": 445, "encrypt": True,
}


def capture(widget, path):
    """Draw a mapped widget to a PNG without going through the compositor."""
    width = widget.get_allocated_width()
    height = widget.get_allocated_height()
    snapshot = Gtk.Snapshot()
    paintable = Gtk.WidgetPaintable.new(widget)
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        print("nothing to draw for", path)
        return
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(widget.get_native().get_surface())
    texture = renderer.render_texture(node, None)
    texture.save_to_png(path)
    renderer.unrealize()
    print("wrote", path)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.YuvalBogo.NASync.Shots")
        self.steps = [
            self.shot_main,
            self.shot_settings,
            self.shot_server,
            self.shot_storage,
            self.shot_light,
            self.finish,
        ]

    def do_activate(self):
        self.win = NasyncWindow(self)
        # A little wider than the app's own default so paths don't ellipsize in
        # a README image, but the same height, because the point of these
        # pictures is what the window actually shows without scrolling.
        self.win.set_default_size(900, 860)
        self.win.present()
        # One idle pass isn't enough: the window still has to allocate and the
        # first frame has to be drawn before the widget tree has any content.
        # The same wait applies between steps — a dialog that was told to
        # present a microsecond ago has not been drawn into the window yet.
        GLib.timeout_add(1400, self.next_step)

    def next_step(self):
        step = self.steps.pop(0)
        step()
        if self.steps:
            GLib.timeout_add(1400, self.next_step)
        return GLib.SOURCE_REMOVE

    def shot_main(self):
        capture(self.win, OUT + "/gui-main.png")

    def shot_settings(self):
        from nasync.core.config import Config
        from nasync.gui.dialogs import SettingsDialog
        self.dialog = SettingsDialog(Config())
        self.dialog.present(self.win)

    def shot_server(self):
        from nasync.gui.dialogs import ServerDialog
        capture(self.win, OUT + "/gui-settings.png")
        self.dialog.close()
        self.dialog = ServerDialog(DEMO_SERVER)
        self.dialog.present(self.win)

    def shot_storage(self):
        from nasync.gui.dialogs import StorageDialog
        capture(self.win, OUT + "/gui-server.png")
        self.dialog.close()
        # Against a live throwaway server when one is running, so this shows
        # real figures instead of a spinner. The dialog labels itself with the
        # *share* name, never the address, so nothing about the host appears.
        self.dialog = StorageDialog(STORAGE_SERVER or DEMO_SERVER)
        self.dialog.present(self.win)

    def shot_light(self):
        capture(self.win, OUT + "/gui-storage.png")
        self.dialog.close()
        # Half the desktops out there are on the light scheme, and the Flathub
        # listing shows whichever the visitor's browser prefers.
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT)

    def finish(self):
        capture(self.win, OUT + "/gui-main-light.png")
        self.quit()


App().run([])
'''


def shoot_gui(env) -> None:
    # Set by the caller when a throwaway Samba container is up; see shot_storage.
    storage = json.loads(os.environ["NASYNC_SHOT_SERVER"]) \
        if os.environ.get("NASYNC_SHOT_SERVER") else None
    script = GUI_SCRIPT % {"repo": str(REPO), "out": str(OUT),
                           "user": DEMO_USER, "storage": storage}
    tmp = Path(tempfile.mkdtemp()) / "shoot_gui.py"
    tmp.write_text(script)
    proc = subprocess.run([str(REPO / ".venv" / "bin" / "python"), str(tmp)],
                          env=env, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        env = demo_home(Path(tmp))
        if what in ("cli", "all"):
            shoot_cli(env)
        if what in ("gui", "all"):
            shoot_gui(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
