"""Dialogs for servers, sync pairs, and app settings.

All of them are built from preference rows, which is what gives them the
native GNOME look without any custom styling. The server dialog can test a
connection and populate its share list in place; the pair dialog opens a real
folder picker rather than asking the user to type a path.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from nasync.core.samba import SAMBAClient  # noqa: E402
from nasync.format import format_bytes, format_volume  # noqa: E402
from nasync.gui.threads import run_async  # noqa: E402
from nasync.system import notify, service  # noqa: E402


#: The two sync modes, in the order they appear in the settings dialog.
#: (mode, title, subtitle) — the subtitles are the only place the user is told
#: which one deletes, so they say it plainly rather than in terms of "mirror".
SYNC_MODE_CHOICES = (
    ("update", "Update",
     "Copies new and changed files to the server. Files you delete locally are "
     "kept on the server. Nothing is ever deleted."),
    ("mirror", "Mirror",
     "Makes the server match your local folders exactly. Files that are on the "
     "server but no longer on this machine are deleted."),
)


#: The automatic-syncing switches, in display order: attribute name, title, and
#: the sentence explaining it. Both surfaces that offer these read from here, so
#: a switch can't be called one thing in the popover and another in Settings.
AUTOMATION_ROWS = (
    ("live", "Live sync",
     "Syncs a few seconds after a file changes. Runs only while this machine "
     "is on the same network as the server, so a laptop away from home catches "
     "up when it gets back."),
    ("scheduled", "Scheduled sync",
     "Syncs once a day. A run missed because the machine was asleep or away "
     "happens at the next opportunity that day."),
    ("background", "Run in the background",
     "Keeps the triggers above running once NASync is closed."),
    ("notifications", "Notify me about syncs",
     "Says when an automatic sync starts and what it moved, and speaks up when "
     "a scheduled one couldn't run at all."),
)

AUTOMATION_TITLE = {key: title for key, title, _ in AUTOMATION_ROWS}
AUTOMATION_SUBTITLE = {key: subtitle for key, _, subtitle in AUTOMATION_ROWS}

#: Why the background service isn't doing anything, for the cases systemd can't
#: describe itself: the settings, not the unit, are what's holding it back.
_SERVICE_OFF = ("Off — the triggers above stay set up, but nothing runs them "
                "until you turn this back on.")
_SERVICE_IDLE = "Starts when you turn on live or scheduled sync."


class AutomationSettings:
    """The automatic-syncing settings, minus any particular widget.

    Two surfaces offer these — the Settings dialog and the main window's
    popover — and both need the same three things: write the change, reconcile
    the background service off the UI thread, and say what the service is doing
    now. Keeping that here is what stops the two from drifting apart, which is
    otherwise inevitable for a panel that exists twice.
    """

    def __init__(self, config, on_changed=None, on_detail=None):
        self._config = config
        self._on_changed = on_changed
        # Called with the service's one-line status whenever it might have
        # changed; each surface renders it in whatever widget it keeps for it.
        self._on_detail = on_detail

    @property
    def config(self):
        return self._config

    def get(self) -> dict:
        return self._config.get_automation()

    # -- writes --------------------------------------------------------- #
    def set_live(self, value: bool) -> None:
        self._write(live_sync=value)

    def set_scheduled(self, value: bool) -> None:
        self._write(scheduled_sync=value)

    def set_background(self, value: bool) -> None:
        self._write(background=value)

    def set_time(self, value: str) -> None:
        self._config.set_automation(scheduled_time=value)
        self._changed("automation")
        # Only worth reconciling the service if it's actually scheduling.
        if self.get()["scheduled_sync"]:
            self.reconcile()

    def set_notifications(self, value: bool) -> None:
        self._config.set_notifications(value)
        self._changed("notifications")

    def _write(self, **values) -> None:
        self._config.set_automation(**values)
        self.reconcile()

    # -- the background service ----------------------------------------- #
    def reconcile(self) -> None:
        """Match the systemd unit to the settings, off the UI thread.

        `systemctl enable --now` shells out, and a switch that freezes the
        window while it runs would be its own kind of bug.
        """
        def work():
            return service.apply(self._config)

        def done(status):
            # apply() already returned the fresh status; asking systemd again
            # would be three more subprocesses for an answer we hold.
            self._publish(self.service_state(status=status)[0])
            self._changed("automation")

        def failed(exc):
            self._publish(f"Couldn't start background syncing: {exc}")
            self._changed("automation")

        run_async(work, on_done=done, on_error=failed)

    def service_state(self, automation=None, status=None) -> tuple:
        """``(detail, available)`` — the unit's state and the settings gating it.

        Both answers come from one `service.status()`, which spawns three
        `systemctl` processes: a caller that asked for them separately would
        pay for it twice on a call that already runs on the UI thread.
        """
        automation = automation or self.get()
        status = status or service.status()
        if not status["available"]:
            return status["detail"], False
        if not automation["background"]:
            return _SERVICE_OFF, True
        if not (automation["live_sync"] or automation["scheduled_sync"]):
            return _SERVICE_IDLE, True
        return status["detail"], True

    def _publish(self, detail: str) -> None:
        if self._on_detail:
            self._on_detail(detail)

    def _changed(self, what: str) -> None:
        if self._on_changed:
            self._on_changed(what)


def _pad_spin(spin) -> bool:
    """Show 02, not 2 — a clock with a bare '2' in it reads as broken."""
    spin.set_text(f"{int(spin.get_value()):02d}")
    return True


def make_time_spin(low, high, step, value, on_changed) -> Gtk.SpinButton:
    """A compact clock field.

    Horizontal, so the row keeps the same height as the switch rows beside it —
    stacked +/- buttons made this row twice as tall as its neighbours.
    """
    spin = Gtk.SpinButton.new_with_range(low, high, step)
    spin.set_value(value)
    spin.set_wrap(True)
    spin.set_numeric(True)
    spin.set_width_chars(2)
    spin.set_max_width_chars(2)
    spin.set_valign(Gtk.Align.CENTER)
    spin.connect("output", _pad_spin)
    spin.connect("value-changed", on_changed)
    return spin


class SettingsDialog(Adw.PreferencesDialog):
    """App settings in full: the sync mode, automatic syncing, notifications.

    The main window carries the same automation and mode settings in popovers,
    for the common case of changing them mid-task; this is where they live
    with room for the explanations.

    Instant-apply, as the GNOME HIG expects of a preferences window: there is
    no Save button, a change writes to the config straight away and calls
    ``on_changed(what)`` so the window can update itself.
    """

    def __init__(self, config, on_changed=None):
        super().__init__()
        self._config = config
        self._on_changed = on_changed
        self._automation = AutomationSettings(
            config, on_changed=self._changed,
            on_detail=lambda detail: self._service_row.set_subtitle(detail))

        self.set_title("Settings")
        # Two pages rather than one long scroll: what a sync does is a
        # different question from when one happens.
        page = Adw.PreferencesPage(title="Syncing", name="syncing",
                                   icon_name="folder-remote-symbolic")

        group = Adw.PreferencesGroup(
            title="Sync mode",
            description="What a sync does with files that are on the server but "
                        "not in your local folder.",
        )
        current = config.get_sync_mode()
        self._buttons = {}
        leader = None
        for mode, title, subtitle in SYNC_MODE_CHOICES:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.set_subtitle_lines(0)      # 0 = no limit; these must not ellipsize

            check = Gtk.CheckButton()
            if leader is None:
                leader = check
            else:
                check.set_group(leader)
            check.set_active(mode == current)
            check.set_valign(Gtk.Align.CENTER)
            check.connect("toggled", self._on_mode_toggled, mode)
            row.add_prefix(check)
            row.set_activatable_widget(check)

            self._buttons[mode] = check
            group.add(row)
        page.add(group)

        # The setting reads as symmetric but isn't, so say so here rather than
        # letting someone discover it by restoring and finding nothing removed.
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
        page.add(self._build_security_group())
        self.add(page)

        automatic = Adw.PreferencesPage(title="Automatic", name="automatic",
                                        icon_name="preferences-system-time-symbolic")
        automatic.add(self._build_automation_group())
        automatic.add(self._build_notifications_group())
        self.add(automatic)

    #: What each key source means, in the second person. Deliberately says
    #: where the key *is*, because that is what tells someone whether losing
    #: their keyring loses their passwords.
    _CREDENTIAL_DETAIL = {
        "keyring": ("Your NAS passwords are encrypted, with the key held in "
                    "your login keyring."),
        "portal": ("Your NAS passwords are encrypted, with the key held for "
                   "NASync by the desktop portal."),
        "": ("Your NAS passwords are stored unencrypted, readable by anything "
             "running as you. No keyring or secret service was found on this "
             "machine to hold an encryption key."),
    }

    def _build_security_group(self) -> Adw.PreferencesGroup:
        """State the truth about credential storage, including when it's bad.

        Not a setting — there is nothing here to choose. It is here because the
        answer changes per machine and silence would read as "encrypted", which
        on a headless box it is not.
        """
        group = Adw.PreferencesGroup(title="Security")
        which = self._config.credentials_backend()
        protected = bool(which)
        # Nothing on the way to this dialog reads a password, so the error has
        # to be gone looking for — see `Config.check_credentials`.
        problem = self._config.check_credentials()

        row = Adw.ActionRow(
            title="Saved passwords are encrypted" if protected
                  else "Saved passwords are not encrypted",
            subtitle=self._CREDENTIAL_DETAIL[which],
        )
        row.set_subtitle_lines(0)
        row.set_activatable(False)
        row.add_prefix(Gtk.Image(
            icon_name="channel-secure-symbolic" if protected
                      else "dialog-warning-symbolic"))
        group.add(row)

        # A read that failed is a different state from either of the above, and
        # the only one with something the user can actually do about it.
        if problem:
            row = Adw.ActionRow(title="Saved passwords couldn't be read",
                                subtitle=problem)
            row.set_subtitle_lines(0)
            row.set_activatable(False)
            row.add_prefix(Gtk.Image(icon_name="dialog-error-symbolic"))

            # Without this the app is a dead end: NASync refuses to save over
            # passwords it can't read, and the recovery — entering them again
            # — is itself a save. Destructive, so it confirms first.
            forget = Gtk.Button(label="Forget saved passwords")
            forget.add_css_class("destructive-action")
            forget.set_valign(Gtk.Align.CENTER)
            forget.connect("clicked", self._on_forget_credentials)
            row.add_suffix(forget)
            group.add(row)
        return group

    def _on_forget_credentials(self, _button) -> None:
        confirm = Adw.AlertDialog(
            heading="Forget saved passwords?",
            body="The passwords stored on this machine can't be decrypted, so "
                 "they can't be recovered. Forgetting them lets you enter them "
                 "again. Your servers and sync pairs are not affected.",
        )
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("forget", "Forget")
        confirm.set_response_appearance("forget",
                                        Adw.ResponseAppearance.DESTRUCTIVE)
        confirm.set_default_response("cancel")
        confirm.connect("response", self._on_forget_answered)
        confirm.present(self)

    def _on_forget_answered(self, _dialog, response: str) -> None:
        if response != "forget":
            return
        self._config.reset_credentials()
        self._changed("credentials")

    def _build_notifications_group(self) -> Adw.PreferencesGroup:
        """A background sync is only trustworthy if it says what it did."""
        group = Adw.PreferencesGroup(title="Notifications")

        self._notify_row = self._switch_row("notifications",
                                            self._config.get_notifications(),
                                            self._on_notifications_toggled)
        group.add(self._notify_row)

        # A switch that's on while nothing can ever appear is a lie; say so
        # rather than letting someone conclude syncing itself is broken.
        if not notify.available():
            warning = Adw.ActionRow(
                title="This machine can't show notifications",
                subtitle="Neither gdbus nor notify-send was found, so nothing "
                         "will appear no matter how this is set.",
            )
            warning.set_subtitle_lines(0)
            warning.set_activatable(False)
            warning.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            group.add(warning)
        return group

    def _on_notifications_toggled(self, row, _param) -> None:
        self._automation.set_notifications(row.get_active())

    # ------------------------------------------------------------------ #
    # Automatic syncing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _switch_row(key: str, active: bool, handler) -> Adw.SwitchRow:
        """One switch row, titled and explained from `AUTOMATION_ROWS`."""
        row = Adw.SwitchRow(title=AUTOMATION_TITLE[key],
                            subtitle=AUTOMATION_SUBTITLE[key])
        row.set_subtitle_lines(0)      # 0 = no limit; these must not ellipsize
        row.set_active(active)
        row.connect("notify::active", handler)
        return row

    def _build_automation_group(self) -> Adw.PreferencesGroup:
        automation = self._automation.get()
        group = Adw.PreferencesGroup(
            title="Automatic syncing",
            description="Off until you turn them on. Both sync using the mode "
                        "set on the Syncing page.",
        )

        self._live_row = self._switch_row("live", automation["live_sync"],
                                          self._on_live_toggled)
        group.add(self._live_row)

        self._scheduled_row = self._switch_row("scheduled",
                                               automation["scheduled_sync"],
                                               self._on_scheduled_toggled)
        group.add(self._scheduled_row)

        self._time_row = self._build_time_row(automation["scheduled_time"])
        group.add(self._time_row)

        # What the two switches above actually depend on. Turning this off
        # keeps them configured but stops anything from acting on them.
        self._service_row = self._switch_row("background", automation["background"],
                                             self._on_background_toggled)
        group.add(self._service_row)

        self._refresh_automation_rows()
        return group

    def _build_time_row(self, value: str) -> Adw.ActionRow:
        """HH:MM as two spin buttons — the same shape GNOME Clocks uses."""
        row = Adw.ActionRow(title="Time", subtitle="24-hour clock")
        hour, minute = (int(part) for part in value.split(":"))

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_valign(Gtk.Align.CENTER)

        self._hour_spin = make_time_spin(0, 23, 1, hour, self._on_time_changed)
        box.append(self._hour_spin)

        colon = Gtk.Label(label=":")
        colon.add_css_class("heading")
        box.append(colon)

        self._minute_spin = make_time_spin(0, 59, 5, minute, self._on_time_changed)
        box.append(self._minute_spin)

        row.add_suffix(box)
        return row

    def _refresh_automation_rows(self) -> None:
        """Time only matters when scheduling; the service only when either is on."""
        automation = self._automation.get()
        self._time_row.set_sensitive(automation["scheduled_sync"])
        detail, available = self._automation.service_state(automation)
        self._service_row.set_sensitive(available)
        self._service_row.set_subtitle(detail)

    def _on_live_toggled(self, row, _param) -> None:
        self._automation.set_live(row.get_active())
        self._refresh_automation_rows()

    def _on_scheduled_toggled(self, row, _param) -> None:
        self._automation.set_scheduled(row.get_active())
        self._refresh_automation_rows()

    def _on_background_toggled(self, row, _param) -> None:
        self._automation.set_background(row.get_active())
        self._refresh_automation_rows()

    def _on_time_changed(self, _spin) -> None:
        self._automation.set_time(
            f"{int(self._hour_spin.get_value()):02d}:"
            f"{int(self._minute_spin.get_value()):02d}")

    def _on_mode_toggled(self, check, mode) -> None:
        # Radio buttons emit for the one being switched off too.
        if not check.get_active():
            return
        self._config.set_sync_mode(mode)
        self._changed(mode)

    def _changed(self, what: str) -> None:
        if self._on_changed:
            self._on_changed(what)


class _StatePopover(Gtk.Popover):
    """Base for the popovers hanging off the main window's state buttons.

    Both of them show settings that another surface (the Settings dialog, or a
    `nasync` command) can change underneath them, so neither keeps state of its
    own: opening one reloads it from the config. `_loading` stops that reload
    from being mistaken for the user flipping every switch it touches.
    """

    WIDTH = 340

    def __init__(self, config, on_changed=None):
        super().__init__()
        self._config = config
        self._on_changed = on_changed
        self._loading = False
        self.connect("show", lambda *_: self.reload())

    def _content_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for setter in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(box, setter)(12)
        box.set_size_request(self.WIDTH, -1)
        return box

    @staticmethod
    def _heading(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("heading")
        return label

    @staticmethod
    def _caption(text: str, indent: int = 0) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("dim-label")
        label.add_css_class("caption")
        label.set_wrap(True)
        label.set_margin_start(indent)
        return label

    def _switch_row(self, key, handler):
        """A title, its explanation, and a switch — titled from `AUTOMATION_ROWS`."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(Gtk.Label(label=AUTOMATION_TITLE[key], xalign=0))
        text.append(self._caption(AUTOMATION_SUBTITLE[key]))
        row.append(text)

        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        switch.connect("notify::active", handler)
        row.append(switch)
        return row, switch

    def reload(self) -> None:
        raise NotImplementedError

    def _changed(self, what: str) -> None:
        if self._on_changed:
            self._on_changed(what)


