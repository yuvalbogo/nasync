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
    Button, DataTable, Footer, Header, Input, Label, RichLog, Select, Static,
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
    Screen { layout: horizontal; }
    #left { width: 55%; padding: 1 2; }
    #right { width: 45%; padding: 1 2; }
    .title { text-style: bold; }
    .section { border: round $primary; padding: 0 1; margin-bottom: 1; height: auto; }
    .row { height: auto; }
    Input { width: 1fr; }
    #status { margin-top: 1; }
    #pairs { height: 8; }
    #log { border: round $secondary; height: 1fr; }
    .buttons { height: auto; align: left middle; }
    Button { margin: 1 1 0 0; }
    #dialog { background: $surface; border: thick $primary; padding: 1 2; width: 60; height: auto; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "save", "Save server"),
        ("t", "test", "Test connection"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="left"):
            with Vertical(classes="section"):
                yield Label("Server", classes="title")
                yield Input(placeholder="Server IP (e.g. 192.168.1.100)", id="ip")
                with Horizontal(classes="row"):
                    yield Input(placeholder="Port (445)", id="port")
                    yield Input(placeholder="Username", id="user")
                yield Input(placeholder="Password (blank for guest)", password=True, id="password")
                with Horizontal(classes="buttons"):
                    yield Button("Test connection", variant="primary", id="btn-test")
                    yield Button("Save server", id="btn-save")
                yield Label("Not tested yet.", id="status")
                yield Label("Share")
                yield Select([], prompt="Test the connection to load shares", id="share")

            with Vertical(classes="section"):
                yield Label("Sync pairs", classes="title")
                yield DataTable(id="pairs")
                with Horizontal(classes="buttons"):
                    yield Button("Add pair", variant="success", id="btn-add")
                    yield Button("Remove selected", variant="error", id="btn-remove")

            with Vertical(classes="section"):
                yield Label("Actions", classes="title")
                with Horizontal(classes="buttons"):
                    yield Button("Sync ▶", variant="primary", id="btn-sync")
                    yield Button("Dry-run", id="btn-dryrun")
                    yield Button("Restore ◀", variant="warning", id="btn-restore")
        with Vertical(id="right"):
            yield Label("Log", classes="title")
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
        return value if value != Select.BLANK else None

    def _set_status(self, text: str, ok: bool = None) -> None:
        colour = "" if ok is None else ("green" if ok else "red")
        label = self.query_one("#status", Label)
        label.update(f"[{colour}]{text}[/{colour}]" if colour else text)

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
    def _start_transfer(self, direction: str, dry_run: bool) -> None:
        # Persist current server fields so what runs matches what's on screen.
        if not dry_run:
            share = self._current_share()
            fields = self._read_server_fields()
            if fields["ip"] and fields["username"] and share:
                self.config.set_server(fields["ip"], fields["username"],
                                       fields["password"], share, fields["port"])
        verb = {"push": "Sync", "pull": "Restore"}[direction]
        self._log(f"\n[b]▶ {verb}{' (dry-run)' if dry_run else ''}…[/b]")
        self._worker_transfer(direction, dry_run)

    @work(thread=True, exclusive=True)
    def _worker_transfer(self, direction: str, dry_run: bool) -> None:
        syncer = Syncer(reporter=self._thread_log)
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


def run() -> None:
    NasyncApp().run()
