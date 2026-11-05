"""Tests for the desktop front-end.

The widget tests need PyGObject with GTK4 + libadwaita, which are system
packages rather than pip dependencies, so they skip when those are missing.
The tests above them cover the no-GTK path and run everywhere.
"""

import os
import tempfile

import pytest
from click.testing import CliRunner

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from nasync.gui.window import NasyncWindow
    from nasync.format import format_bytes as _format_bytes

    HAVE_GTK = True
except (ImportError, ValueError):        # no PyGObject, or no typelibs
    HAVE_GTK = False

# A module-level skip would take the no-GTK tests below down with it.
needs_gtk = pytest.mark.skipif(not HAVE_GTK, reason="PyGObject/GTK4 not installed")


def _children(widget) -> list:
    """A container's children in order — GTK4 offers no list of its own."""
    out = []
    child = widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


# --------------------------------------------------------------------- #
# No GTK required
# --------------------------------------------------------------------- #
def test_gui_command_explains_itself_without_gtk(monkeypatch):
    """Regression: `nasync.gui` defers its `gi` import into run(), so guarding
    the *import* let the real failure escape as a traceback."""
    import nasync.cli as cli
    import nasync.system.launcher as desktop

    monkeypatch.setattr(desktop, "adw_version", lambda: None)
    result = CliRunner().invoke(cli.gui, catch_exceptions=True)

    assert result.exit_code == 1, result.output
    assert "GTK4 and libadwaita" in result.output, result.output
    assert "Traceback" not in result.output, result.output


def test_gui_command_names_the_libadwaita_version_it_needs(monkeypatch):
    """Too-old libadwaita (Ubuntu 22.04 ships 1.1) must say so rather than
    dying on an AttributeError inside a widget constructor."""
    import nasync.cli as cli
    import nasync.system.launcher as desktop

    monkeypatch.setattr(desktop, "adw_version", lambda: (1, 1))
    result = CliRunner().invoke(cli.gui, catch_exceptions=True)

    assert result.exit_code == 1, result.output
    assert "1.1" in result.output and "1.5" in result.output, result.output
    assert "Flatpak" in result.output, result.output


def test_gui_available_rejects_old_libadwaita(monkeypatch):
    import nasync.system.launcher as desktop

    monkeypatch.setattr(desktop, "adw_version", lambda: (1, 4))
    assert desktop.gui_available() is False
    monkeypatch.setattr(desktop, "adw_version", lambda: (1, 5))
    assert desktop.gui_available() is True
    monkeypatch.setattr(desktop, "adw_version", lambda: None)
    assert desktop.gui_available() is False


def test_launcher_and_app_id_agree():
    """GNOME matches a window to its launcher by app_id, and finds the icon via
    the entry's Icon= key. If these drift apart the app shows a generic icon."""
    from nasync.system import launcher as desktop
    from nasync.gui import APP_ID

    assert desktop.APP_ID == APP_ID
    entry = desktop._DESKTOP_ENTRY.format(exec="/usr/bin/nasync", app_id=APP_ID)
    assert f"Icon={APP_ID}" in entry
    assert f"StartupWMClass={APP_ID}" in entry


def test_launcher_prefers_the_running_executable(monkeypatch, tmp_path):
    """The baked-in path must be the interpreter we probed for GTK, not
    whatever else PATH resolves 'nasync' to."""
    import nasync.system.launcher as desktop

    running = tmp_path / "nasync"
    running.write_text("#!/bin/sh\n")
    other = tmp_path / "other-nasync"
    other.write_text("#!/bin/sh\n")

    monkeypatch.setattr(desktop.sys, "argv", [str(running)])
    monkeypatch.setattr(desktop.shutil, "which", lambda _n: str(other))
    assert desktop._nasync_exec() == os.path.realpath(str(running))

    # With no usable argv[0] (pytest, `python -m`), PATH is the fallback.
    monkeypatch.setattr(desktop.sys, "argv", ["/usr/bin/pytest"])
    assert desktop._nasync_exec() == os.path.realpath(str(other))