class ModePopover(_StatePopover):
    """The sync mode, attached to the button that reports it.

    Mirror mode deletes on the server, which is too consequential to live two
    clicks away in a menu — the button says which mode is armed and this opens
    from it.
    """

    def __init__(self, config, on_changed=None):
        super().__init__(config, on_changed)

        box = self._content_box()
        box.append(self._heading("Sync mode"))
        box.append(self._caption("What a sync does with files that are on the "
                                 "server but not in your local folder."))

        self._checks = {}
        leader = None
        for mode, title, subtitle in SYNC_MODE_CHOICES:
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            check = Gtk.CheckButton(label=title)
            if leader is None:
                leader = check
            else:
                check.set_group(leader)
            check.connect("toggled", self._on_toggled, mode)
            group.append(check)
            # Indented under its own choice, so it reads as that option's
            # consequence rather than a note about both.
            group.append(self._caption(subtitle, indent=28))
            box.append(group)
            self._checks[mode] = check

        box.append(Gtk.Separator())
        box.append(self._caption("Restore is unaffected — pulling from the "
                                 "server never deletes local files."))
        self.set_child(box)
        self.reload()

    def reload(self) -> None:
        self._loading = True
        try:
            current = self._config.get_sync_mode()
            for mode, check in self._checks.items():
                check.set_active(mode == current)
        finally:
            self._loading = False

    def _on_toggled(self, check, mode) -> None:
        # Radio buttons emit for the one being switched off too.
        if self._loading or not check.get_active():
            return
        self._config.set_sync_mode(mode)
        self._changed(mode)


