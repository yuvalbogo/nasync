"""Textual terminal UI for nasync.

A thin presentation layer over the same building blocks the CLI uses:
`Config` (persistence), `SAMBAClient` (connection test / share discovery) and
`Syncer` (push/pull). Blocking work runs in worker threads and streams progress
into the log pane through the injectable reporter/progress callbacks.

Servers are compact, saved, named entries — pick one to make it active; the
heavy connection fields live in the Add/Edit modal, keeping the main screen
focused on the sync paths.
"""

import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, ListItem, ListView,
    ProgressBar, RichLog, Rule, Select,
)

from nasync.config import Config
from nasync.samba import SAMBAClient
from nasync.sync import Syncer


class AddPairScreen(ModalScreen):
    """Modal to add or edit one local→server sync pair."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, pair: dict = None):
        super().__init__()
        self._pair = pair or {}
        self._orig_name = self._pair.get("name")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event) -> None:
        self._confirm()  # Enter in any field saves

    def compose(self) -> ComposeResult:
        editing = bool(self._pair)
        box = Vertical(id="dialog")
        box.border_title = "Edit sync pair" if editing else "Add sync pair"
        with box:
            yield Label("Local folder")
            yield Input(value=self._pair.get("local", ""),
                        placeholder="~/Documents", id="ap-local")
            yield Label("Destination under the share")
            yield Input(value=self._pair.get("server", ""),
                        placeholder="/Docs", id="ap-dest")
            yield Label("Name" if editing else "Name (optional)")
            yield Input(value=self._pair.get("name", ""),
                        placeholder="auto from folder name", id="ap-name")
            yield Label("", id="ap-status")
            with Horizontal(classes="buttons"):
                yield Button("Save" if editing else "Add", variant="success", id="ap-ok")
                yield Button("Cancel", id="ap-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ap-cancel":
            self.dismiss(None)
        elif event.button.id == "ap-ok":
            self._confirm()

    def _confirm(self) -> None:
        local = self.query_one("#ap-local", Input).value.strip()
        dest = self.query_one("#ap-dest", Input).value.strip()
        name = self.query_one("#ap-name", Input).value.strip() or None
        if not local or not dest:
            self.query_one("#ap-status", Label).update("[red]Local folder and destination are required.[/red]")
            return
        self.dismiss({"local": local, "dest": dest, "name": name,
                      "orig_name": self._orig_name})


class AddServerScreen(ModalScreen):
    """Modal to add or edit a named server, with a live share loader."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, server: dict = None):
        super().__init__()
        self._server = server or {}
        self._orig_name = self._server.get("name")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event) -> None:
        self._save()  # Enter in any field saves

    def compose(self) -> ComposeResult:
        box = Vertical(id="dialog")
        box.border_title = "Edit server" if self._server else "Add server"
        with box:
            yield Label("Name")
            yield Input(value=self._server.get("name", ""), placeholder="home-nas", id="sv-name")
            with Horizontal(classes="row"):
                yield Input(value=self._server.get("ip", ""),
                            placeholder="IP (192.168.1.100)", id="sv-ip")
                yield Input(value=str(self._server.get("port", "") or ""),
                            placeholder="Port (445)", id="sv-port")
            yield Input(value=self._server.get("username", ""), placeholder="Username", id="sv-user")
            yield Input(value=self._server.get("password", ""),
                        placeholder="Password (blank for guest)", password=True, id="sv-pass")
            with Horizontal(classes="buttons"):
                yield Button("Test & load shares", variant="primary", id="sv-test")
            yield Select([], prompt="Share — press Test to load", id="sv-share")
            yield Label("", id="sv-status")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="success", id="sv-ok")
                yield Button("Cancel", id="sv-cancel")

    def on_mount(self) -> None:
        # When editing, keep the existing share selectable without re-testing.
        share = self._server.get("share")
        if share:
            sel = self.query_one("#sv-share", Select)
            sel.set_options([(share, share)])
            sel.value = share

    def _fields(self) -> dict:
        port_raw = self.query_one("#sv-port", Input).value.strip() or "445"
        try:
            port = int(port_raw)
        except ValueError:
            port = 445
        return {
            "name": self.query_one("#sv-name", Input).value.strip(),
            "ip": self.query_one("#sv-ip", Input).value.strip(),
            "port": port,
            "username": self.query_one("#sv-user", Input).value.strip(),
            "password": self.query_one("#sv-pass", Input).value,
        }

    def _status(self, message: str) -> None:
        self.query_one("#sv-status", Label).update(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sv-cancel":
            self.dismiss(None)
        elif event.button.id == "sv-test":
            self._test()
        elif event.button.id == "sv-ok":
            self._save()

    def _test(self) -> None:
        f = self._fields()
        if not f["ip"] or not f["username"]:
            self._status("[red]Enter an IP and username first.[/red]")
            return
        self._status("Testing…")
        self._worker_test(f)

    @work(thread=True, exclusive=True)
    def _worker_test(self, f: dict) -> None:
        client = SAMBAClient(f["ip"], f["username"], f["password"], "", port=f["port"])
        try:
            shares = client.list_shares()
        except Exception as e:
            self.app.call_from_thread(self._status, f"[red]✗ {e}[/red]")
            return
        self.app.call_from_thread(self._loaded, shares)

    def _loaded(self, shares) -> None:
        self._status(f"[green]✓ connected[/green] — {len(shares)} share(s)")
        sel = self.query_one("#sv-share", Select)
        if shares:
            sel.set_options([(s, s) for s in shares])
            sel.value = shares[0]

    def _save(self) -> None:
        f = self._fields()
        share = self.query_one("#sv-share", Select).value
        share = share if isinstance(share, str) and share else None
        if not f["name"] or not f["ip"] or not f["username"]:
            self._status("[red]Name, IP and username are required.[/red]")
            return
        if not share:
            self._status("[red]Test the connection and pick a share.[/red]")
            return
        self.dismiss({**f, "share": share, "orig_name": self._orig_name})


class NasyncApp(App):
    TITLE = "NASync"
    SUB_TITLE = "Sync your NAS with class"

    CSS = """
    Screen { layout: horizontal; background: $background; }

    #left  { width: 55%; padding: 1 1 1 2; }
    #right { width: 45%; padding: 1 2 1 1; }

    .section {
        border: round $primary 60%;
        border-title-color: $accent;
        border-title-style: bold;
        background: $panel;
        padding: 1 2;
        margin-bottom: 1;
        height: auto;
    }
    #pairs-section { height: 1fr; }
    #log-section { height: 1fr; }

    .row { height: auto; }
    Input { width: 1fr; margin-bottom: 1; }

    #servers { height: auto; max-height: 6; background: $surface; border: none; }
    #status { margin-top: 1; }
    #pairs { height: 1fr; min-height: 8; }

    .buttons { height: auto; align: left middle; margin-top: 1; }
    Button { margin: 0 1 0 0; border: none; height: 3; min-width: 8; }

    /* progress lives at the top of the Activity (log) card */
    #progress-box {
        height: auto;
        background: $boost;
        border: round $primary 40%;
        padding: 1 2;
        margin-bottom: 1;
    }
    #current { height: 1; text-style: bold; }
    #bar { margin: 1 0 0 0; width: 1fr; }
    #bar Bar { width: 1fr; }
    #bar Bar > .bar--bar { color: $accent; }
    #bar Bar > .bar--complete { color: $success; }
    #bar PercentageStatus { color: $accent; text-style: bold; }
    #counter { color: $text-muted; height: 1; }
    #log-divider { color: $primary 30%; margin: 0; }
    #log { height: 1fr; background: $surface; }

    #dialog {
        background: $surface;
        border: round $primary;
        border-title-color: $accent;
        border-title-style: bold;
        padding: 1 2;
        width: 64;
        height: auto;
    }
    """

    BINDINGS = [
        ("s", "sync", "Sync"),
        ("d", "dryrun", "Dry-run"),
        ("t", "test", "Test"),
        ("q", "quit", "Quit"),
    ]

    def _section(self, title: str, *, id: str = None) -> Vertical:
        box = Vertical(classes="section", id=id) if id else Vertical(classes="section")
        box.border_title = title
        return box

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="left"):
            with self._section("Servers"):
                yield ListView(id="servers")
                with Horizontal(classes="buttons"):
                    yield Button("Add", variant="success", id="srv-add")
                    yield Button("Edit", id="srv-edit")
                    yield Button("Remove", variant="error", id="srv-remove")
                    yield Button("Test", variant="primary", id="srv-test")
                yield Label("", id="status")

            with self._section("Sync pairs", id="pairs-section"):
                yield DataTable(id="pairs")
                with Horizontal(classes="buttons"):
                    yield Button("Add pair", variant="success", id="btn-add")
                    yield Button("Edit", id="btn-edit")
                    yield Button("Remove", variant="error", id="btn-remove")

        with Vertical(id="right"):
            with self._section("Actions"):
                with Horizontal(classes="buttons"):
                    yield Button("Sync ▶", variant="primary", id="btn-sync")
                    yield Button("Dry-run", id="btn-dryrun")
                    yield Button("Restore ◀", variant="warning", id="btn-restore")
                with Horizontal(classes="buttons"):
                    yield Button("Auto-sync: Off", id="btn-auto")
            with self._section("Activity", id="log-section"):
                with Vertical(id="progress-box"):
                    yield Label("[dim]Idle[/dim]", id="current")
                    yield ProgressBar(id="bar", total=100, show_eta=True)
                    yield Label("", id="counter")
                yield Rule(id="log-divider")
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:
        self.config = Config()
        table = self.query_one("#pairs", DataTable)
        table.add_columns("Name", "Local", "Server")
        table.cursor_type = "row"
        self._refresh_servers()
        self._refresh_pairs()
        self._log("[dim]Ready. Pick a server (or Add one), then Sync.[/dim]")

    def _refresh_servers(self) -> None:
        lv = self.query_one("#servers", ListView)
        lv.clear()
        active = self.config.get_active_server_name()
        active_index = 0
        for i, s in enumerate(self.config.list_servers()):
            mark = "●" if s["name"] == active else "○"
            text = (f"{mark} [b]{s['name']}[/b]  "
                    f"[dim]{s['ip']}:{s.get('port', 445)}/{s['share']}[/dim]")
            lv.append(ListItem(Label(text)))
            if s["name"] == active:
                active_index = i
        if self.config.list_servers():
            lv.index = active_index

    def _refresh_pairs(self) -> None:
        table = self.query_one("#pairs", DataTable)
        table.clear()
        for p in self.config.get_pairs():
            table.add_row(p["name"], p["local"], p["server"], key=p["name"])

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _thread_log(self, message: str) -> None:
        self.call_from_thread(self._log, message)

    def _set_status(self, text: str, ok: bool = None) -> None:
        colour = "" if ok is None else ("green" if ok else "red")
        self.query_one("#status", Label).update(
            f"[{colour}]{text}[/{colour}]" if colour else text)

    def _selected_server(self):
        idx = self.query_one("#servers", ListView).index
        servers = self.config.list_servers()
        if idx is None or idx >= len(servers):
            return None
        return servers[idx]

    # ------------------------------------------------------------------ #
    # Progress (native ProgressBar + object counter + elapsed)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"

    def _reset_progress(self) -> None:
        self._t0 = time.monotonic()
        self.query_one("#bar", ProgressBar).update(total=100, progress=0)
        self.query_one("#current", Label).update("[b]Starting…[/b]")
        self.query_one("#counter", Label).update("")

    def _on_progress(self, done: int, total, name: str, verb: str) -> None:
        self.query_one("#bar", ProgressBar).update(
            total=total if total else None, progress=done)
        icon = "↑" if verb == "Uploading" else "↓"
        self.query_one("#current", Label).update(f"[$accent]{icon}[/] {verb} [b]{name}[/b]")
        elapsed = self._fmt_elapsed(time.monotonic() - getattr(self, "_t0", time.monotonic()))
        objs = f"{done} / {total} files" if total else f"{done} files"
        self.query_one("#counter", Label).update(f"{objs}  ·  {elapsed} elapsed")

    def _finish_progress(self, result: dict) -> None:
        if result.get("failed"):
            self.query_one("#current", Label).update(
                f"[yellow]Completed with {result['failed']} error(s)[/yellow]")
        else:
            self.query_one("#current", Label).update("[green]✓ Complete[/green]")

    def _thread_progress(self, done, total, name, verb) -> None:
        self.call_from_thread(self._on_progress, done, total, name, verb)

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "srv-add": self._open_add_server,
            "srv-edit": self._open_edit_server,
            "srv-remove": self._remove_selected_server,
            "srv-test": self.action_test,
            "btn-add": self._open_add_pair,
            "btn-edit": self._open_edit_pair,
            "btn-remove": self._remove_selected_pair,
            "btn-sync": lambda: self._start_transfer("push", dry_run=False),
            "btn-dryrun": lambda: self._start_transfer("push", dry_run=True),
            "btn-restore": lambda: self._start_transfer("pull", dry_run=False),
            "btn-auto": self._toggle_auto,
        }
        handler = handlers.get(event.button.id)
        if handler:
            handler()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        servers = self.config.list_servers()
        idx = event.list_view.index
        if idx is None or idx >= len(servers):
            return
        name = servers[idx]["name"]
        if self.config.set_active_server(name):
            self._refresh_servers()
            self._set_status(f"Active: {name}", ok=True)
            self._log(f"[green]● Active server: {name}[/green]")

    # ------------------------------------------------------------------ #
    # Servers: add / edit / remove / test
    # ------------------------------------------------------------------ #
    def _open_add_server(self) -> None:
        self.push_screen(AddServerScreen(), self._on_server_saved)

    def _open_edit_server(self) -> None:
        s = self._selected_server()
        if not s:
            self._set_status("Select a server to edit.", ok=False)
            return
        self.push_screen(AddServerScreen(self.config.get_server(s["name"])),
                         self._on_server_saved)

    def _on_server_saved(self, result) -> None:
        if not result:
            return
        name, orig = result["name"], result.get("orig_name")
        if orig and orig != name:
            self.config.remove_server(orig)
        self.config.add_or_update_server(name, result["ip"], result["username"],
                                         result["password"], result["share"],
                                         result["port"])
        self.config.set_active_server(name)
        self._refresh_servers()
        self._set_status(f"✓ Saved '{name}'", ok=True)
        self._log(f"[green]✓ Saved server '{name}'[/green] "
                  f"({result['ip']}:{result['port']}/{result['share']})")

    def _remove_selected_server(self) -> None:
        s = self._selected_server()
        if not s:
            self._set_status("Select a server to remove.", ok=False)
            return
        self.config.remove_server(s["name"])
        self._refresh_servers()
        self._log(f"[green]✓ Removed server '{s['name']}'[/green]")

    def action_test(self) -> None:
        sc = self.config.get_server_config()
        if not sc["ip"]:
            self._set_status("No server selected.", ok=False)
            return
        self._set_status("Testing…")
        self._log(f"[dim]Testing {sc['ip']}:{sc['port']}/{sc['share']}…[/dim]")
        self._worker_test(sc)

    @work(thread=True, exclusive=True)
    def _worker_test(self, sc: dict) -> None:
        client = SAMBAClient(sc["ip"], sc["username"], sc["password"],
                             sc["share"], port=sc["port"], reporter=self._thread_log)
        try:
            client.connect()
        except Exception as e:
            self.call_from_thread(self._set_status, f"✗ {e}", False)
            self.call_from_thread(self._log, f"[red]✗ {e}[/red]")
            return
        self.call_from_thread(self._set_status, "✓ Connection OK", True)
        self.call_from_thread(self._log,
                              f"[green]✓ {sc['ip']}:{sc['port']}/{sc['share']} reachable[/green]")

    # ------------------------------------------------------------------ #
    # Pairs
    # ------------------------------------------------------------------ #
    def _selected_pair(self):
        table = self.query_one("#pairs", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        name = table.get_row_at(table.cursor_row)[0]
        return self.config.get_pair(name)

    def _open_add_pair(self) -> None:
        self.push_screen(AddPairScreen(), self._on_pair_saved)

    def _open_edit_pair(self) -> None:
        pair = self._selected_pair()
        if not pair:
            self._log("[yellow]Select a pair to edit.[/yellow]")
            return
        self.push_screen(AddPairScreen(pair), self._on_pair_saved)

    def _on_pair_saved(self, result) -> None:
        if not result:
            return
        orig = result.get("orig_name")
        try:
            if orig:
                name = self.config.update_pair(orig, result["local"],
                                               result["dest"], result["name"] or orig)
                action = "Updated"
            else:
                name = self.config.add_pair(result["local"], result["dest"], result["name"])
                action = "Added"
        except ValueError as e:
            self._log(f"[red]✗ {e}[/red]")
            return
        self._refresh_pairs()
        self._log(f"[green]✓ {action} pair '{name}'[/green]: {result['local']} → {result['dest']}")

    def _remove_selected_pair(self) -> None:
        pair = self._selected_pair()
        if not pair:
            self._log("[yellow]No pair selected to remove.[/yellow]")
            return
        self.config.remove_pair(pair["name"])
        self._refresh_pairs()
        self._log(f"[green]✓ Removed pair '{pair['name']}'[/green]")

    # ------------------------------------------------------------------ #
    # Auto-sync (live, session-only file watcher)
    # ------------------------------------------------------------------ #
    def _toggle_auto(self) -> None:
        if getattr(self, "_observer", None):
            self._stop_auto()
        else:
            self._start_auto()

    def _start_auto(self) -> None:
        import os
        from watchdog.observers import Observer
        from nasync.automation import _DebouncedHandler

        if not self.config.get_active_server_name():
            self._log("[red]✗ Pick a server before enabling auto-sync.[/red]")
            return
        pairs = [p for p in self.config.get_pairs() if os.path.isdir(p["local"])]
        if not pairs:
            self._log("[yellow]No local folders to watch.[/yellow]")
            return

        self._observer = Observer()
        self._auto_handlers = []
        trigger = lambda: self.call_from_thread(self._auto_fire)  # noqa: E731
        for p in pairs:
            handler = _DebouncedHandler(trigger)
            self._auto_handlers.append(handler)
            self._observer.schedule(handler, p["local"], recursive=True)
        self._observer.start()

        btn = self.query_one("#btn-auto", Button)
        btn.label = "Auto-sync: On"
        btn.variant = "success"
        self._log(f"[green]👁️  Auto-sync ON[/green] — watching {len(pairs)} folder(s)")

    def _auto_fire(self) -> None:
        self._log("[dim]Auto-sync: change detected[/dim]")
        self._start_transfer("push", dry_run=False)

    def _stop_auto(self) -> None:
        for h in getattr(self, "_auto_handlers", []):
            h.cancel()
        if getattr(self, "_observer", None):
            self._observer.stop()
            self._observer.join()
        self._observer = None
        self._auto_handlers = []
        btn = self.query_one("#btn-auto", Button)
        btn.label = "Auto-sync: Off"
        btn.variant = "default"
        self._log("[yellow]⏸️  Auto-sync OFF[/yellow]")

    def on_unmount(self) -> None:
        for h in getattr(self, "_auto_handlers", []):
            h.cancel()
        observer = getattr(self, "_observer", None)
        if observer:
            observer.stop()
            observer.join()

    # ------------------------------------------------------------------ #
    # Transfers
    # ------------------------------------------------------------------ #
    def action_sync(self) -> None:
        self._start_transfer("push", dry_run=False)

    def action_dryrun(self) -> None:
        self._start_transfer("push", dry_run=True)

    def _start_transfer(self, direction: str, dry_run: bool) -> None:
        if not self.config.get_active_server_name():
            self._log("[red]✗ No server selected — Add or pick one first.[/red]")
            return
        self._reset_progress()
        verb = {"push": "Sync", "pull": "Restore"}[direction]
        self._log(f"\n[b]▶ {verb}{' (dry-run)' if dry_run else ''}…[/b]")
        self._worker_transfer(direction, dry_run)

    @work(thread=True, exclusive=True)
    def _worker_transfer(self, direction: str, dry_run: bool) -> None:
        syncer = Syncer(reporter=self._thread_log, progress=self._thread_progress)
        try:
            if direction == "push":
                result = syncer.sync_pair(dry_run=dry_run)
            else:
                result = syncer.restore_pair(dry_run=dry_run)
        except Exception as e:
            self.call_from_thread(self._log, f"[red]✗ {e}[/red]")
            return
        summary = f"[green]Done[/green] — {result['ok']} ok, {result['failed']} failed"
        self.call_from_thread(self._log, summary)
        self.call_from_thread(self._finish_progress, result)


def run() -> None:
    NasyncApp().run()