# --------------------------------------------------------------------- #
# GTK required
# --------------------------------------------------------------------- #
@pytest.fixture
def window(monkeypatch):
    """A real window backed by a throwaway config — and a throwaway data dir.

    The data dir matters since 0.20.0: a window checks the app launcher at
    start and repairs a broken one, and a test must never be the thing that
    rewrites the real ~/.local/share/applications entry.
    """
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("XDG_CONFIG_HOME", tmp)
        monkeypatch.setenv("XDG_DATA_HOME", os.path.join(tmp, "data"))
        Adw.init()
        app = Adw.Application(application_id="io.github.YuvalBogo.NASync.Test")
        yield NasyncWindow(app)


@needs_gtk
def test_format_bytes():
    assert _format_bytes(512) == "512 B"
    assert _format_bytes(1536) == "1.5 KB"
    assert _format_bytes(5 * 1024 ** 3) == "5.0 GB"


@needs_gtk
def test_summary_counts_per_file_errors_not_just_failed_pairs(window):
    """A pair can succeed overall while individual files fail; reporting only
    'failed' used to render that run as a clean 'Complete'."""
    window._errors = ["a.txt: permission denied"]
    window._on_transfer_done({"ok": 1, "failed": 0, "errors": 1, "transferred": 3,
                              "skipped": 2, "deleted": 1, "unsupported": 0})

    assert window._progress_title.get_label() == "Finished with 1 error(s)"
    assert window._details_button.get_visible()


@needs_gtk
def test_summary_reports_unsupported_names(window):
    window._errors = []
    window._on_transfer_done({"ok": 1, "failed": 0, "errors": 0, "transferred": 1,
                              "skipped": 0, "deleted": 0, "unsupported": 4})

    assert window._progress_title.get_label() == "Complete"
    assert "4 skipped (see details)" in window._progress_detail.get_label()
    assert not window._details_button.get_visible()


@needs_gtk
def test_collect_report_keeps_failures_and_drops_the_rest(window):
    window._errors = []
    for line in (
        "  Uploaded 3 · unchanged 2 · deleted 0",
        "  ! a.txt: permission denied",
        "  ! skipped (name illegal on SMB): c:d.txt",
        "❌ Failed on photos: connection reset",
    ):
        window._collect_report(line)

    # A skipped file is a file the backup doesn't have, so it is listed too.
    assert window._errors == ["a.txt: permission denied",
                              "skipped (name illegal on SMB): c:d.txt",
                              "Failed on photos: connection reset"]


@needs_gtk
def test_collect_report_is_bounded(window):
    window._errors = []
    for i in range(500):
        window._collect_report(f"  ! file{i}.txt: nope")
    assert len(window._errors) == 200


@needs_gtk
def test_byte_progress_advances_within_a_file(window):
    window._update_file_progress(1, 4, "big.iso", "Uploading")
    window._update_byte_progress(512, 1024, "big.iso", "Uploading")
    # Half of the first of four files.
    assert window._progress_bar.get_fraction() == pytest.approx(0.125)


@needs_gtk
def test_cancel_sits_at_the_end_of_the_action_row(window):
    """Layout contract: Cancel shares the line with the actions but sits at the
    far edge of it, and the state is centred on top of the bar."""
    action_row = window._sync_button.get_parent()
    assert window._cancel_button.get_parent() is action_row
    assert window._restore_button.get_parent() is action_row

    order = _children(action_row)
    assert order.index(window._cancel_button) == len(order) - 1
    assert order.index(window._restore_button) < order.index(window._cancel_button)
    # Independent buttons, not one linked strip.
    assert not action_row.has_css_class("linked")

    progress_box = window._progress_bar.get_parent()
    order = _children(progress_box)
    assert order.index(window._progress_title) < order.index(window._progress_bar)
    assert order.index(window._progress_bar) < order.index(
        window._progress_detail.get_parent())
    assert window._progress_title.get_halign() == Gtk.Align.CENTER