class AutomaticPopover(_StatePopover):
    """Automatic syncing, on the page it affects rather than behind a menu.

    Everything here is instant-apply, and each change reconciles the background
    service, exactly as the Settings dialog does — this is a second door onto
    the same settings, not a second copy of them.
    """

    def __init__(self, config, on_changed=None):
        super().__init__(config, on_changed)
        self._automation = AutomationSettings(
            config, on_changed=self._changed,
            on_detail=lambda detail: self._service_label.set_label(detail))

        box = self._content_box()
        box.append(self._heading("Automatic syncing"))

        live, self._live_switch = self._switch_row(
            "live", self._on_live_toggled)
        box.append(live)

        scheduled, self._scheduled_switch = self._switch_row(
            "scheduled", self._on_scheduled_toggled)
        box.append(scheduled)

        self._time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        time_label = Gtk.Label(label="Time", xalign=0)
        time_label.set_hexpand(True)
        self._time_row.append(time_label)
        self._hour_spin = make_time_spin(0, 23, 1, 2, self._on_time_changed)
        self._time_row.append(self._hour_spin)
        colon = Gtk.Label(label=":")
        colon.add_css_class("heading")
        self._time_row.append(colon)
        self._minute_spin = make_time_spin(0, 59, 5, 0, self._on_time_changed)
        self._time_row.append(self._minute_spin)
        box.append(self._time_row)

        box.append(Gtk.Separator())

        background, self._background_switch = self._switch_row(
            "background", self._on_background_toggled)
        box.append(background)
        self._service_label = self._caption("")
        box.append(self._service_label)

        notifications, self._notify_switch = self._switch_row(
            "notifications", self._on_notifications_toggled)
        box.append(notifications)

        box.append(Gtk.Separator())
        settings = Gtk.Button(label="All settings…")
        settings.add_css_class("flat")
        settings.connect("clicked", self._on_settings_clicked)
        box.append(settings)

        self.set_child(box)
        self.reload()

    def reload(self) -> None:
        self._loading = True
        try:
            automation = self._automation.get()
            self._live_switch.set_active(automation["live_sync"])
            self._scheduled_switch.set_active(automation["scheduled_sync"])
            self._background_switch.set_active(automation["background"])
            self._notify_switch.set_active(self._config.get_notifications())
            hour, minute = (int(p) for p in automation["scheduled_time"].split(":"))
            self._hour_spin.set_value(hour)
            self._minute_spin.set_value(minute)
            self._refresh_rows(automation)
        finally:
            self._loading = False

    def _refresh_rows(self, automation=None) -> None:
        """Time only matters when scheduling; say what the service is doing."""
        automation = automation or self._automation.get()
        self._time_row.set_sensitive(automation["scheduled_sync"])
        detail, available = self._automation.service_state(automation)
        self._background_switch.set_sensitive(available)
        self._service_label.set_label(detail)

    def _on_live_toggled(self, switch, _param) -> None:
        if self._loading:
            return
        self._automation.set_live(switch.get_active())
        self._refresh_rows()

    def _on_scheduled_toggled(self, switch, _param) -> None:
        if self._loading:
            return
        self._automation.set_scheduled(switch.get_active())
        self._refresh_rows()

    def _on_background_toggled(self, switch, _param) -> None:
        if self._loading:
            return
        self._automation.set_background(switch.get_active())
        self._refresh_rows()

    def _on_notifications_toggled(self, switch, _param) -> None:
        if self._loading:
            return
        self._automation.set_notifications(switch.get_active())

    def _on_time_changed(self, _spin) -> None:
        if self._loading:
            return
        self._automation.set_time(
            f"{int(self._hour_spin.get_value()):02d}:"
            f"{int(self._minute_spin.get_value()):02d}")

    def _on_settings_clicked(self, _button) -> None:
        self.popdown()
        self._changed("open-settings")


