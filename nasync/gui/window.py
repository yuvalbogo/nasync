"""The NASync main window.

Layout follows the shape of the app: a list of saved servers (one active), a
list of sync pairs that target whichever server is active, and a transfer
area that reports progress and offers a cancel button while work is running.
"""

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from nasync.core.config import Config  # noqa: E402
from nasync.core.samba import SAMBAClient  # noqa: E402
from nasync.core.sync import Syncer  # noqa: E402
from nasync.format import format_bytes  # noqa: E402
from nasync.gui.dialogs import (AutomaticPopover, ModePopover, PairDialog,  # noqa: E402
                                ServerDialog, ServerInfoDialog,  # noqa: E402
                                SettingsDialog, StorageDialog)
from nasync.gui.threads import on_main, run_async  # noqa: E402
from nasync.system.notify import Notifier  # noqa: E402

#: How the mode button presents each mode: (label, icon, tooltip). Mirror makes
#: the server a copy of the source and update only ever adds to it, so they get
#: an icon each rather than a warning triangle sitting permanently on the
#: default mode. The tooltip spells out the destructive one, which is the whole
#: reason the mode is on screen at all.
MODE_PRESENTATION = {
    "mirror": ("Mirror", "edit-copy-symbolic",
               "Mirror mode — the server is made to match your local folders "
               "exactly. Files removed locally are deleted on the server."),
    "update": ("Update", "list-add-symbolic",
               "Update mode — new and changed files are copied. Nothing is "
               "deleted on the server."),
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


class NasyncWindow(Adw.ApplicationWindow):

    #: Smallest gap between byte-progress repaints. No eye reads faster than
    #: this, and the callback arrives far more often — see `_thread_byte_progress`.
    BYTE_PROGRESS_INTERVAL = 0.1

    def __init__(self, application):
        super().__init__(application=application, title="NASync")
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
        # For syncs the user walked away from; the window itself reports the
        # ones they stayed to watch.
        self._notifier = Notifier(enabled=self.config.get_notifications())

        self._toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
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

        # One page per server, switched by tabs. Each page owns its server's
        # pairs, so a tab is the whole context for a sync. The tabs sit in
        # their own bar rather than the header, which keeps the app's title
        # and tagline where they belong.
        self._stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self._stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        self._switcher_bar = Gtk.Box()
        self._switcher_bar.set_halign(Gtk.Align.CENTER)
        self._switcher_bar.set_margin_top(6)
        self._switcher_bar.set_margin_bottom(6)
        self._switcher_bar.append(switcher)
        self._switcher_bar.set_visible(False)
        toolbar.add_top_bar(self._switcher_bar)

        self._stack.connect("notify::visible-child-name", self._on_tab_switched)
        toolbar.set_content(self._stack)

        toolbar.add_bottom_bar(self._build_action_bar())

        self._toasts.set_child(toolbar)
        self.set_content(self._toasts)

        self._pair_groups = {}
        self.refresh_servers()
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
        section.append("Settings", "win.settings")
        section.append("About NASync", "app.about")
        model.append_section(None, section)

        button.set_menu_model(model)

    def _build_server_page(self, server: dict) -> Adw.PreferencesPage:
        """One server: connection details (top), automation cards, then pairs dropdown."""
        name = server["name"]
        page = Adw.PreferencesPage()

        # ──── SERVER SECTION (stays at top) ────
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

        # ──── AUTOMATION CARDS ────
        page.add(self._build_automation_cards(name))

        # ──── SYNC PAIRS DROPDOWN ────
        pairs_group = Adw.PreferencesGroup(title="Sync pairs")
        self._pairs_dropdown = self._build_pairs_dropdown(name)
        pairs_group.add(self._pairs_dropdown)
        page.add(pairs_group)

        self._pair_groups[name] = {"group": pairs_group, "dropdown": self._pairs_dropdown}
        return page

    def _build_automation_cards(self, server_name: str) -> Adw.PreferencesGroup:
        """Cards showing quick access to popular automation settings."""
        group = Adw.PreferencesGroup(title="Quick settings")

        # Automatic sync card
        auto_row = Adw.ActionRow(
            title="Automatic syncing",
            subtitle="Schedule and live sync settings",
        )
        auto_button = Gtk.Button(label="Edit")
        auto_button.add_css_class("flat")
        auto_button.connect("clicked", lambda *_: self.open_settings())
        auto_row.add_suffix(auto_button)
        group.add(auto_row)

        # LAN-only card
        lan_row = Adw.ActionRow(title="Sync only on home network")
        lan_switch = Gtk.Switch()
        lan_switch.set_active(self.config.get_automation().get("lan_only", True))
        lan_switch.connect("notify::active", self._on_lan_only_changed)
        lan_switch.set_valign(Gtk.Align.CENTER)
        lan_row.add_suffix(lan_switch)
        group.add(lan_row)

        # Notifications card
        notif_row = Adw.ActionRow(title="Sync notifications")
        notif_switch = Gtk.Switch()
        notif_switch.set_active(self.config.get_notifications())
        notif_switch.connect("notify::active", self._on_notif_changed)
        notif_switch.set_valign(Gtk.Align.CENTER)
        notif_row.add_suffix(notif_switch)
        group.add(notif_row)

        return group

    def _build_pairs_dropdown(self, server_name: str) -> Gtk.Widget:
        """Dropdown showing sync pairs for this server."""
        return self._build_pairs_dropdown_content(server_name)

    def _build_pairs_dropdown_content(self, server_name: str) -> Gtk.Widget:
        """Build or rebuild the pairs dropdown content."""
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        container.set_margin_top(6)
        container.set_margin_bottom(6)
        container.set_margin_start(6)
        container.set_margin_end(6)

        # Summary row showing pair count
        pairs = self.config.get_pairs(server_name)
        count = len(pairs) if pairs else 0
        summary_label = Gtk.Label(
            label=f"{count} pair{'s' if count != 1 else ''}" if count else "No pairs yet"
        )
        summary_label.add_css_class("dim-label")

        # Expandable section
        expander = Gtk.Expander(label="Show all pairs")
        expander.set_child(self._build_pairs_list(server_name))

        container.append(summary_label)
        container.append(expander)
        return container

    def _build_pairs_list(self, server_name: str) -> Gtk.Widget:
        """Scrollable list of sync pairs for this server."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        pairs = self.config.get_pairs(server_name)
        if not pairs:
            row = Gtk.Label(label="Add a folder to get started")
            row.add_css_class("dim-label")
            box.append(row)
        else:
            for pair in pairs:
                # Use an Adw.ActionRow for consistent styling and better visibility
                pair_row = Adw.ActionRow(
                    title=pair["name"],
                    subtitle=f"{pair['local']}  →  {pair['server']}"
                )
                pair_row.set_subtitle_lines(1)

                # Add edit and remove buttons
                pair_row.add_suffix(_icon_button("document-edit-symbolic", "Edit",
                                                self._on_edit_pair, pair["name"], server_name))
                pair_row.add_suffix(_icon_button("user-trash-symbolic", "Remove",
                                                self._on_remove_pair, pair["name"], server_name))
                box.append(pair_row)

        # Add button at bottom
        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_row.set_margin_start(12)
        add_row.set_margin_top(6)
        add_row.set_margin_bottom(6)
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.set_tooltip_text("Add a sync pair")
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", lambda *_, s=server_name: self.open_pair_dialog(server_name=s))
        add_row.append(add_btn)
        add_row.append(Gtk.Label(label="Add pair"))
        box.append(add_row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(box)
        # Don't limit height - let the expander expand naturally
        scroll.set_propagate_natural_height(True)
        scroll.set_propagate_natural_width(True)
        return scroll

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
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        bar.set_margin_top(10)
        bar.set_margin_bottom(12)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.append(self._build_state_bar())
        bar.append(self._build_buttons())
        bar.append(self._build_progress())
        return bar

    def _build_state_bar(self) -> Gtk.Widget:
        """What a sync does, and whether one happens by itself — as controls.

        Both were previously a caption here and switches two clicks deep in the
        menu: readable but not changeable, which is the wrong way round for the
        mode that deletes on the server. Each button now reports its own state
        and opens the settings behind it.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._mode_content = Adw.ButtonContent()
        self._mode_popover = ModePopover(self.config,
                                         on_changed=self._on_settings_changed)
        self._mode_button = Gtk.MenuButton()
        self._mode_button.set_child(self._mode_content)
        self._mode_button.set_popover(self._mode_popover)
        self._mode_button.set_always_show_arrow(True)
        row.append(self._mode_button)

        self._auto_content = Adw.ButtonContent()
        self._auto_popover = AutomaticPopover(self.config,
                                              on_changed=self._on_settings_changed)
        self._auto_button = Gtk.MenuButton()
        self._auto_button.set_child(self._auto_content)
        self._auto_button.set_popover(self._auto_popover)
        self._auto_button.set_always_show_arrow(True)
        row.append(self._auto_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        return row

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
        """State centred on the bar, with a scrollable log underneath.

        The state word is readable at a glance above the bar. The log below
        uses monospace font and scrolls, so file listings stay readable even
        as they update rapidly during sync.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self._progress_title = Gtk.Label(label="Idle")
        self._progress_title.add_css_class("heading")
        self._progress_title.set_halign(Gtk.Align.CENTER)
        box.append(self._progress_title)

        self._progress_bar = Gtk.ProgressBar(show_text=False)
        box.append(self._progress_bar)

        # Scrollable log with monospace font for readability
        log_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_container.set_margin_top(4)

        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_max_content_height(120)
        log_scroll.set_propagate_natural_height(True)

        self._progress_log = Gtk.Label(label="Pick a server, then Sync.")
        self._progress_log.add_css_class("dim-label")
        self._progress_log.set_selectable(True)
        self._progress_log.set_wrap(True)
        self._progress_log.set_halign(Gtk.Align.START)
        self._progress_log.set_valign(Gtk.Align.START)
        self._progress_log.set_margin_start(6)
        self._progress_log.set_margin_end(6)

        log_scroll.set_child(self._progress_log)
        log_container.append(log_scroll)

        detail_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        detail_row.set_halign(Gtk.Align.CENTER)

        self._progress_detail = Gtk.Label(label="")
        self._progress_detail.add_css_class("dim-label")
        self._progress_detail.add_css_class("caption")
        self._progress_detail.set_visible(False)
        detail_row.append(self._progress_detail)

        self._details_button = Gtk.Button(label="Show errors")
        self._details_button.add_css_class("flat")
        self._details_button.set_visible(False)
        self._details_button.connect("clicked", lambda *_: self.show_error_details())
        detail_row.append(self._details_button)

        log_container.append(detail_row)
        box.append(log_container)
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

        servers = self.config.list_servers()
        if not servers:
            self._stack.add_titled(self._build_empty_page(), "none", "No servers")
            self._switcher_bar.set_visible(False)
            self._set_actions_sensitive(False)
            return

        for server in servers:
            page = self._stack.add_titled(self._build_server_page(server),
                                          server["name"], server["name"])
            page.set_icon_name("network-server-symbolic")
            count = self.config.count_pairs(server["name"])
            # A badge on the tab, so "which of these has folders set up?" is
            # answerable without clicking through them.
            page.set_badge_number(count)
            page.set_needs_attention(count == 0)

        active = self.config.get_active_server_name() or servers[0]["name"]
        self._stack.set_visible_child_name(active)
        # With one server the tab is just a label for the only thing on screen,
        # and the page already names it.
        self._switcher_bar.set_visible(len(servers) > 1)
        self._set_actions_sensitive(True)

    def refresh_pairs(self, server_name: str = None) -> None:
        """Refresh pairs display after changes (add/remove/edit)."""
        name = server_name or self.config.get_active_server_name()
        entry = self._pair_groups.get(name)
        if entry is None:
            return

        group = entry["group"]

        # Rebuild the pairs dropdown content
        old_dropdown = entry.get("dropdown")
        if old_dropdown:
            group.remove(old_dropdown)

        new_dropdown = self._build_pairs_dropdown_content(name)
        group.add(new_dropdown)
        self._pair_groups[name]["dropdown"] = new_dropdown

    def _set_actions_sensitive(self, sensitive: bool) -> None:
        """Nothing to sync without a server, so don't offer to."""
        if self._busy:
            return
        for button in (self._sync_button, self._dryrun_button, self._restore_button):
            button.set_sensitive(sensitive)

    def _on_lan_only_changed(self, switch, param) -> None:
        """Update LAN-only setting when toggle changes."""
        automation = self.config.get_automation()
        automation["lan_only"] = switch.get_active()
        self.config.set_automation(automation)

    def _on_notif_changed(self, switch, param) -> None:
        """Update notification setting when toggle changes."""
        self.config.set_notifications(switch.get_active())
        self._notifier.enabled = switch.get_active()

    def refresh_summary(self) -> None:
        """Put the current state on the two buttons that also change it."""
        mode = self.config.get_sync_mode()
        label, icon, tooltip = MODE_PRESENTATION[mode]
        self._mode_content.set_label(label)
        self._mode_content.set_icon_name(icon)
        self._mode_button.set_tooltip_text(tooltip)

        icon, label, tooltip = self._automatic_state()
        self._auto_content.set_icon_name(icon)
        self._auto_content.set_label(label)
        self._auto_button.set_tooltip_text(tooltip)

        self._sync_button.set_tooltip_text(f"Sync to the server ({mode} mode)")

        # Idle text describes the tab you're on; a finished run's summary stays.
        if not self._busy and self._progress_title.get_label() == "Idle":
            active = self.config.get_active_server_name()
            if not active:
                self._progress_detail.set_label("Add a server to get started.")
            else:
                count = self.config.count_pairs(active)
                self._progress_detail.set_label(
                    f"Sync sends {count} folder(s) to '{active}'." if count
                    else f"'{active}' has no sync pairs yet.")

    def _automatic_state(self) -> tuple:
        """(icon, label, tooltip) describing automatic syncing at a glance.

        The icon carries the state and the label the detail, so "is this
        machine syncing by itself?" is answerable without reading. "Paused"
        earns its own look: the triggers are set up and deliberately not
        running, which is neither on nor off.
        """
        automation = self.config.get_automation()
        triggers = []
        if automation["live_sync"]:
            triggers.append("Live")
        if automation["scheduled_sync"]:
            triggers.append(f"daily {automation['scheduled_time']}")

        if not triggers:
            return ("media-playback-pause-symbolic", "Automatic: off",
                    "Nothing syncs unless you press Sync. Click to turn on "
                    "live or scheduled syncing.")
        if not automation["background"]:
            return ("media-playback-pause-symbolic", "Paused",
                    f"{' · '.join(triggers)} are set up, but 'Run in the "
                    "background' is off, so nothing runs them.")

        icon = ("emblem-synchronizing-symbolic" if automation["live_sync"]
                else "alarm-symbolic")
        notifications = ("" if self.config.get_notifications()
                         else "  Notifications are off.")
        return (icon, " · ".join(triggers),
                f"Syncing automatically: {' · '.join(triggers)}.{notifications}")

    def toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, timeout=3))

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def open_settings(self) -> None:
        SettingsDialog(self.config,
                       on_changed=self._on_settings_changed).present(self)

    def open_server_info(self) -> None:
        """What the active server is, and what was negotiated with it."""
        server = self._active_server()
        if not server:
            self.toast("Add a server first.")
            return
        ServerInfoDialog(server).present(self)

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
        """One handler for both the popovers and the Settings dialog.

        They write the same config, so whichever one made the change, the
        buttons on the main page have to catch up with it.
        """
        if what == "open-settings":
            self.open_settings()
            return

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
            count = self.config.count_pairs(server_name)
            stack_page = self._stack.get_page(page)
            stack_page.set_badge_number(count)
            stack_page.set_needs_attention(count == 0)
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

    def start_transfer(self, direction: str, dry_run: bool) -> None:
        if self._busy:
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
            if direction == "push":
                return syncer.sync_pair(dry_run=dry_run)
            return syncer.restore_pair(dry_run=dry_run)

        run_async(work, on_done=self._on_transfer_done, on_error=self._on_transfer_error)

    def cancel_transfer(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
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
        if unsupported:
            parts.append(f"{unsupported} skipped (name illegal on SMB)")
        self._progress_detail.set_label("  ·  ".join(parts))
        self._details_button.set_visible(bool(self._errors))
        self.toast("Sync complete" if not problems
                   else f"Finished with {problems} error(s)")
        self._notify_unattended(
            f"Sync finished with {problems} error(s)" if problems else "Sync complete",
            f"{self.config.get_active_server_name()}  ·  " + "  ·  ".join(parts))

    def _on_transfer_error(self, exc) -> None:
        self._set_busy(False)
        self._cancel_event = None
        self._progress_title.set_label("Failed")
        self._progress_detail.set_label(str(exc))
        self._progress_bar.set_fraction(0.0)
        self.toast(str(exc))
        self._notify_unattended("Sync failed", str(exc), urgency="critical")