@needs_gtk
def test_settings_dialog_states_credential_protection_either_way(window,
                                                                 monkeypatch):
    """Silence would read as "encrypted", which on a headless box it isn't."""
    from nasync.system import vault
    from nasync.gui.dialogs import SettingsDialog

    monkeypatch.setattr(vault, "backend", lambda: "keyring")
    group = SettingsDialog(window.config)._build_security_group()
    row = group.get_first_child()
    assert "are encrypted" in _row_titles(group)[0]

    monkeypatch.setattr(vault, "backend", lambda: "")
    group = SettingsDialog(window.config)._build_security_group()
    assert "not encrypted" in _row_titles(group)[0]
    del row


@needs_gtk
def test_settings_dialog_surfaces_an_unreadable_credential_file(window,
                                                                monkeypatch):
    from nasync.system import vault
    from nasync.gui.dialogs import SettingsDialog

    monkeypatch.setattr(vault, "backend", lambda: "keyring")
    window.config.credentials_error = "The keyring is locked."

    titles = _row_titles(SettingsDialog(window.config)._build_security_group())
    assert any("couldn't be read" in t for t in titles)


def _row_titles(group) -> list:
    """Every ActionRow title inside a PreferencesGroup, in order.

    Adw nests its rows inside a listbox rather than parenting them to the group
    directly, so this walks rather than iterating children.
    """
    from gi.repository import Gtk

    found, stack = [], [group]
    while stack:
        widget = stack.pop(0)
        if hasattr(widget, "get_title") and isinstance(widget, Gtk.ListBoxRow):
            found.append(widget.get_title())
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


_SERVER = {"name": "nas", "ip": "10.0.0.2", "port": 445, "username": "u",
           "password": "p", "share": "media", "encrypt": False}

_LISTING = [
    {"name": "photos", "path": "photos", "is_dir": True,
     "is_symlink": False, "size": None},
    {"name": "notes.txt", "path": "notes.txt", "is_dir": False,
     "is_symlink": False, "size": 2048},
]


@needs_gtk
def test_remote_browser_renders_folders_and_files(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    # The dialog fetches on a worker thread; calling the callback directly is
    # what makes this deterministic without a main loop to pump.
    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)

    browser = RemoteBrowserDialog(_SERVER)
    browser._on_loaded("", _LISTING)

    titles = _row_titles(browser._list)
    assert titles == ["photos", "notes.txt"]


@needs_gtk
def test_remote_browser_selects_the_folder_it_is_showing(monkeypatch):
    """Selecting where you are, not a highlighted row — the picker's rule, and
    it removes the ambiguity from an action that can delete files."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    chosen = []

    browser = RemoteBrowserDialog(_SERVER, on_chosen=chosen.append)
    browser._on_select_clicked(None)
    assert chosen == ["/"]

    browser = RemoteBrowserDialog(_SERVER, start_path="photos/2024",
                                  on_chosen=chosen.append)
    browser._on_select_clicked(None)
    assert chosen[-1] == "photos/2024"      # stored relative since 0.19.0


@needs_gtk
def test_the_browser_strips_the_root_by_folder_and_ignoring_case(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    browser = RemoteBrowserDialog(_SERVER, confine_to="Yuval")
    browser._path = "yuval/photos"
    assert browser._relative_to_root() == "photos"
    browser._path = "yuval2/photos"           # beside the root, not inside it
    assert browser._relative_to_root() == "yuval2/photos"


@needs_gtk
def test_remote_browser_navigates_down_and_back_up(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    browser = RemoteBrowserDialog(_SERVER)

    browser._on_row_activated(None, "photos")
    assert browser._path == "photos"
    browser._on_row_activated(None, "photos/2024")
    assert browser._path == "photos/2024"

    browser._on_up_clicked(None)
    assert browser._path == "photos"
    browser._on_up_clicked(None)
    assert browser._path == ""
    # Already at the root: Up must not climb out of the share.
    browser._on_up_clicked(None)
    assert browser._path == ""


@needs_gtk
def test_a_late_listing_for_an_abandoned_folder_is_discarded(monkeypatch):
    """Navigate away while a slow directory is still loading and its answer
    must not replace what is now on screen."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    browser = RemoteBrowserDialog(_SERVER)
    browser._path = "photos"
    browser._on_loaded("somewhere-else", _LISTING)
    assert _row_titles(browser._list) == []

    browser._on_failed("somewhere-else", Exception("too slow"))
    assert browser._banner.get_revealed() is False