class ServerDialog(Adw.Dialog):
    """Add or edit a named server, with a live share loader.

    `on_saved(values)` receives the field dict; `values["orig_name"]` is the
    name the server had before editing, or None when adding.
    """

    def __init__(self, server: dict = None, on_saved=None):
        super().__init__()
        self._server = server or {}
        self._orig_name = self._server.get("name")
        self._on_saved = on_saved
        self._shares = []

        self.set_title("Edit server" if server else "Add server")
        self.set_content_width(460)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self._save_button = Gtk.Button(label="Save")
        self._save_button.add_css_class("suggested-action")
        self._save_button.connect("clicked", self._on_save_clicked)
        header.pack_end(self._save_button)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()

        self._name = Adw.EntryRow(title="Name")
        self._name.set_text(self._server.get("name", ""))
        group.add(self._name)

        self._ip = Adw.EntryRow(title="IP address")
        self._ip.set_text(self._server.get("ip", ""))
        group.add(self._ip)

        self._port = Adw.EntryRow(title="Port")
        self._port.set_text(str(self._server.get("port", 445)))
        group.add(self._port)

        self._user = Adw.EntryRow(title="Username")
        self._user.set_text(self._server.get("username", ""))
        group.add(self._user)

        self._password = Adw.PasswordEntryRow(title="Password")
        self._password.set_text(self._server.get("password", ""))
        group.add(self._password)
        page.add(group)

        share_group = Adw.PreferencesGroup(
            title="Share",
            description="Test the connection to list the shares this server offers.",
        )
        self._share_row = Adw.ComboRow(title="Share")
        self._share_row.set_model(Gtk.StringList.new([]))
        share_group.add(self._share_row)

        # When editing, keep the saved share selectable without re-testing.
        existing = self._server.get("share")
        if existing:
            self._set_shares([existing])

        self._test_row = Adw.ActionRow(title="Test connection")
        test_button = Gtk.Button(label="Test")
        test_button.set_valign(Gtk.Align.CENTER)
        test_button.connect("clicked", self._on_test_clicked)
        self._test_row.add_suffix(test_button)
        self._test_row.set_activatable_widget(test_button)
        share_group.add(self._test_row)
        page.add(share_group)

        # Its own group, below the share: this is a property of the connection
        # rather than of the credentials, and the trade-off needs room to be
        # stated. Someone who turns it on and then can't connect should be able
        # to read why here rather than deducing it from an error.
        security_group = Adw.PreferencesGroup(title="Security")
        self._encrypt = Adw.SwitchRow(
            title="Encrypt the connection",
            subtitle="Encrypts file contents on the network with SMB 3. The "
                     "server must support it — NASync refuses to connect "
                     "rather than fall back to sending files in the clear. "
                     "Costs speed on a NAS without hardware AES.",
        )
        self._encrypt.set_subtitle_lines(0)
        self._encrypt.set_active(bool(self._server.get("encrypt", False)))
        security_group.add(self._encrypt)
        page.add(security_group)

        self._banner = Adw.Banner()
        self._banner.set_revealed(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._banner)
        box.append(page)
        toolbar.set_content(box)
        self.set_child(toolbar)

    # ------------------------------------------------------------------ #
    def _set_shares(self, shares) -> None:
        self._shares = list(shares)
        self._share_row.set_model(Gtk.StringList.new(self._shares))
        if self._shares:
            current = self._server.get("share")
            index = self._shares.index(current) if current in self._shares else 0
            self._share_row.set_selected(index)

    def _selected_share(self):
        index = self._share_row.get_selected()
        if 0 <= index < len(self._shares):
            return self._shares[index]
        return None

    def _notify(self, message: str, error: bool = False) -> None:
        self._banner.set_title(message)
        self._banner.set_revealed(True)
        if error:
            self._banner.add_css_class("error")
        else:
            self._banner.remove_css_class("error")

    def _fields(self) -> dict:
        try:
            port = int(self._port.get_text().strip() or "445")
        except ValueError:
            port = 445
        return {
            "name": self._name.get_text().strip(),
            "ip": self._ip.get_text().strip(),
            "port": port,
            "username": self._user.get_text().strip(),
            "password": self._password.get_text(),
            "encrypt": self._encrypt.get_active(),
        }

    # ------------------------------------------------------------------ #
    def _on_test_clicked(self, _button) -> None:
        values = self._fields()
        if not values["ip"] or not values["username"]:
            self._notify("Enter an IP address and username first.", error=True)
            return
        self._notify("Testing connection…")

        def work():
            # Tested with the switch as it stands, not as it was saved: the
            # point of Test is to find out whether these settings work before
            # committing to them, and encryption is the one most likely not to.
            client = SAMBAClient(values["ip"], values["username"],
                                 values["password"], "", port=values["port"],
                                 encrypt=values["encrypt"])
            return client.list_shares()

        run_async(work, on_done=self._on_shares_loaded, on_error=self._on_test_failed)

    def _on_shares_loaded(self, shares) -> None:
        if not shares:
            self._notify("Connected, but no shares were offered.", error=True)
            return
        self._set_shares(shares)
        self._notify(f"Connected — {len(shares)} share(s) found.")

    def _on_test_failed(self, exc) -> None:
        self._notify(str(exc), error=True)

    def _on_save_clicked(self, _button) -> None:
        values = self._fields()
        if not values["name"] or not values["ip"] or not values["username"]:
            self._notify("Name, IP address and username are required.", error=True)
            return
        share = self._selected_share()
        if not share:
            self._notify("Test the connection and pick a share.", error=True)
            return
        if self._on_saved:
            self._on_saved({**values, "share": share, "orig_name": self._orig_name})
        self.close()


