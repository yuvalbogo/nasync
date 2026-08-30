"""The Adw.Application that owns the NASync window."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk  # noqa: E402

from nasync import __version__  # noqa: E402
from nasync.gui import APP_ID  # noqa: E402
from nasync.gui.window import NasyncWindow  # noqa: E402


class NasyncApplication(Adw.Application):

    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._window = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        # Fallback for backends that don't resolve the icon via the desktop
        # entry (X11, and any session where the launcher isn't installed).
        Gtk.Window.set_default_icon_name(APP_ID)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self) -> None:
        if self._window is None:
            self._window = NasyncWindow(self)

            test = Gio.SimpleAction.new("test", None)
            test.connect("activate", lambda *_: self._window.test_connection())
            self._window.add_action(test)

            settings = Gio.SimpleAction.new("settings", None)
            settings.connect("activate", lambda *_: self._window.open_settings())
            self._window.add_action(settings)
            self.set_accels_for_action("win.settings", ["<Primary>comma"])

            server_info = Gio.SimpleAction.new("serverinfo", None)
            server_info.connect("activate",
                                lambda *_: self._window.open_server_info())
            self._window.add_action(server_info)

            storage = Gio.SimpleAction.new("storage", None)
            storage.connect("activate", lambda *_: self._window.open_storage())
            self._window.add_action(storage)

            readme = Gio.SimpleAction.new("readme", None)
            readme.connect("activate", lambda *_: self._window.open_readme())
            self._window.add_action(readme)
            self.set_accels_for_action("win.readme", ["F1"])

        self._window.present()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name="NASync",
            application_icon=APP_ID,
            version=__version__,
            comments="Sync your NAS with CLASS.",
            website="https://github.com/YuvalBogo/nasync",
            issue_url="https://github.com/YuvalBogo/nasync/issues",
            developer_name="Yuval Bogo",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self._window)