@needs_gtk
def test_clicking_a_folder_mid_load_still_fetches_it(monkeypatch):
    """Regression: an "already loading, skip this one" guard stranded the
    dialog. The click has already moved `_path`, so the in-flight reply for the
    old folder is discarded — and if the new fetch was never started, nothing
    ever arrives to replace it and the spinner stays up forever."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    fetched = []
    monkeypatch.setattr(dialogs, "run_async",
                        lambda work, on_done=None, **k: fetched.append(True))

    browser = RemoteBrowserDialog(_SERVER)          # one fetch in flight
    browser._on_row_activated(None, "photos")       # clicked before it landed
    assert len(fetched) == 2, "the second folder was never requested"

    # And the abandoned reply is still correctly ignored.
    browser._on_loaded("", _LISTING)
    assert _row_titles(browser._list) == []


@needs_gtk
def test_a_failed_listing_is_shown_rather_than_left_blank(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import RemoteBrowserDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    browser = RemoteBrowserDialog(_SERVER)
    browser._on_failed("", Exception("'nope' doesn't exist on the server."))

    assert browser._banner.get_revealed() is True
    assert "doesn't exist" in browser._banner.get_title()


@needs_gtk
def test_pair_dialog_offers_browsing_only_when_it_has_a_server():
    """Without a server there is nothing to browse, so the button would open a
    dialog that cannot connect."""
    from nasync.gui.dialogs import PairDialog

    assert PairDialog(server=_SERVER)._server is not None
    typed_only = PairDialog()
    assert typed_only._server is None
    # The field still works either way — this never removes a capability.
    assert typed_only._dest is not None


_USAGE = {
    "path": "", "total": 10550, "files": 5,
    "children": [
        {"name": "movies", "path": "movies", "is_dir": True,
         "total": 10000, "files": 2},
        {"name": "docs", "path": "docs", "is_dir": True,
         "total": 500, "files": 2},
        {"name": "loose.txt", "path": "loose.txt", "is_dir": False,
         "total": 50, "files": 1},
    ],
}


@needs_gtk
def test_storage_dialog_lists_biggest_first_with_recursive_totals(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import StorageDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    storage = StorageDialog(_SERVER)
    storage._on_usage("", {"total": 40_000, "free": 10_000, "disk_free": 10_000,
                           "used": 30_000}, _USAGE)

    assert _row_titles(storage._list) == ["movies/", "docs/", "loose.txt"]
    assert "10.3 KB" in storage._summary.get_subtitle()
    assert "free" in storage._summary.get_subtitle()


@needs_gtk
def test_storage_dialog_survives_a_server_that_wont_report_free_space(monkeypatch):
    """Not every NAS answers the volume query; the report is still the point."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import StorageDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    storage = StorageDialog(_SERVER)
    storage._on_usage("", None, _USAGE)

    assert _row_titles(storage._list) == ["movies/", "docs/", "loose.txt"]
    assert "free" not in storage._summary.get_subtitle()