class RemoteBrowserDialog(Adw.Dialog):
    """Browse the server's share and pick a destination folder.

    The counterpart to `Gtk.FileDialog` on the local side of a pair. Before
    this, the destination was a text field and getting it wrong was easy —
    which matters more here than it looks: in mirror mode a destination
    pointing at the wrong folder *deletes* whatever is in it.

    `on_chosen(path)` receives the POSIX path relative to the share root, the
    same shape a sync pair stores.

    Every listing is one directory, fetched on a worker thread. A share with
    100k files in it must not be indexed to show its top level, and the UI must
    not freeze while even one directory is fetched over a slow link.
    """

    def __init__(self, server: dict, start_path: str = "", on_chosen=None):
        super().__init__()
        self._server = server
        self._on_chosen = on_chosen
        self._path = (start_path or "").strip("/")

        self.set_title("Choose a folder on the server")
        self.set_content_width(520)
        self.set_content_height(560)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self._select = Gtk.Button(label="Select")
        self._select.add_css_class("suggested-action")
        self._select.connect("clicked", self._on_select_clicked)
        header.pack_end(self._select)
        toolbar.add_top_bar(header)

        # Where we are, and the way back out. Kept in a second bar rather than
        # the header so the path has the full width — share paths get long.
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nav.set_margin_top(6)
        nav.set_margin_bottom(6)
        nav.set_margin_start(12)
        nav.set_margin_end(12)

        self._up = Gtk.Button(icon_name="go-up-symbolic")
        self._up.set_tooltip_text("Up one folder")
        self._up.connect("clicked", self._on_up_clicked)
        nav.append(self._up)

        self._location = Gtk.Label(xalign=0.0)
        self._location.set_ellipsize(3)          # PANGO_ELLIPSIZE_END
        self._location.add_css_class("heading")
        self._location.set_hexpand(True)
        nav.append(self._location)
        toolbar.add_top_bar(nav)

        self._banner = Adw.Banner()
        self._banner.set_revealed(False)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(12)
        self._list.set_margin_end(12)
        self._list.set_margin_bottom(12)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self._list)

        self._spinner = Adw.StatusPage(title="Reading the server…")
        self._spinner.set_child(Gtk.Spinner(spinning=True, width_request=32,
                                            height_request=32))

        # One at a time: the listing or the "loading" page, never both.
        self._stack = Gtk.Stack()
        self._stack.add_named(scroller, "list")
        self._stack.add_named(self._spinner, "loading")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._banner)
        box.append(self._stack)
        toolbar.set_content(box)
        self.set_child(toolbar)

        self._load()

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        """Fetch the current directory, off the main loop.

        Deliberately *not* guarded against overlapping fetches. An in-flight
        request is left to finish and its answer discarded if it no longer
        matches where we are — refusing to start a second one instead would
        strand the dialog: the click has already moved `_path`, so the reply
        for the old folder gets dropped and nothing ever replaces it.
        """
        self._banner.set_revealed(False)
        self._stack.set_visible_child_name("loading")
        self._location.set_label(f"{self._server.get('share', '')}:/{self._path}")
        self._up.set_sensitive(bool(self._path))

        wanted = self._path
        server = self._server

        def work():
            client = SAMBAClient(server["ip"], server["username"],
                                 server.get("password", ""), server["share"],
                                 port=server.get("port", 445),
                                 encrypt=server.get("encrypt", False))
            client.connect()
            try:
                return client.list_directory(wanted)
            finally:
                # release, not disconnect: overlapping listings share one
                # pooled connection, and a sync may be using it too.
                client.release()

        run_async(work,
                  on_done=lambda entries: self._on_loaded(wanted, entries),
                  on_error=lambda exc: self._on_failed(wanted, exc))

    def _on_loaded(self, path: str, entries: list) -> None:
        # A slow listing for a folder we have since navigated away from must
        # not overwrite the one now on screen.
        if path != self._path:
            return
        self._stack.set_visible_child_name("list")
        self._fill(entries)

    def _on_failed(self, path: str, exc: Exception) -> None:
        if path != self._path:
            return
        self._stack.set_visible_child_name("list")
        self._fill([])
        self._banner.set_title(str(exc))
        self._banner.add_css_class("error")
        self._banner.set_revealed(True)

    def _fill(self, entries: list) -> None:
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)

        if not entries:
            empty = Adw.ActionRow(title="This folder is empty")
            empty.set_activatable(False)
            empty.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            self._list.append(empty)
            return

        for entry in entries:
            row = Adw.ActionRow(title=entry["name"])
            if entry["is_dir"]:
                row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.set_activatable(True)
                row.connect("activated", self._on_row_activated, entry["path"])
            else:
                # Files are shown but not selectable: a destination is a
                # folder. Hiding them would remove the only cue that you are
                # in the right place.
                icon = "emblem-symbolic-link" if entry["is_symlink"] \
                    else "text-x-generic-symbolic"
                row.add_prefix(Gtk.Image(icon_name=icon))
                row.add_suffix(Gtk.Label(label=format_bytes(entry["size"]),
                                         css_classes=["dim-label"]))
                row.set_activatable(False)
            self._list.append(row)

    def _on_row_activated(self, _row, path: str) -> None:
        self._path = path
        self._load()

    def _on_up_clicked(self, _button) -> None:
        self._path = self._path.rsplit("/", 1)[0] if "/" in self._path else ""
        self._load()

    def _on_select_clicked(self, _button) -> None:
        """Choose the folder currently being shown.

        Selecting *where you are* rather than a highlighted row is what the
        GTK folder picker does, and it removes the "did I select it or just
        look at it" ambiguity from an action that can delete files.
        """
        if self._on_chosen:
            self._on_chosen("/" + self._path if self._path else "/")
        self.close()


