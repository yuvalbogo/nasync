"""The NASync main window.

Layout follows the shape of the app: a list of saved servers (one active), a
list of sync pairs that target whichever server is active, and a transfer
area that reports progress and offers a cancel button while work is running.
"""

import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from nasync.core import activity  # noqa: E402
from nasync.core.config import Config  # noqa: E402
from nasync.core.samba import SAMBAClient  # noqa: E402
from nasync.core.sync import Syncer  # noqa: E402
from nasync.format import format_bytes  # noqa: E402
from nasync.gui.dialogs import (AppSettingsDialog, AutomationRows,  # noqa: E402
                                PairDialog, ServerDialog,  # noqa: E402
                                ServerInfoDialog, StorageDialog,  # noqa: E402
                                SyncModeRow)
from nasync.gui.threads import on_main, run_async  # noqa: E402
from nasync.system import launcher, service  # noqa: E402
from nasync.system.notify import Notifier  # noqa: E402

#: How each mode is described where a run is about to happen:
#: ``(label, tooltip, consequence)``. The consequence is the clause appended to
#: the line above the Sync button — a mode's *name* never said whether pressing
#: Sync would delete anything, which is the one thing worth knowing before
#: pressing it.
MODE_PRESENTATION = {
    "mirror": ("Mirror",
               "Mirror mode — the server is made to match your local folders "
               "exactly. Files removed locally are deleted on the server.",
               " · deletes server files you removed locally"),
    "update": ("Update",
               "Update mode — new and changed files are copied. Nothing is "
               "deleted on the server.",
               " · nothing is deleted on the server"),
}


def _icon_button(icon_name: str, tooltip: str, handler, *args) -> Gtk.Button:
    """The flat, vertically-centred icon button used throughout the rows."""
    button = Gtk.Button(icon_name=icon_name)
    button.set_valign(Gtk.Align.CENTER)
    button.add_css_class("flat")
    button.set_tooltip_text(tooltip)
    button.connect("clicked", handler, *args)
    return button


