"""Textual terminal UI for nasync.

This is a thin presentation layer over the same building blocks the CLI uses:
`Config` (persistence), `SAMBAClient` (connection test / share discovery) and
`Syncer` (push/pull). Blocking work runs in worker threads and streams progress
into the log pane through the injectable reporter added to those classes.
"""

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog,
    Select, Static,
)

from samba_sync.config import Config
from samba_sync.samba import SAMBAClient
from samba_sync.sync import Syncer


class AddPairScreen(ModalScreen):
    """Small modal to enter one local→server sync pair."""

    def __init__(self, default_share: str = ""):
        super().__init__()
        self._share = default_share

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add sync pair", classes="title")
            yield Label("Local folder")
            yield Input(placeholder="~/Documents", id="ap-local")
            yield Label("Destination under the share")
            yield Input(placeholder="/Docs", id="ap-dest")
            yield Label("Name (optional)")
            yield Input(placeholder="auto from folder name", id="ap-name")
            with Horizontal(classes="buttons"):
                yield Button("Add", variant="success", id="ap-ok")
                yield Button("Cancel", id="ap-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ap-cancel":
            self.dismiss(None)
            return
        local = self.query_one("#ap-local", Input).value.strip()
        dest = self.query_one("#ap-dest", Input).value.strip()
        name = self.query_one("#ap-name", Input).value.strip() or None
        if not local or not dest:
            self.query_one(".title", Label).update("Add sync pair — local and destination are required")
            return
        self.dismiss({"local": local, "dest": dest, "name": name})


class NasyncApp(App):
    TITLE = "nasync"
    SUB_TITLE = "SAMBA sync for your home NAS"

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
    #log-section { height: 1fr; }

    .row { height: auto; }
    Input { width: 1fr; margin-bottom: 1; }
    #status { margin: 1 0; }
    #pairs { height: 7; margin-bottom: 1; }

    .buttons { height: auto; align: left middle; margin-top: 1; }
    Button { margin: 0 1 0 0; border: none; height: 3; min-width: 12; }

    /* progress */
    #current { color: $text-muted; height: 1; margin-top: 1; }
    #bar { margin-top: 1; width: 1fr; }

    #log { height: 1fr; background: $surface; }

    #dialog {
        background: $surface;
        border: round $primary;
        border-title-color: $accent;
        border-title-style: bold;
        padding: 1 2;
        width: 62;
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
            with self._section("Server"):
                yield Input(placeholder="Server IP (e.g. 192.168.1.100)", id="ip")
                with Horizontal(classes="row"):
                    yield Input(placeholder="Port (445)", id="port")
                    yield Input(placeholder="Username", id="user")
                yield Input(placeholder="Password (blank for guest)", password=True, id="password")
                yield Select([], prompt="Share — test the connection to load", id="share")
                with Horizontal(classes="buttons"):
                    yield Button("Test connection", variant="primary", id="btn-test")
                    yield Button("Save", id="btn-save")
                yield Label("Not tested yet.", id="status")

            with self._section("Sync pairs"):
                yield DataTable(id="pairs")
                with Horizontal(classes="buttons"):
                    yield Button("Add pair", variant="success", id="btn-add")
                    yield Button("Remove", variant="error", id="btn-remove")

        with Vertical(id="right"):
            with self._section("Actions"):
                with Horizontal(classes="buttons"):
                    yield Button("Sync ▶", variant="primary", id="btn-sync")
                    yield Button("Dry-run", id="btn-dryrun")
                    yield Button("Restore ◀", variant="warning", id="btn-restore")
                yield Label("", id="current")
                yield ProgressBar(id="bar", total=100, show_eta=False)
            with self._section("Log", id="log-section"):
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
        self._load_config_into_fields()
        self._refresh_pairs()
        self._log("[dim]Ready. Edit server details and press Test connection.[/dim]")

    def _load_config_into_fields(self) -> None:
        sc = self.config.get_server_config()
        if sc.get("ip"):
            self.query_one("#ip", Input).value = sc["ip"]
        self.query_one("#port", Input).value = str(sc.get("port") or 445)
        if sc.get("username"):
            self.query_one("#user", Input).value = sc["username"]
        if sc.get("password"):
            self.query_one("#password", Input).value = sc["password"]
        if sc.get("share"):
            share = sc["share"]
            self.query_one("#share", Select).set_options([(share, share)])
            self.query_one("#share", Select).value = share

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
        """Reporter passed into worker threads; marshals back to the UI."""
        self.call_from_thread(self._log, message)

    def _read_server_fields(self) -> dict:
        port_raw = self.query_one("#port", Input).value.strip() or "445"
        try:
            port = int(port_raw)
        except ValueError:
            port = 445
        return {
            "ip": self.query_one("#ip", Input).value.strip(),
            "port": port,
            "username": self.query_one("#user", Input).value.strip(),
            "password": self.query_one("#password", Input).value,
        }

    def _current_share(self):
        value = self.query_one("#share", Select).value
        # Share names are strings; anything else (blank sentinel) means "none".
        return value if isinstance(value, str) and value else None

    def _set_status(self, text: str, ok: bool = None) -> None:
        colour = "" if ok is None else ("green" if ok else "red")
        label = self.query_one("#status", Label)
        label.update(f"[{colour}]{text}[/{colour}]" if colour else text)

    # ------------------------------------------------------------------ #
    # Progress (dnf-style bar + current action)
    # ------------------------------------------------------------------ #
    def _reset_progress(self) -> None:
        self.query_one("#bar", ProgressBar).update(total=100, progress=0)
        self.query_one("#current", Label).update("")

    def _on_progress(self, done: int, total, name: str, verb: str) -> None:
        bar = self.query_one("#bar", ProgressBar)
        if total:
            bar.update(total=total, progress=done)
        else:
            bar.update(total=None, progress=done)  # indeterminate (unknown total)
        counter = f"{done}/{total}" if total else str(done)
        self.query_one("#current", Label).update(f"{verb} [b]{name}[/b]  ({counter})")

    def _thread_progress(self, done, total, name, verb) -> None:
        """Progress callback for worker threads; marshals to the UI thread."""
        self.call_from_thread(self._on_progress, done, total, name, verb)

    # ------------------------------------------------------------------ #
    # Button routing
    # ------------------------------------------------------------------ #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "btn-test": self.action_test,
            "btn-save": self.action_save,
            "btn-add": self._open_add_pair,
            "btn-remove": self._remove_selected_pair,
            "btn-sync": lambda: self._start_transfer("push", dry_run=False),
            "btn-dryrun": lambda: self._start_transfer("push", dry_run=True),
            "btn-restore": lambda: self._start_transfer("pull", dry_run=False),
        }
        handler = handlers.get(event.button.id)
        if handler:
            handler()

    # ------------------------------------------------------------------ #
    # Server: test + save
    # ------------------------------------------------------------------ #
    def action_test(self) -> None:
        fields = self._read_server_fields()
        if not fields["ip"] or not fields["username"]:
            self._set_status("Enter at least an IP and username.", ok=False)
            return
        self._set_status("Testing…")
        self._log(f"[dim]Testing {fields['ip']}:{fields['port']} as {fields['username']}…[/dim]")
        self._worker_test(fields)

    @work(thread=True, exclusive=True)
    def _worker_test(self, fields: dict) -> None:
        client = SAMBAClient(fields["ip"], fields["username"], fields["password"],
                             "", port=fields["port"], reporter=self._thread_log)
        try:
            shares = client.list_shares()
        except Exception as e:
            self.call_from_thread(self._set_status, f"✗ {e}", False)
            self.call_from_thread(self._log, f"[red]✗ {e}[/red]")
            return
        self.call_from_thread(self._on_test_ok, shares)

    def _on_test_ok(self, shares) -> None:
        self._set_status("✓ Connected", ok=True)
        self._log(f"[green]✓ Connected.[/green] Shares: {', '.join(shares) or '(none listed)'}")
        select = self.query_one("#share", Select)
        if shares:
            keep = self._current_share()
            select.set_options([(s, s) for s in shares])
            select.value = keep if keep in shares else shares[0]

    def action_save(self) -> None:
        fields = self._read_server_fields()
        share = self._current_share()
        if not fields["ip"] or not fields["username"]:
            self._set_status("Enter at least an IP and username.", ok=False)
            return
        if not share:
            self._set_status("Pick a share first (Test connection loads them).", ok=False)
            return
        self.config.set_server(fields["ip"], fields["username"], fields["password"],
                               share, fields["port"])
        self._set_status(f"✓ Saved {fields['ip']}:{fields['port']} / {share}", ok=True)
        self._log(f"[green]✓ Saved server config[/green] ({fields['ip']}:{fields['port']}, share '{share}')")

    # ------------------------------------------------------------------ #
    # Pairs
    # ------------------------------------------------------------------ #
    def _open_add_pair(self) -> None:
        self.push_screen(AddPairScreen(self._current_share() or ""), self._on_pair_added)

    def _on_pair_added(self, result) -> None:
        if not result:
            return
        try:
            name = self.config.add_pair(result["local"], result["dest"], result["name"])
        except ValueError as e:
            self._log(f"[red]✗ {e}[/red]")
            return
        self._refresh_pairs()
        self._log(f"[green]✓ Added pair '{name}'[/green]: {result['local']} → {result['dest']}")

    def _remove_selected_pair(self) -> None:
        table = self.query_one("#pairs", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            self._log("[yellow]No pair selected to remove.[/yellow]")
            return
        name = table.get_row_at(table.cursor_row)[0]
        self.config.remove_pair(name)
        self._refresh_pairs()
        self._log(f"[green]✓ Removed pair '{name}'[/green]")

    # ------------------------------------------------------------------ #
    # Transfers
    # ------------------------------------------------------------------ #
    def action_sync(self) -> None:
        self._start_transfer("push", dry_run=False)

    def action_dryrun(self) -> None:
        self._start_transfer("push", dry_run=True)

    def _start_transfer(self, direction: str, dry_run: bool) -> None:
        # Persist current server fields so what runs matches what's on screen.
        if not dry_run:
            share = self._current_share()
            fields = self._read_server_fields()
            if fields["ip"] and fields["username"] and share:
                self.config.set_server(fields["ip"], fields["username"],
                                       fields["password"], share, fields["port"])
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
        self.call_from_thread(lambda: self.query_one("#current", Label).update(""))


def run() -> None:
    NasyncApp().run()