class ServerInfoDialog(Adw.Dialog):
    """What this server is, and what was actually negotiated with it.

    Read-only and protocol-based: no vendor APIs, so it reads the same on a
    Synology, a TrueNAS box and a Raspberry Pi running Samba. That constraint
    is deliberate — administering a NAS is the vendor's web UI's job, and
    competing with it would mean a different branch per manufacturer.

    Everything shown is a *fact about the live connection*, not a setting.
    A server that quietly negotiated an older dialect than expected, or a
    connection that isn't encrypted although the switch is on, is precisely
    what this exists to make visible.
    """

    def __init__(self, server: dict):
        super().__init__()
        self._server = server

        self.set_title("Server info")
        self.set_content_width(560)
        self.set_content_height(620)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        header.pack_start(close)
        toolbar.add_top_bar(header)

        self._page = Adw.PreferencesPage()
        self._status = Adw.StatusPage(title="Asking the server…")
        self._status.set_child(Gtk.Spinner(spinning=True, width_request=32,
                                           height_request=32))
        self._stack = Gtk.Stack()
        self._stack.add_named(self._page, "info")
        self._stack.add_named(self._status, "loading")
        self._stack.set_visible_child_name("loading")

        toolbar.set_content(self._stack)
        self.set_child(toolbar)
        self._load()

    def _load(self) -> None:
        server = self._server

        def work():
            client = SAMBAClient(server["ip"], server["username"],
                                 server.get("password", ""), server["share"],
                                 port=server.get("port", 445),
                                 encrypt=server.get("encrypt", False))
            client.connect()
            try:
                return client.server_info()
            finally:
                client.release()

        run_async(work, on_done=self._on_loaded, on_error=self._on_failed)

    @staticmethod
    def _row(title: str, subtitle: str, icon: str = None) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_subtitle_lines(0)
        row.set_activatable(False)
        if icon:
            row.add_prefix(Gtk.Image(icon_name=icon))
        return row

    def _on_loaded(self, info: dict) -> None:
        conn = info["connection"]

        connection = Adw.PreferencesGroup(title="Connection")
        connection.add(self._row(
            "Protocol", conn["dialect_name"], "network-transmit-receive-symbolic"))

        if conn["encrypted"]:
            encrypted = ("Encrypted", "File contents are encrypted on the "
                         "network with SMB 3.", "channel-secure-symbolic")
        elif conn["supports_encryption"]:
            encrypted = ("Not encrypted",
                         "This server supports encryption — turn it on in this "
                         "server's settings.", "channel-insecure-symbolic")
        else:
            encrypted = ("Not encrypted",
                         "This server is too old to encrypt; it needs SMB 3.",
                         "channel-insecure-symbolic")
        connection.add(self._row(*encrypted))

        connection.add(self._row(
            "Signed" if conn["signed"] else "Not signed",
            "Tampering with traffic in transit would be detected."
            if conn["signed"] else
            ("Encryption already covers this." if conn["encrypted"]
             else "This server doesn't require signed traffic."),
            "security-high-symbolic" if conn["signed"] or conn["encrypted"]
            else "security-low-symbolic"))
        self._page.add(connection)

        volume = info.get("volume")
        storage = Adw.PreferencesGroup(title="Storage")
        summary, quota = format_volume(volume)
        if summary:
            detail = summary + (f"\n{quota}" if quota else "")
            storage.add(self._row("Space", detail, "drive-harddisk-symbolic"))
        else:
            storage.add(self._row("Space", "This server doesn't report it.",
                                  "drive-harddisk-symbolic"))
        self._page.add(storage)

        shares = info.get("shares")
        group = Adw.PreferencesGroup(
            title="Shares",
            description="What this server offers. NASync syncs to the one "
                        "marked below.")
        if shares is None:
            group.add(self._row("Couldn't be listed",
                                info.get("shares_error", "Unknown reason."),
                                "dialog-warning-symbolic"))
        else:
            for share in shares:
                subtitle = share["type"]
                if share["is_special"]:
                    subtitle += " · administrative"
                if share["comment"]:
                    subtitle += f" · {share['comment']}"
                row = self._row(share["name"], subtitle, "folder-remote-symbolic")
                if share["name"] == info["share"]:
                    row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
                group.add(row)
        self._page.add(group)

        self._stack.set_visible_child_name("info")

    def _on_failed(self, exc: Exception) -> None:
        self._status.set_title("Couldn't reach the server")
        self._status.set_description(str(exc))
        self._status.set_child(Gtk.Image(icon_name="dialog-error-symbolic",
                                         pixel_size=48))