def _confirm(parent, heading: str, body: str, verb: str, handler, *args) -> None:
    """Ask before something destructive, with Cancel as the default.

    Every one of these deletes something or overwrites local files, so the
    default response matters: it's what an accidental Enter selects. Building
    them in one place is what stops the fourth one from omitting it.
    """
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response(verb.lower(), verb)
    dialog.set_response_appearance(verb.lower(), Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.connect("response", handler, *args)
    dialog.present(parent)


# Kept as a module-level name because the window uses it throughout; the one
# implementation now lives beside the other shared formatters.
_format_bytes = format_bytes


def _merge_results(total, result):
    """Add one Syncer result into a running total of several."""
    if total is None:
        return dict(result)
    merged = dict(total)
    for key, value in result.items():
        if isinstance(value, bool):
            merged[key] = merged.get(key, False) or value
        elif isinstance(value, (int, float)):
            merged[key] = merged.get(key, 0) + value
        elif isinstance(value, list):
            merged[key] = merged.get(key, []) + value
    return merged


class NasyncWindow(Adw.ApplicationWindow):

    #: Smallest gap between byte-progress repaints. No eye reads faster than
    #: this, and the callback arrives far more often — see `_thread_byte_progress`.
    BYTE_PROGRESS_INTERVAL = 0.1

    def __init__(self, application):
        super().__init__(application=application, title="NASync")
        # Sized for the Server tab — a server and a handful of pairs — with the
        # action bar pinned under it. The settings live on their own tab now,
        # so the window no longer has to be tall enough for both at once.
        self.set_default_size(720, 780)

        self.config = Config()
        self._cancel_event = None
        self._busy = False
        self._started_at = None
        self._current_file = None
        self._current_pair = None
        self._errors = []
        self._rebuilding = False
        self._last_byte_update = 0.0
        self._last_toast = None
        self._last_toast_at = 0.0
        # For syncs the user walked away from; the window itself reports the
        # ones they stayed to watch.
        self._notifier = Notifier(enabled=self.config.get_notifications())
        #: The sync another process is running, as last read — None when
        #: nothing is. See `_poll_activity`.
        self._bg_status = None
        self._bg_cancelling = False

        self._toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()

        header = self._header = Adw.HeaderBar()
        self._window_title = Adw.WindowTitle(title="NASync",
                                             subtitle="Sync your NAS with CLASS")
        header.set_title_widget(self._window_title)

        add_server = Gtk.Button(icon_name="list-add-symbolic")
        add_server.set_tooltip_text("Add a server")
        add_server.connect("clicked", lambda *_: self.open_server_dialog())
        header.pack_start(add_server)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("Main menu")
        self._build_menu(menu_button)
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        # Two levels, outer first: which server, then which aspect of it.
        # Servers are the outer scope — a tab is the whole context for a sync —
        # so they sit in the header, and the Server/Settings switcher for
        # whichever is selected sits in the bar beneath.
        self._stack = Adw.ViewStack()
        self._server_switcher = Adw.ViewSwitcher(
            stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE)

        self._sub_switcher = Adw.ViewSwitcher(
            policy=Adw.ViewSwitcherPolicy.WIDE)
        self._sub_switcher.set_halign(Gtk.Align.CENTER)
        self._switcher_bar = Gtk.Box()
        self._switcher_bar.set_halign(Gtk.Align.CENTER)
        self._switcher_bar.set_margin_top(6)
        self._switcher_bar.set_margin_bottom(6)
        self._switcher_bar.append(self._sub_switcher)
        self._switcher_bar.set_visible(False)
        toolbar.add_top_bar(self._switcher_bar)

        #: The Server/Settings stack belonging to each server page, and which
        #: of the two the user last chose. Remembered so that switching servers
        #: keeps you on the tab you were reading rather than snapping back.
        self._sub_stacks = {}
        self._sub_page = "server"

        self._stack.connect("notify::visible-child-name", self._on_tab_switched)
        toolbar.set_content(self._stack)

        toolbar.add_bottom_bar(self._build_action_bar())

        self._toasts.set_child(toolbar)
        self.set_content(self._toasts)

        self._pair_groups = {}
        self._syncing_rows = []
        self.refresh_servers()
        # What the background service (or the CLI) is doing, checked once a
        # second. Until 0.20.0 the window had no way to know: a scheduled sync
        # could be twenty minutes in while this said "Idle".
        GLib.timeout_add_seconds(1, self._poll_activity)
        self._repair_installs()
        self.refresh_summary()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def _build_menu(self, button) -> None:
        from gi.repository import Gio

        model = Gio.Menu()
        model.append("Test connection", "win.test")
        model.append("Server info…", "win.serverinfo")
        model.append("Storage…", "win.storage")

        section = Gio.Menu()
        section.append("App settings…", "win.settings")
        section.append("Documentation", "win.readme")
        section.append("About NASync", "app.about")
        model.append_section(None, section)

        button.set_menu_model(model)

    def _build_server_page(self, server: dict) -> Gtk.Widget:
        """One server as two tabs: what it syncs, and how it syncs.

        Splitting them keeps the screen you land on about the job — this NAS
        and these folders — while the settings stay one click away rather than
        behind a menu. They are per server, so they belong inside the server's
        own context rather than in an app-wide dialog.
        """
        name = server["name"]
        stack = Adw.ViewStack()

        page = stack.add_titled(self._build_server_tab(server), "server", "Server")
        page.set_icon_name("network-server-symbolic")
        page = stack.add_titled(self._build_settings_tab(name), "settings",
                                "Settings")
        page.set_icon_name("emblem-system-symbolic")

        stack.set_visible_child_name(self._sub_page)
        stack.connect("notify::visible-child-name", self._on_sub_tab_switched)
        self._sub_stacks[name] = stack
        return stack

    def _build_server_tab(self, server: dict) -> Adw.PreferencesPage:
        """Which NAS, and which folders go to it."""
        name = server["name"]
        page = Adw.PreferencesPage()

        details = Adw.PreferencesGroup(title="Server")
        row = Adw.ActionRow(
            title=name,
            subtitle=f"{server['ip']}:{server.get('port', 445)}/{server['share']}"
                     f"  ·  user {server['username']}",
        )
        row.set_activatable(False)
        row.add_prefix(Gtk.Image(icon_name="network-server-symbolic"))

        row.add_suffix(_icon_button("document-edit-symbolic", "Edit this server",
                                    self._on_edit_server, name))
        row.add_suffix(_icon_button("user-trash-symbolic", "Remove this server",
                                    self._on_remove_server, name))
        details.add(row)
        page.add(details)

        pairs_group = Adw.PreferencesGroup(title="Sync pairs")
        # Beside the group's own title, which is where GNOME puts the action
        # that adds to a list — not as a row inside the list it adds to.
        add_pair = _icon_button("list-add-symbolic", "Add a sync pair",
                                lambda *_, s=name: self.open_pair_dialog(server_name=s))
        pairs_group.set_header_suffix(add_pair)
        self._fill_pairs_group(pairs_group, name)
        page.add(pairs_group)

        self._pair_groups[name] = pairs_group
        return page

    def _fill_pairs_group(self, group, server_name: str) -> None:
        """One row per pair, straight into the group.

        No disclosure widget in front of them: the pairs are what this screen
        is for, and a folder you are syncing is not a detail to be expanded.
        """
        for row in getattr(group, "_rows", []):
            group.remove(row)
        rows = []

        pairs = self.config.get_pairs(server_name)
        if not pairs:
            empty = Adw.ActionRow(
                title="No sync pairs yet",
                subtitle="Add a folder to start syncing it to this server.",
            )
            empty.set_activatable(False)
            empty.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            group.add(empty)
            rows.append(empty)
        else:
            # Pairs sharing a server folder delete each other's files in mirror
            # mode, so sync skips them; the row says why, so the skip is never
            # a mystery found in a log.
            clashes = self.config.overlapping_pairs(server_name)
            for pair in pairs:
                row = Adw.ActionRow(title=pair["name"],
                                    subtitle=f"{pair['local']}  →  {pair['server']}")
                row.set_subtitle_lines(1)
                if pair["name"] in clashes:
                    row.set_subtitle(
                        f"Skipped: shares its server folder with "
                        f"{', '.join(clashes[pair['name']])}. Edit it to give "
                        f"it a folder of its own.")
                    row.set_subtitle_lines(2)
                    row.add_css_class("error")
                    row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                else:
                    row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                row.add_suffix(_icon_button(
                    "document-edit-symbolic", "Edit this pair",
                    self._on_edit_pair, pair["name"], server_name))
                row.add_suffix(_icon_button(
                    "user-trash-symbolic", "Remove this pair",
                    self._on_remove_pair, pair["name"], server_name))
                group.add(row)
                rows.append(row)

        group._rows = rows

    def _build_settings_tab(self, name: str) -> Adw.PreferencesPage:
        """This server's own mode and triggers.

        Everything here belongs to `name` alone: since 0.18.0 a home NAS can
        mirror on a schedule while a work one only ever takes a manual push.
        The genuinely app-wide settings — the background service, notifications
        and password security — are in the menu's App settings instead.
        """
        page = Adw.PreferencesPage()

        # Stated before the mode, because it bounds what the mode can do: what
        # mirror deletes is "things inside this folder", and seeing the folder
        # is what makes that sentence mean something.
        root = self.config.get_root(name)
        share = (self.config.get_server(name, with_password=False) or {}).get("share", "")
        root_group = Adw.PreferencesGroup(title="Sync root")
        root_row = Adw.ActionRow(
            title=f"{share}:/{root}" if root else "No sync root",
            subtitle=("Everything this server syncs lives inside here. Nothing "
                      "beside it on the share can be reached — or deleted."
                      if root else
                      "The whole share is in range of a sync. Set a root in "
                      "this server's settings to put the rest out of reach."),
        )
        root_row.set_subtitle_lines(0)
        root_row.set_activatable(False)
        root_row.add_prefix(Gtk.Image(
            icon_name="folder-symbolic" if root else "dialog-warning-symbolic"))
        edit_root = _icon_button("document-edit-symbolic", "Change the sync root",
                                 self._on_edit_server, name)
        root_row.add_suffix(edit_root)
        root_group.add(root_row)
        page.add(root_group)

        mode_group = Adw.PreferencesGroup(
            title="Sync mode",
            description=f"What a sync does with files that are on '{name}' but "
                        "not in your local folder.",
        )
        mode_row = SyncModeRow(self.config, server=name,
                               on_changed=self._on_settings_changed)
        mode_group.add(mode_row)
        page.add(mode_group)

        auto_group = Adw.PreferencesGroup(
            title="Automatic syncing",
            description=f"Off until you turn them on. Both sync '{name}' using "
                        "the mode above.",
        )
        automation = AutomationRows(self.config, server=name,
                                    on_changed=self._on_settings_changed)
        for row in automation.rows:
            auto_group.add(row)
        page.add(auto_group)

        note_group = Adw.PreferencesGroup(title="Restore")
        note = Adw.ActionRow(
            title="Restore never deletes local files",
            subtitle="Restoring from the server adds and overwrites local files "
                     "only. Local files the server doesn't have are left alone, "
                     "in either mode.",
        )
        note.set_subtitle_lines(0)
        note.set_activatable(False)
        note.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        note_group.add(note)
        page.add(note_group)

        # Per server page, and the settings behind them are per server too, so
        # each set reloads only its own.
        self._syncing_rows.append((mode_row, automation))
        return page

    def _on_sub_tab_switched(self, stack, _param) -> None:
        """Remember Server/Settings so switching NAS doesn't snap you back."""
        name = stack.get_visible_child_name()
        if name and not self._rebuilding:
            self._sub_page = name

    def _build_empty_page(self) -> Adw.StatusPage:
        page = Adw.StatusPage(
            icon_name="network-server-symbolic",
            title="No servers yet",
            description="Add the NAS you want to sync with, then give it folders.",
        )
        button = Gtk.Button(label="Add a server")
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.open_server_dialog())
        page.set_child(button)
        return page

    def _build_action_bar(self) -> Gtk.Widget:
        """Actions, then the transfer state they produce, pinned to the bottom.

        Reading order matches cause and effect: press a button at the top of
        this bar, watch the result directly underneath it. Keeping it out of
        the scrolling page also means progress can't be scrolled off-screen
        mid-transfer.
        """
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        bar.set_margin_top(18)
        bar.set_margin_bottom(18)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.append(self._build_buttons())
        bar.append(self._build_progress())
        return bar

    def _build_buttons(self) -> Gtk.Widget:
        """Sync, Dry run, Restore — and Cancel at the far end of the same line.

        Each is its own button rather than one linked strip: they aren't
        variations on a single action, and the gap before Cancel keeps the
        control that stops work away from the three that start it.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._sync_button = Gtk.Button()
        self._sync_button.set_child(
            Adw.ButtonContent(label="Sync", icon_name="document-send-symbolic"))
        self._sync_button.add_css_class("suggested-action")
        # The primary action, so it carries both the accent colour and the
        # width: the two secondary actions sit at their natural size beside it.
        self._sync_button.set_size_request(150, -1)
        self._sync_button.connect("clicked", lambda *_: self.start_transfer("push", False))
        row.append(self._sync_button)

        self._dryrun_button = Gtk.Button(label="Dry run")
        self._dryrun_button.connect("clicked", lambda *_: self.start_transfer("push", True))
        row.append(self._dryrun_button)

        self._restore_button = Gtk.Button()
        self._restore_button.set_child(
            Adw.ButtonContent(label="Restore", icon_name="document-revert-symbolic"))
        self._restore_button.connect("clicked", lambda *_: self.confirm_restore())
        row.append(self._restore_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)

        self._cancel_button = Gtk.Button(label="Cancel")
        self._cancel_button.add_css_class("destructive-action")
        self._cancel_button.set_sensitive(False)
        self._cancel_button.connect("clicked", lambda *_: self.cancel_transfer())
        row.append(self._cancel_button)

        return row

    def _build_progress(self) -> Gtk.Widget:
        """State, bar, live log, then what the run means.

        The state word reads at a glance above the bar; the log below carries
        the file-by-file detail while a run is going. The last line is the one
        that matters at rest: it says what pressing Sync would do, and after a
        run, what it did.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self._progress_title = Gtk.Label(label="Idle")
        self._progress_title.add_css_class("heading")
        self._progress_title.set_halign(Gtk.Align.CENTER)
        box.append(self._progress_title)

        self._progress_bar = Gtk.ProgressBar(show_text=False)
        box.append(self._progress_bar)

        # Hidden until something runs. A live file-by-file log has nothing to
        # say at rest, and an empty scroller here cost the page ~80px — enough
        # to push the settings group below the fold on an 800-tall window.
        self._log_scroll = log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_max_content_height(120)
        log_scroll.set_propagate_natural_height(True)
        log_scroll.set_visible(False)

        self._progress_log = Gtk.Label(label="")
        self._progress_log.add_css_class("dim-label")
        self._progress_log.set_selectable(True)
        self._progress_log.set_wrap(True)
        self._progress_log.set_halign(Gtk.Align.START)
        self._progress_log.set_valign(Gtk.Align.START)
        self._progress_log.set_margin_start(6)
        self._progress_log.set_margin_end(6)

        log_scroll.set_child(self._progress_log)
        box.append(log_scroll)

        detail_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        detail_row.set_halign(Gtk.Align.CENTER)

        # Visible, unlike its predecessor: this label was created hidden and
        # never shown, so every message written to it — the idle summary, the
        # final tally, the error — was discarded.
        self._progress_detail = Gtk.Label(label="")
        self._progress_detail.add_css_class("dim-label")
        self._progress_detail.add_css_class("caption")
        self._progress_detail.set_wrap(True)
        self._progress_detail.set_justify(Gtk.Justification.CENTER)
        detail_row.append(self._progress_detail)

        self._details_button = Gtk.Button(label="Show errors")
        self._details_button.add_css_class("flat")
        self._details_button.set_visible(False)
        self._details_button.connect("clicked", lambda *_: self.show_error_details())
        detail_row.append(self._details_button)

        box.append(detail_row)
        return box

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def refresh_servers(self) -> None:
        """Rebuild one tab per server, keeping the active one selected."""
        # Adding pages makes the first one visible, which would fire the tab
        # handler and save the wrong active server before we select the right
        # one. Ignore switches until the rebuild is done.
        self._rebuilding = True
        try:
            self._rebuild_server_tabs()
        finally:
            self._rebuilding = False

    def _rebuild_server_tabs(self) -> None:
        child = self._stack.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._stack.remove(child)
            child = following
        self._pair_groups = {}
        self._syncing_rows = []
        self._sub_stacks = {}

        servers = self.config.list_servers()
        if not servers:
            self._stack.add_titled(self._build_empty_page(), "none", "No servers")
            self._switcher_bar.set_visible(False)
            self._header.set_title_widget(self._window_title)
            self._set_actions_sensitive(False)
            return

        for server in servers:
            page = self._stack.add_titled(self._build_server_page(server),
                                          server["name"], server["name"])
            page.set_icon_name("network-server-symbolic")
            # A badge on the tab, so "which of these has folders set up?" is
            # answerable without clicking through them. No attention dot beside
            # it: a badge reading 0 already says the same thing twice.
            page.set_badge_number(self.config.count_pairs(server["name"]))

        active = self.config.get_active_server_name() or servers[0]["name"]
        self._stack.set_visible_child_name(active)
        # With one server there is nothing to switch between, so the header
        # goes back to naming the app rather than showing a switcher of one.
        self._header.set_title_widget(
            self._server_switcher if len(servers) > 1 else self._window_title)
        self._switcher_bar.set_visible(True)
        self._bind_sub_switcher(active)
        self._set_actions_sensitive(True)

    def _bind_sub_switcher(self, server_name: str) -> None:
        """Point the Server/Settings switcher at the visible server's stack.

        One switcher rather than one per page: it lives in a bar of its own, so
        it has to be re-aimed whenever the server changes — and re-aimed at the
        tab the user was already on.
        """
        stack = self._sub_stacks.get(server_name)
        if stack is None:
            return
        if stack.get_visible_child_name() != self._sub_page:
            stack.set_visible_child_name(self._sub_page)
        self._sub_switcher.set_stack(stack)

    def refresh_pairs(self, server_name: str = None) -> None:
        """Refresh pairs display after changes (add/remove/edit)."""
        name = server_name or self.config.get_active_server_name()
        group = self._pair_groups.get(name)
        if group is not None:
            self._fill_pairs_group(group, name)

    def _set_actions_sensitive(self, sensitive: bool) -> None:
        """Nothing to sync without a server, so don't offer to."""
        if self._busy:
            return
        for button in (self._sync_button, self._dryrun_button, self._restore_button):
            button.set_sensitive(sensitive)

    def refresh_summary(self) -> None:
        """Restate what pressing Sync would do, on the line above the button.

        The mode lives in a row on the page, so this doesn't repeat the setting
        — it spells out the consequence, which the mode's name alone never did.
        A run that has finished keeps its summary until the next one starts.
        """
        label, tooltip, consequence = MODE_PRESENTATION[self.config.get_sync_mode()]
        self._sync_button.set_tooltip_text(tooltip)

        for mode_row, automation in self._syncing_rows:
            mode_row.reload()
            automation.reload()

        if self._busy:
            return

        active = self.config.get_active_server_name()
        if not active:
            self._progress_detail.set_label("Add a server to get started.")
            return

        count = self.config.count_pairs(active)
        if not count:
            self._progress_detail.set_label(f"'{active}' has no sync pairs yet.")
            return

        summary = f"{label} · sends {count} folder(s) to '{active}'{consequence}"
        # The run that happened while the window was closed is the one people
        # open the app to ask about.
        last = activity.read_last(self.config.config_dir)
        if last:
            summary += "\n" + activity.describe_last(last)
        self._progress_detail.set_label(summary)

    #: A repeat of the same message inside this many seconds is swallowed. One
    #: settings change can report itself twice — once on write, once when the
    #: background service finishes reconciling — and two identical banners
    #: sliding in on top of each other reads as a stutter, not as information.
    TOAST_REPEAT_SECONDS = 2.0

    def toast(self, message: str) -> None:
        now = time.monotonic()
        if (message == self._last_toast
                and now - self._last_toast_at < self.TOAST_REPEAT_SECONDS):
            return
        self._last_toast, self._last_toast_at = message, now
        self._toasts.add_toast(Adw.Toast(title=message, timeout=3))

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def open_settings(self) -> None:
        """The app-wide settings. Per-server ones are on each server's tab."""
        AppSettingsDialog(self.config,
                          on_changed=self._on_settings_changed).present(self)

    def open_server_info(self) -> None:
        """What the active server is, and what was negotiated with it."""
        server = self._active_server()
        if not server:
            self.toast("Add a server first.")
            return
        ServerInfoDialog(server).present(self)

    #: The project README, which is the app's documentation. Opened online
    #: rather than from disk: a pip install keeps no README beside the code and
    #: a Flatpak cannot see the source tree at all, so the file is only
    #: reliably present for someone running from a checkout.
    README_URL = "https://github.com/YuvalBogo/nasync#readme"

    def open_readme(self) -> None:
        """Hand the README to the desktop's browser, via the portal in a sandbox."""
        launcher = Gtk.UriLauncher(uri=self.README_URL)
        launcher.launch(self, None, self._on_readme_launched)

    def _on_readme_launched(self, launcher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            # No browser, or the portal refused. Say so rather than looking
            # like the menu item does nothing.
            self.toast(f"Couldn't open the documentation: {exc.message}")

    def open_storage(self) -> None:
        """What's using space on the active server, and what's stored twice."""
        server = self._active_server()
        if not server:
            self.toast("Add a server first.")
            return
        StorageDialog(server).present(self)

    def _active_server(self):
        """The active server's settings, or None if there isn't a usable one."""
        name = self.config.get_active_server_name()
        server = self.config.get_server(name) if name else None
        return server if server and server.get("ip") else None

    def _on_settings_changed(self, what: str) -> None:
        """One handler for both the page rows and the Settings dialog.

        They write the same app-wide config, so whichever one made the change,
        every other surface showing it has to catch up.
        """
        self.refresh_summary()
        if what == "automation":
            automation = self.config.get_automation()
            if automation["live_sync"] or automation["scheduled_sync"]:
                self.toast("Automatic syncing updated")
            else:
                self.toast("Automatic syncing off")
        elif what == "notifications":
            enabled = self.config.get_notifications()
            self._notifier.enabled = enabled
            self.toast("Notifications on" if enabled else "Notifications off")
        else:
            self.toast(f"Sync mode: {what}")

    # ------------------------------------------------------------------ #
    # Servers
    # ------------------------------------------------------------------ #
    def _on_tab_switched(self, stack, _param) -> None:
        """The visible tab *is* the active server — that's what a tab means."""
        name = stack.get_visible_child_name()
        if self._rebuilding or not name or name == "none":
            return
        # Re-aim the Server/Settings switcher even when the active server is
        # unchanged: the stack it points at belongs to the visible page.
        self._bind_sub_switcher(name)
        if self.config.get_active_server_name() == name:
            return
        if self.config.set_active_server(name):
            self.refresh_summary()

    def open_server_dialog(self, server: dict = None) -> None:
        ServerDialog(server, on_saved=self._on_server_saved).present(self)

    def _on_edit_server(self, _button, name) -> None:
        self.open_server_dialog(self.config.get_server(name))

    def _on_server_saved(self, values) -> None:
        # save_server carries the pairs across a rename; remove-then-add, which
        # this used to do, would have deleted them with the old name.
        self.config.save_server(
            values["name"], values["ip"], values["username"],
            values["password"], values["share"], values["port"],
            orig_name=values.get("orig_name"),
            encrypt=values.get("encrypt", False),
            root=values.get("root"),
        )
        self.config.set_active_server(values["name"])
        self.refresh_servers()
        self.refresh_summary()
        self.toast(f"Saved server '{values['name']}'")

    def _on_remove_server(self, _button, name) -> None:
        count = self.config.count_pairs(name)
        pairs_note = (f" Its {count} sync pair(s) are removed too."
                      if count else "")
        _confirm(self, f"Remove '{name}'?",
                 f"The server and its saved password are deleted.{pairs_note} "
                 "Nothing on the server itself is touched.",
                 "Remove", self._on_remove_server_response, name)

    def _on_remove_server_response(self, _dialog, response, name) -> None:
        if response != "remove":
            return
        self.config.remove_server(name)
        self.refresh_servers()
        self.refresh_summary()
        self.toast(f"Removed server '{name}'")

    # ------------------------------------------------------------------ #
    # Pairs
    # ------------------------------------------------------------------ #
    def open_pair_dialog(self, pair: dict = None, server_name: str = None) -> None:
        """`server_name` is the tab the pair belongs to — pairs are per-server."""
        target = server_name or self.config.get_active_server_name()
        PairDialog(pair, on_saved=lambda values: self._on_pair_saved(values, target),
                   server_name=target,
                   # Enables the Browse button on the destination field. None
                   # when no server is configured yet, which leaves the field
                   # typed-only rather than offering a browser that can't
                   # connect to anything.
                   server=self.config.get_server(target) if target else None,
                   ).present(self)

    def _on_edit_pair(self, _button, name, server_name) -> None:
        self.open_pair_dialog(self.config.get_pair(name, server_name),
                              server_name=server_name)

    def _on_pair_saved(self, values, server_name) -> None:
        original = values.get("orig_name")
        try:
            if original:
                name = self.config.update_pair(original, values["local"],
                                               values["dest"],
                                               values["name"] or original,
                                               server=server_name)
                verb = "Updated"
            else:
                name = self.config.add_pair(values["local"], values["dest"],
                                            values["name"], server=server_name)
                verb = "Added"
        except ValueError as exc:
            self.toast(str(exc))
            return
        self.refresh_pairs(server_name)
        self._refresh_tab_badge(server_name)
        self.toast(f"{verb} pair '{name}' on '{server_name}'")

    def _on_remove_pair(self, _button, name, server_name) -> None:
        _confirm(self, f"Remove '{name}'?",
                 f"This only removes the pair from '{server_name}' in NASync. "
                 "No files are deleted, locally or on the server.",
                 "Remove", self._on_remove_pair_response, name, server_name)

    def _on_remove_pair_response(self, _dialog, response, name, server_name) -> None:
        if response != "remove":
            return
        self.config.remove_pair(name, server=server_name)
        self.refresh_pairs(server_name)
        self._refresh_tab_badge(server_name)
        self.toast(f"Removed pair '{name}'")

    def _refresh_tab_badge(self, server_name: str) -> None:
        page = self._stack.get_child_by_name(server_name)
        if page is not None:
            self._stack.get_page(page).set_badge_number(
                self.config.count_pairs(server_name))
        self.refresh_summary()      # the idle line counts pairs too

    # ------------------------------------------------------------------ #
    # Connection test
    # ------------------------------------------------------------------ #
    def test_connection(self) -> None:
        settings = self.config.get_server_config()
        if not settings["ip"]:
            self.toast("No server selected.")
            return
        self.toast("Testing connection…")

        def work():
            SAMBAClient(settings["ip"], settings["username"], settings["password"],
                        settings["share"], port=settings["port"],
                        encrypt=settings.get("encrypt", False)).connect()
            return True

        run_async(work,
                  on_done=lambda _r: self.toast("Connection OK"),
                  on_error=lambda e: self.toast(str(e)))

    # ------------------------------------------------------------------ #
    # Transfers
    # ------------------------------------------------------------------ #
    def confirm_restore(self) -> None:
        _confirm(self, "Restore from the server?",
                 "Files on the server overwrite the matching local files. "
                 "Local changes that haven't been synced will be lost.",
                 "Restore",
                 lambda _d, r: self.start_transfer("pull", False) if r == "restore" else None)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            for button in (self._sync_button, self._dryrun_button,
                           self._restore_button):
                button.set_sensitive(False)
        else:
            # Coming out of a transfer must not re-enable the buttons on a
            # window that has no server to sync to.
            self._set_actions_sensitive(
                bool(self.config.get_active_server_name()))
        self._cancel_button.set_sensitive(busy)

    def start_transfer(self, direction: str, dry_run: bool,
                       confirmed: list = None) -> None:
        """Run a sync, dry run or restore.

        `confirmed` names pairs whose mirror run was held back and that a
        person has now agreed may delete; only those are run, with that yes.
        """
        if self._busy or self._bg_status is not None:
            return
        if not self.config.get_active_server_name():
            self.toast("Pick a server first.")
            return

        self._cancel_event = threading.Event()
        self._started_at = time.monotonic()
        self._errors = []
        self._set_busy(True)
        self._progress_bar.set_fraction(0.0)
        self._details_button.set_visible(False)
        # From here there is a running commentary worth showing; it stays up
        # afterwards so a finished run's file list is still readable.
        self._log_scroll.set_visible(True)
        verb = "Syncing" if direction == "push" else "Restoring"
        self._progress_title.set_label(f"{verb}{' (dry run)' if dry_run else ''}…")
        self._progress_detail.set_label("Starting…")

        self._current_pair = None
        self._current_file = None
        syncer = Syncer(
            pair_progress=self._thread_pair_progress,
            reporter=self._collect_report,
            progress=self._thread_file_progress,
            byte_progress=self._thread_byte_progress,
            cancel_event=self._cancel_event,
        )

        def work():
            if confirmed:
                merged = None
                for name in confirmed:
                    result = syncer.sync_pair(name, allow_mass_delete=True)
                    merged = _merge_results(merged, result)
                    if result.get("cancelled"):
                        break
                return merged
            if direction == "push":
                return syncer.sync_pair(dry_run=dry_run)
            return syncer.restore_pair(dry_run=dry_run)

        self._last_dry_run = dry_run
        run_async(work, on_done=self._on_transfer_done, on_error=self._on_transfer_error)

    def cancel_transfer(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
            self._progress_title.set_label("Cancelling…")
            self._cancel_button.set_sensitive(False)
        elif self._bg_status is not None:
            # Not ours: ask whichever process is running it to stop.
            if activity.request_cancel(self.config.config_dir):
                self._bg_cancelling = True
                self._progress_title.set_label("Cancelling…")
                self._cancel_button.set_sensitive(False)

    def _collect_report(self, message: str) -> None:
        """Keep the failure lines out of the reporter stream, drop the rest.

        Called on the worker thread, so it only appends to a list that the
        main loop reads once the run is over. Files with SMB-illegal names are
        already counted as `unsupported`, so they aren't failures.
        """
        if len(self._errors) >= 200:      # a pathological run shouldn't grow unbounded
            return
        text = message.strip()
        if text.startswith("❌"):
            self._errors.append(text.lstrip("❌ "))
        elif text.startswith("🛑"):
            self._errors.append(text.lstrip("🛑 "))
        elif text.startswith("!") and "name illegal on SMB" not in text:
            self._errors.append(text.lstrip("! "))

    def show_error_details(self) -> None:
        dialog = Adw.AlertDialog(heading="Transfer errors",
                                 body=f"{len(self._errors)} item(s) did not transfer.")
        text = Gtk.Label(label="\n".join(self._errors), xalign=0)
        text.set_selectable(True)
        text.set_wrap(True)
        text.add_css_class("monospace")
        text.add_css_class("caption")
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(220)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(text)
        dialog.set_extra_child(scroller)
        dialog.add_response("close", "Close")
        dialog.present(self)

    # -- progress callbacks arrive on a worker thread ------------------- #
    def _thread_pair_progress(self, index, total, name, verb) -> None:
        on_main(self._update_pair_progress, index, total, name, verb)

    def _thread_file_progress(self, done, total, name, verb) -> None:
        on_main(self._update_file_progress, done, total, name, verb)

    def _thread_byte_progress(self, done, total, name, verb) -> None:
        # Dropped here, on the worker thread, rather than queued and discarded
        # later: a gigabit LAN produces one of these per 1 MiB chunk — over a
        # hundred a second, each relaying out a label — and the idle queue is
        # unbounded, so anything queued still has to be drained. The last
        # update of a file always goes through, so the bar still lands on full.
        now = time.monotonic()
        if done < total and now - self._last_byte_update < self.BYTE_PROGRESS_INTERVAL:
            return
        self._last_byte_update = now
        on_main(self._update_byte_progress, done, total, name, verb)

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - (self._started_at or time.monotonic()))
        return f"{seconds // 60}:{seconds % 60:02d} elapsed"

    @staticmethod
    def _pair_fraction(within: float) -> float:
        """Clamp a position within the current pair; see `_update_pair_progress`."""
        return min(max(within, 0.0), 1.0)

    def _update_pair_progress(self, index, total, name, verb) -> None:
        """A new pair restarts the bar, since the bar measures that pair.

        Comparing a large unchanged folder takes real time and moves no files,
        so the detail line names the pair and says what it's doing — otherwise
        a bar sitting at 0% through a long compare reads as hung.
        """
        self._current_pair = (index, total, name)
        self._current_file = None
        self._progress_bar.set_fraction(0.0)
        self._progress_log.set_label(
            f"{name}  ·  pair {index} of {total}  ·  comparing files…"
            f"  ·  {self._elapsed()}")

    def _update_file_progress(self, done, total, name, verb) -> None:
        self._current_file = (done, total, name)
        if total:
            self._progress_bar.set_fraction(self._pair_fraction(done / total))
        counter = f"{done} of {total} files" if total else f"{done} files"
        self._progress_log.set_label(
            f"{self._pair_prefix()}{verb} {name}  ·  {counter}  ·  {self._elapsed()}")

    def _update_byte_progress(self, done, total, name, verb) -> None:
        # Blend within-file progress into the overall bar so a single large
        # file still moves the needle instead of sitting still.
        current = self._current_file
        if current and current[1] and total:
            index, files_total, _ = current
            within = ((index - 1) + min(done / total, 1.0)) / files_total
            self._progress_bar.set_fraction(self._pair_fraction(within))
        if total:
            self._progress_log.set_label(
                f"{self._pair_prefix()}{name} — "
                f"{_format_bytes(done)} of {_format_bytes(total)}")

    def _pair_prefix(self) -> str:
        if not self._current_pair:
            return ""
        index, total, name = self._current_pair
        return f"{name} ({index}/{total})  ·  " if total > 1 else f"{name}  ·  "

    def _notify_unattended(self, summary: str, body: str,
                           urgency: str = "normal") -> None:
        """Notify only when the window isn't the one being looked at.

        A notification for a sync whose progress bar is on screen is noise;
        one for a sync started before switching away is the whole point. The
        send shells out, so it goes off the main loop — a notification daemon
        that hangs must not take the window with it.
        """
        if self.is_active():
            return
        self._notifier.enabled = self.config.get_notifications()
        run_async(lambda: self._notifier.notify(summary, body, urgency=urgency))

    def _on_transfer_done(self, result) -> None:
        self._set_busy(False)
        self._cancel_event = None
        if result.get("cancelled"):
            self._progress_title.set_label("Cancelled")
            self._progress_detail.set_label("The transfer was stopped part-way.")
            self._progress_bar.set_fraction(0.0)
            self.toast("Transfer cancelled")
            return

        self._progress_bar.set_fraction(1.0)
        unsupported = result.get("unsupported", 0)
        # A pair can fail outright ("failed") or individual files within an
        # otherwise fine pair can fail ("errors"); both mean data didn't land.
        problems = result.get("failed", 0) + result.get("errors", 0)
        self._progress_title.set_label(
            f"Finished with {problems} error(s)" if problems else "Complete")

        parts = [f"{result.get('transferred', 0)} transferred",
                 f"{result.get('skipped', 0)} unchanged",
                 f"{result.get('deleted', 0)} removed"]
        if result.get("moved"):
            parts.append(f"{result['moved']} moved")
        if unsupported:
            parts.append(f"{unsupported} skipped (name illegal on SMB)")
        held = [r for r in result.get("refused", []) if not r.get("dry_run")]
        would_hold = [r for r in result.get("refused", []) if r.get("dry_run")]
        if held:
            parts.append(f"{len(held)} held back")
        if would_hold:
            parts.append("a real run would stop at "
                         + ", ".join(r["pair"] for r in would_hold))
        if result.get("overlapping"):
            parts.append(f"{len(result['overlapping'])} skipped (shared folder)")
        self._progress_detail.set_label("  ·  ".join(parts))
        self._details_button.set_visible(bool(self._errors))
        self.toast("Sync complete" if not problems
                   else f"Finished with {problems} error(s)")
        self._notify_unattended(
            f"Sync finished with {problems} error(s)" if problems else "Sync complete",
            f"{self.config.get_active_server_name()}  ·  " + "  ·  ".join(parts))
        confirmable = [r for r in held if r.get("confirmable")]
        if confirmable:
            self._confirm_mass_delete(confirmable)

    def _confirm_mass_delete(self, held: list) -> None:
        """Ask before letting held-back mirror runs delete.

        Nothing has been deleted when this appears: the run stopped before
        touching the server. Saying so is half the point of the dialog — the
        other half is that the default answer keeps the files.
        """
        lines = [f"• '{r['pair']}' would delete {r['deletes']:,} of "
                 f"{r['total']:,} file(s) in its server folder." for r in held]
        dialog = Adw.AlertDialog(
            heading="Delete files on the server?",
            body="\n".join(lines) + "\n\nNothing has been deleted yet. This "
                 "usually means a folder moved, a disk didn't mount, or a pair "
                 "points somewhere unexpected. Dry run lists every file first.")
        dialog.add_response("keep", "Keep Them")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        names = [r["pair"] for r in held]
        dialog.connect("response", lambda _d, response: (
            self.start_transfer("push", False, confirmed=names)
            if response == "delete" else None))
        dialog.present(self)

    # ------------------------------------------------------------------ #
    # Syncs run by another process
    # ------------------------------------------------------------------ #
    def _poll_activity(self) -> bool:
        """Show a sync this window didn't start: the service's, or the CLI's.

        A file read and a lock probe, once a second — cheap enough to leave
        running for the life of the window. Returns True to keep the timer.
        """
        if self._busy:
            return True                      # our own run; its progress is up
        status = activity.read_status(self.config.config_dir)
        if status is not None and status.get("pid") == os.getpid():
            status = None
        if status is not None:
            self._show_background(status, first=self._bg_status is None)
        elif self._bg_status is not None:
            self._background_finished(self._bg_status)
        self._bg_status = status
        return True

    def _show_background(self, status: dict, first: bool) -> None:
        if first:
            for button in (self._sync_button, self._dryrun_button,
                           self._restore_button):
                button.set_sensitive(False)
            self._cancel_button.set_sensitive(True)
            self._details_button.set_visible(False)
            self._log_scroll.set_visible(True)
            self._bg_cancelling = False
        if not self._bg_cancelling:
            self._progress_title.set_label(
                "Background sync running…" if status.get("source") == "background"
                else "Sync running elsewhere…")
        self._progress_bar.set_fraction(activity.progress_fraction(status))
        line = activity.describe(status)
        self._progress_detail.set_label(line[0].upper() + line[1:])
        if status.get("files_total"):
            self._progress_log.set_label(
                f"{status.get('verb', 'Working')} {status.get('files_done', 0)}"
                f"/{status['files_total']} — {status.get('file', '')}")
        else:
            self._progress_log.set_label(
                f"Comparing '{status.get('pair', '')}'…" if status.get("pair")
                else "Starting…")

    def _background_finished(self, was: dict) -> None:
        """The other process is done: say what it did, then hand back control."""
        self._cancel_button.set_sensitive(False)
        self._set_actions_sensitive(bool(self.config.get_active_server_name()))
        self._log_scroll.set_visible(False)
        self._bg_cancelling = False
        last = activity.read_last(self.config.config_dir)
        if not last or abs(last.get("started", 0) - was.get("started", -1)) > 1:
            self._progress_title.set_label("Idle")
            self._progress_bar.set_fraction(0.0)
            self.refresh_summary()
            return
        outcome = last.get("result", {})
        problems = outcome.get("failed", 0) + outcome.get("errors", 0)
        who = ("Background sync" if last.get("source") == "background"
               else "Sync")
        if outcome.get("cancelled"):
            self._progress_title.set_label(f"{who} cancelled")
            self._progress_bar.set_fraction(0.0)
        else:
            self._progress_title.set_label(
                f"{who} finished with {problems} error(s)" if problems
                else f"{who} complete")
            self._progress_bar.set_fraction(1.0)
        self._progress_detail.set_label(activity.describe_last(last))

    def _repair_installs(self) -> None:
        """Repoint a launcher or service that points at a NASync that's gone.

        A checkout moved, or a Python upgrade under its virtualenv, leaves both
        failing silently — the launcher opens nothing, the service restarts
        every thirty seconds forever. This program evidently runs, so it is
        what they should point at. Off the main loop: checking means running
        the old program once.
        """
        def work():
            fixed = []
            for name, module in (("app launcher", launcher),
                                 ("background service", service)):
                try:
                    if module.repair():
                        fixed.append(name)
                except Exception:          # noqa: BLE001 — best effort
                    pass
            return fixed

        def done(fixed):
            if fixed:
                self.toast(f"Repaired the {' and '.join(fixed)}: it pointed "
                           f"at a copy of NASync that no longer runs")

        run_async(work, on_done=done)

    def _on_transfer_error(self, exc) -> None:
        self._set_busy(False)
        self._cancel_event = None
        self._progress_title.set_label("Failed")
        self._progress_detail.set_label(str(exc))
        self._progress_bar.set_fraction(0.0)
        self.toast(str(exc))
        self._notify_unattended("Sync failed", str(exc), urgency="critical")