@needs_gtk
def test_storage_dialog_drills_into_a_folder(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import StorageDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    storage = StorageDialog(_SERVER)
    storage._on_row_activated(None, "movies")
    assert storage._path == "movies"
    storage._on_up_clicked(None)
    assert storage._path == ""


@needs_gtk
def test_storage_dialog_says_duplicates_are_by_size_and_never_deleted(monkeypatch):
    """Both caveats matter: same size isn't proof, and NASync must not be
    thought to be about to remove anything."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import StorageDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    storage = StorageDialog(_SERVER)
    storage._on_duplicates("", [
        {"size": 9000, "paths": ["a.mkv", "b/a.mkv"], "wasted": 9000,
         "hash": None},
    ])

    subtitle = storage._dup_summary.get_subtitle()
    assert "Matched by size" in subtitle
    assert "never removes files" in subtitle
    assert _row_titles(storage._dup_list)[0] == "8.8 KB wasted"


@needs_gtk
def test_storage_dialog_reports_no_duplicates_without_implying_a_clean_bill(
        monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import StorageDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    storage = StorageDialog(_SERVER)
    storage._on_duplicates("", [])
    # The size floor has to be stated, or "no duplicates" overclaims.
    assert "were not considered" in storage._dup_summary.get_subtitle()


@needs_gtk
def test_a_late_storage_scan_for_an_abandoned_folder_is_discarded(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import StorageDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    storage = StorageDialog(_SERVER)
    storage._path = "movies"
    storage._on_usage("somewhere-else", None, _USAGE)
    assert _row_titles(storage._list) == []


_INFO = {
    "ip": "10.0.0.2", "port": 445, "share": "media", "username": "u",
    "connection": {"dialect": 0x0311, "dialect_name": "SMB 3.1.1",
                   "encrypted": False, "signed": True,
                   "supports_encryption": True, "requires_signing": True,
                   "max_read": 8388608, "max_write": 8388608},
    "volume": {"total": 1000, "free": 400, "disk_free": 400, "used": 600},
    "shares": [
        {"name": "media", "type": "Disk", "comment": "", "is_disk": True,
         "is_special": False, "is_temporary": False},
        {"name": "IPC$", "type": "IPC", "comment": "IPC Service",
         "is_disk": False, "is_special": True, "is_temporary": False},
    ],
}


@needs_gtk
def test_server_info_reports_what_was_negotiated(monkeypatch):
    from nasync.gui import dialogs
    from nasync.gui.dialogs import ServerInfoDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    dialog = ServerInfoDialog(_SERVER)
    dialog._on_loaded(_INFO)

    titles = _row_titles(dialog._page)
    assert "Protocol" in titles
    assert "media" in titles and "IPC$" in titles


@needs_gtk
def test_server_info_points_at_encryption_when_the_server_supports_it(monkeypatch):
    """An unencrypted connection to a server that *could* encrypt is the one
    case worth nudging about — the rest is reporting, not advice."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import ServerInfoDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    dialog = ServerInfoDialog(_SERVER)
    dialog._on_loaded(_INFO)
    assert "Not encrypted" in _row_titles(dialog._page)

    encrypted = {**_INFO, "connection": {**_INFO["connection"],
                                         "encrypted": True, "signed": False}}
    dialog = ServerInfoDialog(_SERVER)
    dialog._on_loaded(encrypted)
    titles = _row_titles(dialog._page)
    assert "Encrypted" in titles
    # Signing off *because* encryption covers it must not read as a weakness.
    assert "Not signed" in titles


@needs_gtk
def test_server_info_survives_a_server_that_refuses_the_share_list(monkeypatch):
    """An old NAS may refuse SRVSVC; the rest of the panel is still useful."""
    from nasync.gui import dialogs
    from nasync.gui.dialogs import ServerInfoDialog

    monkeypatch.setattr(dialogs, "run_async", lambda work, on_done=None, **k: None)
    dialog = ServerInfoDialog(_SERVER)
    dialog._on_loaded({**_INFO, "shares": None,
                       "shares_error": "The server refused to talk about its shares."})

    titles = _row_titles(dialog._page)
    assert "Protocol" in titles              # still reported
    assert "Couldn't be listed" in titles


@needs_gtk
def test_switching_tab_switches_the_active_server(window):
    """The visible tab is the active server — that's what selecting one means."""
    window.config.add_or_update_server("nas", "192.0.2.10", "u", "p", "media", 445)
    window.config.add_or_update_server("work", "192.0.2.20", "u", "p", "share", 445)
    window.config.set_active_server("nas")
    window.refresh_servers()

    window._stack.set_visible_child_name("work")
    assert window.config.get_active_server_name() == "work"
    assert "'work'" in window._progress_detail.get_label()


@needs_gtk
def test_the_bar_measures_the_current_pair_not_the_whole_run(window):
    """Across a whole run a big folder crawls the bar forward and reads as
    stalled; per pair it fills, resets and fills again."""
    window._started_at = None

    window._update_pair_progress(1, 4, "Documents", "Syncing")
    assert window._progress_bar.get_fraction() == pytest.approx(0.0)
    assert "pair 1 of 4" in window._progress_log.get_label()
    assert "comparing files" in window._progress_log.get_label()

    # Half of this pair's files is half a bar, not an eighth of one.
    window._update_file_progress(1, 2, "a.jpg", "Uploading")
    assert window._progress_bar.get_fraction() == pytest.approx(0.5)

    window._update_byte_progress(512, 1024, "a.jpg", "Uploading")
    # Half of the first of this pair's two files.
    assert window._progress_bar.get_fraction() == pytest.approx(0.25)

    # A new pair starts its own bar over, and the detail line carries how far
    # through the run we are.
    window._update_pair_progress(2, 4, "Photos", "Syncing")
    assert window._progress_bar.get_fraction() == pytest.approx(0.0)
    assert "pair 2 of 4" in window._progress_log.get_label()

    window._update_file_progress(2, 2, "b.jpg", "Uploading")
    assert window._progress_bar.get_fraction() == pytest.approx(1.0)
    assert "Photos (2/4)" in window._progress_log.get_label()


@needs_gtk
def test_rebuilding_tabs_does_not_change_the_active_server(window):
    """Adding pages makes the first one visible; without a guard that would
    save the wrong server before the right tab is selected."""
    window.config.add_or_update_server("aaa", "192.0.2.10", "u", "p", "a", 445)
    window.config.add_or_update_server("zzz", "192.0.2.20", "u", "p", "z", 445)
    window.config.set_active_server("zzz")

    window.refresh_servers()

    assert window.config.get_active_server_name() == "zzz"
    assert window._stack.get_visible_child_name() == "zzz"


@needs_gtk
def test_actions_are_disabled_without_a_server(window):
    window.refresh_servers()
    assert not window._sync_button.get_sensitive()
    assert window._stack.get_visible_child_name() == "none"

    window.config.add_or_update_server("nas", "192.0.2.10", "u", "p", "media", 445)
    window.refresh_servers()
    assert window._sync_button.get_sensitive()

    # Finishing a transfer must not re-enable them on a serverless window.
    window.config.remove_server("nas")
    window.refresh_servers()
    window._set_busy(False)
    assert not window._sync_button.get_sensitive()


@needs_gtk
def test_a_sync_notifies_only_when_nobody_is_watching(window, monkeypatch, recording_notifier):
    """A notification for a sync whose progress bar is on screen is noise; one
    for a sync started before switching away is the whole point."""
    import nasync.gui.window as window_module

    window._notifier = recording_notifier
    # The send shells out, so the window defers it — run it inline instead.
    monkeypatch.setattr(window_module, "run_async", lambda work, **kw: work())

    monkeypatch.setattr(window, "is_active", lambda: True, raising=False)
    window._on_transfer_done({"transferred": 2, "skipped": 1, "deleted": 0})
    assert recording_notifier.sent == []

    monkeypatch.setattr(window, "is_active", lambda: False, raising=False)
    window._on_transfer_done({"transferred": 2, "skipped": 1, "deleted": 0})
    assert recording_notifier.sent[-1][0] == "Sync complete"
    assert "2 transferred" in recording_notifier.sent[-1][1]

    window._on_transfer_error(Exception("connection refused"))
    summary, body, urgency = recording_notifier.sent[-1]
    assert summary == "Sync failed"
    assert "connection refused" in body
    assert urgency == "critical"


@needs_gtk
def test_turning_notifications_off_reaches_the_window(window, monkeypatch, recording_notifier):
    """The popover writes the config; the window holds the notifier that has
    to honour it."""
    import nasync.gui.window as window_module

    window._notifier = recording_notifier
    monkeypatch.setattr(window_module, "run_async", lambda work, **kw: work())
    monkeypatch.setattr(window, "is_active", lambda: False, raising=False)

    window.config.set_notifications(False)
    window._on_settings_changed("notifications")
    assert recording_notifier.enabled is False

    # The window re-reads the setting at send time, so a notifier flipped off
    # elsewhere still can't leak a notification through.
    window._on_transfer_done({"transferred": 1, "skipped": 0, "deleted": 0})
    assert recording_notifier.enabled is False


@needs_gtk
def test_byte_progress_is_throttled_but_never_drops_the_last(window, monkeypatch):
    """A gigabit LAN fires this per 1 MiB chunk — over 100 a second, each one
    relaying out a label — so most are dropped on the worker thread. The one
    that completes a file must still get through, or the bar stops short."""
    import nasync.gui.window as window_module

    delivered = []
    monkeypatch.setattr(window_module, "on_main",
                        lambda func, *args: delivered.append(args))

    window._last_byte_update = 0.0
    for done in range(1, 6):                    # a burst, well inside the window
        window._thread_byte_progress(done, 100, "big.iso", "Uploading")
    assert len(delivered) == 1, delivered       # the first; the rest coalesced

    # The final chunk of the file bypasses the throttle entirely.
    window._thread_byte_progress(100, 100, "big.iso", "Uploading")
    assert delivered[-1] == (100, 100, "big.iso", "Uploading")


# --------------------------------------------------------------------------- #
# Settings, rewritten in 0.21.0 for the per-server Settings tab. The tests
# above these used to import AutomaticPopover and ModePopover, removed in
# 0.17.0; the ImportError was read as "GTK missing" and every widget test in
# this file was skipped without anyone noticing.
# --------------------------------------------------------------------------- #
def _with_server(window, name="nas"):
    window.config.add_or_update_server(name, "192.0.2.10", "u", "p", "media", 445)
    window.config.set_active_server(name)
    window.refresh_servers()
    return name


def _inline_service(monkeypatch, status=None):
    """Run service reconciliation inline, against a stand-in service."""
    import nasync.gui.dialogs as dialogs_module
    from nasync.system import service
    applied = []
    state = status or {"available": True, "backend": "systemd", "installed": True,
                       "active": True, "detail": "Running."}
    monkeypatch.setattr(service, "apply",
                        lambda config: applied.append(config) or state)
    monkeypatch.setattr(service, "status", lambda config=None: state)
    monkeypatch.setattr(dialogs_module, "run_async",
                        lambda work, on_done=None, on_error=None: on_done(work()))
    return applied


@needs_gtk
def test_the_mode_row_applies_instantly_to_its_own_server(window):
    from nasync.gui.dialogs import SyncModeRow
    server = _with_server(window)
    changed = []
    row = SyncModeRow(window.config, on_changed=changed.append, server=server)
    assert window.config.get_sync_mode(server) == "mirror"

    row.set_selected(row._modes.index("update"))
    assert window.config.get_sync_mode(server) == "update"
    assert changed == ["update"]
    assert "Nothing is deleted" in row.get_subtitle() or "nothing" in \
        row.get_subtitle().lower()

    # Reloading is not the user choosing, so it must not report a change.
    window.config.set_sync_mode("mirror", server=server)
    row.reload()
    assert changed == ["update"]


@needs_gtk
def test_automation_switches_save_per_server_and_reach_the_service(window,
                                                                   monkeypatch):
    from nasync.gui.dialogs import AutomationRows
    server = _with_server(window)
    applied = _inline_service(monkeypatch)
    rows = AutomationRows(window.config, server=server)
    assert not rows.live.get_active() and not rows.scheduled.get_active()
    assert not rows.time.get_sensitive()          # nothing to schedule yet

    rows.live.set_active(True)
    assert window.config.get_automation(server)["live_sync"] is True
    rows.scheduled.set_active(True)
    assert window.config.get_automation(server)["scheduled_sync"] is True
    assert rows.time.get_sensitive()
    assert len(applied) >= 2, "a switch saved but never reached the service"


@needs_gtk
def test_the_summary_says_off_live_scheduled_or_paused(window, monkeypatch):
    from nasync.gui.dialogs import AutomationRows
    server = _with_server(window)
    _inline_service(monkeypatch)
    rows = AutomationRows(window.config, server=server)
    assert rows.summary() == "Off"
    window.config.set_automation(live_sync=True, scheduled_sync=True,
                                 scheduled_time="7:5", server=server)
    window.config.set_automation(background=True)
    assert rows.summary() == "Live · daily 07:05"
    # Triggers on with the background off is neither on nor off, and it's the
    # state most likely to be mistaken for "it's syncing".
    window.config.set_automation(background=False)
    assert rows.summary() == "Paused"


@needs_gtk
def test_turning_the_background_off_keeps_the_triggers(window, monkeypatch):
    from nasync.gui.dialogs import GlobalAutomationRows
    server = _with_server(window)
    _inline_service(monkeypatch)
    window.config.set_automation(live_sync=True, server=server)
    window.config.set_automation(background=True)
    rows = GlobalAutomationRows(window.config)
    rows.background.set_active(False)
    assert window.config.get_background() is False
    assert window.config.get_automation(server)["live_sync"] is True


@needs_gtk
def test_a_missing_background_mechanism_is_said(window, monkeypatch):
    from nasync.gui.dialogs import GlobalAutomationRows
    from nasync.system import service
    server = _with_server(window)
    window.config.set_automation(live_sync=True, server=server)
    monkeypatch.setattr(service, "backend", lambda: "")
    rows = GlobalAutomationRows(window.config)
    assert "only runs while NASync is open" in rows.background.get_subtitle()


@needs_gtk
def test_the_line_above_sync_says_what_mirror_will_do(window):
    server = _with_server(window)
    window.config.add_pair("/tmp", "backups", "tmp-pair", server=server)
    window.refresh_summary()
    assert "Mirror" in window._progress_detail.get_label()
    window.config.set_sync_mode("update", server=server)
    window.refresh_summary()
    assert "Update" in window._progress_detail.get_label()


@needs_gtk
def test_a_settings_change_made_elsewhere_reaches_the_settings_tab(window):
    server = _with_server(window)
    window.config.set_sync_mode("update", server=server)
    window.refresh_summary()
    mode_row, _automation = window._syncing_rows[0]
    assert mode_row._modes[mode_row.get_selected()] == "update"


@needs_gtk
def test_each_server_gets_a_tab_owning_its_own_pairs(window):
    window.config.add_or_update_server("nas", "192.0.2.10", "u", "p", "media", 445)
    window.config.add_or_update_server("work", "192.0.2.20", "u", "p", "share", 445)
    window.config.add_pair("/tmp", "backups/tmp", "tmp-pair", server="nas")
    window.config.set_active_server("nas")
    window.refresh_servers()

    assert window._stack.get_visible_child_name() == "nas"
    assert len(window._pair_groups["nas"]._rows) == 1
    assert window._pair_groups["work"]._rows[0].get_title() == "No sync pairs yet"
    assert window._stack.get_page(
        window._stack.get_child_by_name("nas")).get_badge_number() == 1
    assert window._stack.get_page(
        window._stack.get_child_by_name("work")).get_badge_number() == 0


@needs_gtk
def test_one_server_keeps_the_app_title_in_the_header(window):
    assert window._window_title.get_title() == "NASync"
    _with_server(window, "solo")
    # One server is unambiguous: the header names the app, not a switcher of
    # one — the Server/Settings bar below it is what's visible instead.
    assert window._header.get_title_widget() is window._window_title
    assert window._switcher_bar.get_visible()