class StorageDialog(Adw.Dialog):
    """What is using space on the server, and what is stored twice.

    Both views run off the same walk the sync engine uses — one round trip per
    directory — so this costs a scan, not a scan per question.

    Everything here is **read-only**. NASync will not delete a duplicate for
    you: a sync tool that also removes files on its own initiative is one bad
    heuristic away from being the reason someone lost a photo library.
    """

    #: Files smaller than this are ignored when hunting duplicates. Every empty
    #: file matches every other one, and a share has thousands of small
    #: identical files (thumbnails, __init__.py) that nobody wants listed.
    DUPLICATE_MIN_SIZE = 1024 * 1024

    def __init__(self, server: dict):
        super().__init__()
        self._server = server
        self._path = ""

        self.set_title("Storage")
        self.set_content_width(620)
        self.set_content_height(640)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        header.pack_start(close)

        self._up = Gtk.Button(icon_name="go-up-symbolic")
        self._up.set_tooltip_text("Up one folder")
        self._up.connect("clicked", self._on_up_clicked)
        header.pack_start(self._up)

        self._duplicates_button = Gtk.Button(label="Find duplicates")
        self._duplicates_button.connect("clicked", self._on_duplicates_clicked)
        header.pack_end(self._duplicates_button)
        toolbar.add_top_bar(header)

        self._summary = Adw.ActionRow()
        self._summary.set_activatable(False)
        self._summary.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic"))
        summary_group = Adw.PreferencesGroup()
        summary_group.add(self._summary)
        summary_group.set_margin_start(12)
        summary_group.set_margin_end(12)
        summary_group.set_margin_top(12)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(12)
        self._list.set_margin_end(12)
        self._list.set_margin_top(12)
        self._list.set_margin_bottom(12)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self._list)

        self._status = Adw.StatusPage(title="Scanning the server…",
                                      description="Reading one folder at a time.")
        self._status.set_child(Gtk.Spinner(spinning=True, width_request=32,
                                           height_request=32))

        self._stack = Gtk.Stack()
        self._stack.add_named(scroller, "list")
        self._stack.add_named(self._status, "scanning")

        self._banner = Adw.Banner()
        self._banner.set_revealed(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._banner)
        box.append(summary_group)
        box.append(self._stack)
        toolbar.set_content(box)
        self.set_child(toolbar)

        self._load()

    def _client(self):
        return SAMBAClient(self._server["ip"], self._server["username"],
                           self._server.get("password", ""),
                           self._server["share"],
                           port=self._server.get("port", 445),
                           encrypt=self._server.get("encrypt", False))

    # ------------------------------------------------------------------ #
    # Space
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        """Scan the current folder. Overlapping scans are allowed on purpose —
        see the note in `RemoteBrowserDialog._load`; a guard here would strand
        the dialog the same way."""
        self._banner.set_revealed(False)
        self._stack.set_visible_child_name("scanning")
        self._status.set_title("Scanning the server…")
        self._duplicates_button.set_sensitive(True)
        self._up.set_sensitive(bool(self._path))

        wanted = self._path

        def work():
            client = self._client()
            client.connect()
            try:
                volume = None
                try:
                    volume = client.volume_info()
                except Exception:
                    pass       # not every server answers; the report still works
                return volume, client.usage_tree(wanted)
            finally:
                client.release()

        run_async(work,
                  on_done=lambda result: self._on_usage(wanted, *result),
                  on_error=lambda exc: self._on_failed(wanted, exc))

    def _on_usage(self, path: str, volume, report: dict) -> None:
        if path != self._path:
            return
        self._stack.set_visible_child_name("list")

        location = f"{self._server.get('share', '')}:/{report['path']}"
        detail = f"{format_bytes(report['total'])} in {report['files']:,} file(s)"
        summary, quota = format_volume(volume)
        if summary:
            detail += f" · disk {summary}"
            if quota:
                # Was missing here, so this was the one surface that promised
                # space a quota won't let you write.
                detail += f"\n{quota}"
        self._summary.set_title(location)
        self._summary.set_subtitle(detail)
        self._summary.set_subtitle_lines(0)

        self._clear()
        if not report["children"]:
            self._empty("Nothing in this folder")
            return

        largest = report["children"][0]["total"] or 1
        for child in report["children"]:
            row = Adw.ActionRow(
                title=child["name"] + ("/" if child["is_dir"] else ""),
                subtitle=f"{format_bytes(child['total'])} · "
                         f"{child['files']:,} file(s)")
            row.add_prefix(Gtk.Image(
                icon_name="folder-symbolic" if child["is_dir"]
                else "text-x-generic-symbolic"))

            # A bar makes the shape of the answer readable at a glance; the
            # numbers alone need arithmetic to compare.
            meter = Gtk.ProgressBar()
            meter.set_fraction(child["total"] / largest)
            meter.set_valign(Gtk.Align.CENTER)
            meter.set_size_request(120, -1)
            row.add_suffix(meter)

            if child["is_dir"]:
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.set_activatable(True)
                row.connect("activated", self._on_row_activated, child["path"])
            else:
                row.set_activatable(False)
            self._list.append(row)

    # ------------------------------------------------------------------ #
    # Duplicates
    # ------------------------------------------------------------------ #
    def _on_duplicates_clicked(self, _button) -> None:
        self._banner.set_revealed(False)
        self._stack.set_visible_child_name("scanning")
        self._status.set_title("Looking for duplicates…")
        self._duplicates_button.set_sensitive(False)

        wanted = self._path

        def work():
            client = self._client()
            client.connect()
            try:
                # Size-matching only. Confirming by content has to read every
                # candidate over the network — gigabytes on a media share —
                # so it is not something to do because a button was clicked.
                return client.find_duplicates(
                    wanted, min_size=self.DUPLICATE_MIN_SIZE)
            finally:
                client.release()

        run_async(work,
                  on_done=lambda groups: self._on_duplicates(wanted, groups),
                  on_error=lambda exc: self._on_failed(wanted, exc))

    def _on_duplicates(self, path: str, groups: list) -> None:
        if path != self._path:
            return
        self._stack.set_visible_child_name("list")
        self._duplicates_button.set_sensitive(True)
        self._clear()

        location = f"{self._server.get('share', '')}:/{path}"
        if not groups:
            self._summary.set_title(f"No duplicates found in {location}")
            self._summary.set_subtitle(
                f"Files under {format_bytes(self.DUPLICATE_MIN_SIZE)} "
                f"were not considered.")
            self._empty("Nothing appears to be stored twice")
            return

        recoverable = sum(group["wasted"] for group in groups)
        self._summary.set_title(
            f"{len(groups)} possible duplicate group(s) in {location}")
        self._summary.set_subtitle(
            f"About {format_bytes(recoverable)} could be recovered. Matched by "
            f"size — check before deleting anything, and delete it yourself: "
            f"NASync never removes files here.")
        self._summary.set_subtitle_lines(0)

        for group in groups:
            row = Adw.ExpanderRow(
                title=f"{format_bytes(group['wasted'])} wasted",
                subtitle=f"{len(group['paths'])} × {format_bytes(group['size'])}")
            row.add_prefix(Gtk.Image(icon_name="edit-copy-symbolic"))
            for rel in group["paths"]:
                copy = Adw.ActionRow(title=rel)
                copy.set_activatable(False)
                copy.add_css_class("dim-label")
                row.add_row(copy)
            self._list.append(row)

    # ------------------------------------------------------------------ #
    def _clear(self) -> None:
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)

    def _empty(self, message: str) -> None:
        row = Adw.ActionRow(title=message)
        row.set_activatable(False)
        row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        self._list.append(row)

    def _on_failed(self, path: str, exc: Exception) -> None:
        if path != self._path:
            return
        self._stack.set_visible_child_name("list")
        self._duplicates_button.set_sensitive(True)
        self._clear()
        self._banner.set_title(str(exc))
        self._banner.add_css_class("error")
        self._banner.set_revealed(True)

    def _on_row_activated(self, _row, path: str) -> None:
        self._path = path
        self._load()

    def _on_up_clicked(self, _button) -> None:
        self._path = self._path.rsplit("/", 1)[0] if "/" in self._path else ""
        self._load()


class PairDialog(Adw.Dialog):
    """Add or edit one local→server sync pair.

    `server_name` is the server the pair belongs to. Pairs are per-server, so
    the dialog names it rather than leaving the user to infer which tab they
    were on.

    `server` is that server's full settings. Passing it enables the Browse
    button beside the destination; without it the field still accepts a typed
    path, which is how this worked before 0.13.0.
    """

    def __init__(self, pair: dict = None, on_saved=None, server_name: str = None,
                 server: dict = None):
        super().__init__()
        self._pair = pair or {}
        self._server = server
        self._orig_name = self._pair.get("name")
        self._on_saved = on_saved
        self._server_name = server_name

        self.set_title("Edit sync pair" if pair else "Add sync pair")
        self.set_content_width(460)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save_clicked)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=(f"This pair belongs to '{server_name}'."
                         if server_name else None),
        )

        self._local = Adw.EntryRow(title="Local folder")
        self._local.set_text(self._pair.get("local", ""))
        browse = Gtk.Button(icon_name="folder-open-symbolic")
        browse.set_valign(Gtk.Align.CENTER)
        browse.set_tooltip_text("Choose a folder")
        browse.add_css_class("flat")
        browse.connect("clicked", self._on_browse_clicked)
        self._local.add_suffix(browse)
        group.add(self._local)

        self._dest = Adw.EntryRow(title="Destination under the share")
        self._dest.set_text(self._pair.get("server", ""))
        if self._server and self._server.get("ip"):
            browse_remote = Gtk.Button(icon_name="folder-remote-symbolic")
            browse_remote.set_valign(Gtk.Align.CENTER)
            browse_remote.set_tooltip_text("Browse the server")
            browse_remote.add_css_class("flat")
            browse_remote.connect("clicked", self._on_browse_remote_clicked)
            self._dest.add_suffix(browse_remote)
        group.add(self._dest)

        self._name = Adw.EntryRow(title="Name (optional)")
        self._name.set_text(self._pair.get("name", ""))
        group.add(self._name)
        page.add(group)

        self._banner = Adw.Banner()
        self._banner.set_revealed(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._banner)
        box.append(page)
        toolbar.set_content(box)
        self.set_child(toolbar)

    # ------------------------------------------------------------------ #
    def _on_browse_clicked(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Choose a folder to sync")
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return  # the user dismissed the picker
        if folder is not None:
            self._local.set_text(folder.get_path() or "")

    def _on_browse_remote_clicked(self, _button) -> None:
        # Opens where the field currently points, so editing an existing pair
        # starts from that pair's folder rather than from the share root.
        RemoteBrowserDialog(
            self._server,
            start_path=self._dest.get_text().strip(),
            on_chosen=self._dest.set_text,
        ).present(self)

    def _on_save_clicked(self, _button) -> None:
        local = self._local.get_text().strip()
        dest = self._dest.get_text().strip()
        if not local or not dest:
            self._banner.set_title("Local folder and destination are required.")
            self._banner.add_css_class("error")
            self._banner.set_revealed(True)
            return
        if self._on_saved:
            self._on_saved({
                "local": local,
                "dest": dest,
                "name": self._name.get_text().strip() or None,
                "orig_name": self._orig_name,
            })
        self.close()
