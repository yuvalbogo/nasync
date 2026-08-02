import os
import click
from getpass import getpass
from nasync import __version__
from nasync.config import Config
from nasync.samba import SAMBAClient
from nasync.sync import Syncer
from nasync.watch import Watcher


def _choose_share(shares):
    """Let the user pick a share by number or name; fall back to manual entry."""
    if not shares:
        click.echo("  (couldn't auto-list shares — the server may not allow it)")
        return click.prompt("  Share name")
    click.echo("  Shares found on the server:")
    for i, s in enumerate(shares, 1):
        click.echo(f"    {i}) {s}")
    while True:
        choice = click.prompt("  Pick a share (number or name)", default="1").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(shares):
            return shares[int(choice) - 1]
        if choice in shares:
            return choice
        click.echo("  Invalid choice — enter a listed number or name.")


def _add_pairs_interactive(config, share):
    """Prompt for one or more local→server sync pairs and save them."""
    while True:
        local = click.prompt("  Local folder to sync")
        local_abs = os.path.abspath(os.path.expanduser(local))
        if os.path.isdir(local_abs):
            click.echo(f"    exists ✓  ({local_abs})")
        elif not click.confirm(f"    '{local_abs}' doesn't exist yet — add it anyway?",
                               default=False):
            continue

        dest = click.prompt("  Destination path under the share (e.g. /Docs)")
        first = dest.strip("/").split("/")[0] if dest.strip("/") else ""
        if first == share:
            click.echo(
                f"    ⚠️  That starts with the share name '{share}'. Paths are "
                f"relative to the share root, so this would nest under "
                f"'{share}/{share}/…' — drop the leading '{share}/' unless you mean it."
            )

        default_name = os.path.basename(local_abs.rstrip("/")) or "root"
        name = click.prompt("  Name for this pair", default=default_name)
        try:
            saved = config.add_pair(local_abs, dest, name)
            click.echo(f"    ✓ added '{saved}':  {local_abs} → {dest}")
        except ValueError as e:
            click.echo(f"    ✗ {e}")

        if not click.confirm("  Add another pair?", default=False):
            break


@click.group()
@click.version_option(version=__version__, prog_name="nasync")
def main():
    """SAMBA Sync Auto - Automatic sync tool for SAMBA home NAS backup"""
    pass


@main.command()
def init():
    """Initialize SAMBA server configuration"""
    click.echo("🔧 SAMBA Server Setup")
    name = click.prompt("Name for this server", default="home-nas")
    ip = click.prompt("Server IP address (e.g. 192.168.1.100)")
    username = click.prompt("Username")
    password = getpass("Password: ")
    share = click.prompt("Share name", default="backup")
    port = click.prompt("SMB port", default=445, type=int)

    config = Config()
    config.add_or_update_server(name, ip, username, password, share, port)
    config.set_active_server(name)
    click.echo(f"✅ Saved server '{name}' ({ip}:{port}) and set it active")


@main.command()
def setup():
    """Guided first-time setup: server, share, and sync pairs (recommended)"""
    config = Config()
    click.echo("🚀 nasync setup — let's get you syncing.\n")

    # ---- Step 1: server details, with a live connection test ----------------
    click.echo("Step 1/3 — Server")
    shares = []
    while True:
        ip = click.prompt("  Server IP address (e.g. 192.168.1.100)")
        port = click.prompt("  SMB port", default=445, type=int)
        username = click.prompt("  Username")
        password = click.prompt("  Password (blank for guest)",
                                default="", hide_input=True, show_default=False)

        click.echo("  → Testing connection...", nl=False)
        probe = SAMBAClient(ip, username, password, "", port=port)
        try:
            shares = probe.list_shares()   # proves IP + auth, and finds shares
            click.echo(" ✓ connected")
            break
        except Exception as e:
            click.echo(" ✗")
            click.echo(f"    {e}")
            if not click.confirm("  Try again?", default=True):
                click.echo("Setup cancelled — nothing was saved.")
                return

    # ---- Step 2: pick the share, and confirm it's actually accessible -------
    click.echo("\nStep 2/3 — Share")
    while True:
        share = _choose_share(shares)
        click.echo(f"  → Checking access to '{share}'...", nl=False)
        try:
            SAMBAClient(ip, username, password, share, port=port).connect()
            click.echo(" ✓")
            break
        except Exception as e:
            click.echo(" ✗")
            click.echo(f"    {e}")
            if not click.confirm("  Pick a different share?", default=True):
                click.echo("Setup cancelled — nothing was saved.")
                return

    default_name = share or ip
    name = click.prompt("  Name for this server", default=default_name)
    config.add_or_update_server(name, ip, username, password, share, port)
    config.set_active_server(name)
    click.echo(f"  Saved server '{name}' ({ip}:{port}, share '{share}').")

    # ---- Step 3: sync pairs -------------------------------------------------
    click.echo("\nStep 3/3 — Sync pairs")
    existing = config.get_pairs()
    if existing:
        click.echo(f"  You already have {len(existing)} pair(s) configured.")
        if not click.confirm("  Keep them?", default=True):
            for p in list(existing):
                config.remove_pair(p["name"])
            click.echo("  Cleared existing pairs.")
    _add_pairs_interactive(config, share)

    click.echo("\n✅ All set! Next steps:")
    click.echo("   nasync status     # review your configuration")
    click.echo("   nasync sync       # push your folders to the server")


@main.command()
@click.argument("local_path")
@click.argument("server_path")
@click.option("--name", default=None, help="Custom name for this pair")
def add(local_path, server_path, name):
    """Add a new sync pair"""
    config = Config()
    try:
        pair_name = config.add_pair(local_path, server_path, name)
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    click.echo(f"✅ Added sync pair: {pair_name}")
    import os
    if not os.path.isdir(os.path.expanduser(local_path)):
        click.echo(f"⚠️  Note: local path does not exist yet: {local_path}")


@main.command(name="list")
@click.option("--pair", default=None, help="Specific pair name to list")
def list_pairs(pair):
    """List all configured sync pairs"""
    config = Config()
    pairs = config.get_pairs()

    if not pairs:
        click.echo("No sync pairs configured yet.")
        return

    server_config = config.get_server_config()
    click.echo(f"\n📋 Server: {server_config['ip']} / {server_config['share']}")
    click.echo("Sync Pairs:")

    for p in pairs:
        click.echo(f"  {p['name']}")
        click.echo(f"    Local:  {p['local']}")
        click.echo(f"    Server: {p['server']}")


@main.command()
@click.argument("name")
def remove(name):
    """Remove a sync pair"""
    config = Config()
    if not config.get_pair(name):
        click.echo(f"❌ Pair '{name}' not found.", err=True)
        raise SystemExit(1)
    config.remove_pair(name)
    click.echo(f"✅ Removed sync pair: {name}")


@main.command()
@click.option("--pair", default=None, help="Specific pair name to sync")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without doing it")
@click.option("--no-delete", is_flag=True,
              help="Don't remove server files that were deleted locally (no mirror)")
def sync(pair, dry_run, no_delete):
    """Sync local directories to the server (incremental mirror)

    Only new or changed files are uploaded; unchanged files are skipped, and
    files removed locally are deleted on the server. Use --no-delete to keep
    server-only files.
    """
    if dry_run:
        click.echo("🔍 Dry-run mode (no files will be transferred)")

    syncer = Syncer()
    try:
        result = syncer.sync_pair(pair, dry_run=dry_run, mirror=not no_delete)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)
    if result["failed"] and not result["ok"]:
        raise SystemExit(1)
    click.echo("\n✅ Sync completed!")


@main.command()
@click.option("--pair", default=None, help="Specific pair name to restore")
@click.option("--dry-run", is_flag=True, help="Show what would be restored without doing it")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def restore(pair, dry_run, yes):
    """Restore local directories from server backup"""
    if dry_run:
        click.echo("🔍 Dry-run mode (no files will be transferred)")
    elif not yes:
        click.echo("⚠️  This will overwrite local files with server versions")
        if not click.confirm("Continue?"):
            click.echo("Cancelled.")
            return

    syncer = Syncer()
    try:
        result = syncer.restore_pair(pair, dry_run=dry_run)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)
    if result["failed"] and not result["ok"]:
        raise SystemExit(1)
    click.echo("\n✅ Restore completed!")


@main.command()
@click.option("--interval", type=int, default=None,
              help="Also sync every N minutes (0 turns the interval off)")
@click.option("--on-change/--no-on-change", "on_change", default=None,
              help="Sync when watched files change (default on)")
@click.option("--status", is_flag=True, help="Show automation settings and exit")
def auto(interval, on_change, status):
    """Keep folders synced automatically (watch on-change and/or on an interval)

    Runs until Ctrl+C. Options are remembered, so a later plain `nasync auto`
    reuses them. Good as a systemd/user service — see the README.
    """
    config = Config()
    a = config.get_automation()
    if interval is not None:
        a = config.set_automation(interval_minutes=interval)
    if on_change is not None:
        a = config.set_automation(on_change=on_change)

    if status:
        click.echo(f"Automation: on-change={a['on_change']}, "
                   f"interval={a['interval_minutes']}m")
        return

    config.set_automation(enabled=True)
    from nasync.automation import Automator
    try:
        Automator(reporter=click.echo).run(
            on_change=a["on_change"], interval_minutes=a["interval_minutes"])
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


@main.command()
@click.option("--pair", default=None, help="Specific pair to watch")
def watch(pair):
    """Watch local directories and auto-sync on changes (alias: nasync auto)"""
    watcher = Watcher()
    try:
        watcher.watch_pairs(pair)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


@main.command()
def tui():
    """Launch the interactive terminal UI"""
    try:
        from nasync.tui import run
    except ImportError:
        click.echo(
            "The TUI needs Textual, which isn't installed. Add it with:\n"
            "  pip install 'nasync[tui]'    (or:  pip install textual )",
            err=True,
        )
        raise SystemExit(1)
    run()


@main.command(name="install-desktop")
@click.option("--uninstall", is_flag=True, help="Remove the launcher instead of adding it")
def install_desktop(uninstall):
    """Add a desktop launcher + icon to your app menu (Linux)"""
    from nasync import desktop
    try:
        if uninstall:
            removed = desktop.uninstall()
            if removed:
                for p in removed:
                    click.echo(f"🗑️  removed {p}")
                click.echo("✅ Desktop launcher removed.")
            else:
                click.echo("Nothing to remove — no launcher was installed.")
        else:
            written = desktop.install()
            for p in written:
                click.echo(f"📄 wrote {p}")
            click.echo("\n✅ Installed! Look for 'nasync' in your application menu.")
            click.echo("   Clicking it opens 'nasync tui' in a terminal window.")
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)


@main.command(name="test-connection")
def test_connection():
    """Check server connectivity, credentials, and share access"""
    config = Config()
    sc = config.get_server_config()
    if not all(sc.get(k) for k in ("ip", "username", "share")):
        click.echo("❌ Server not configured. Run 'nasync init' first.", err=True)
        raise SystemExit(1)

    client = SAMBAClient(sc["ip"], sc["username"], sc["password"],
                         sc["share"], port=sc.get("port", 445))
    click.echo(
        f"🔌 Testing {sc['ip']}:{sc['port']} "
        f"(share '{sc['share']}', user '{sc['username']}')..."
    )

    connected = True
    try:
        client.connect()
        click.echo("  ✓ Reached server, authenticated, and share is accessible")
    except Exception as e:
        click.echo(f"  ✗ {e}", err=True)
        connected = False

    # List shares too — especially useful when the share name is wrong.
    try:
        shares = client.list_shares()
        if shares:
            click.echo(f"  ℹ️  Available shares: {', '.join(shares)}")
            if sc["share"] not in shares:
                click.echo(
                    f"  ⚠️  Configured share '{sc['share']}' is not in that list — "
                    f"re-run 'nasync init' with one of the names above."
                )
    except Exception:
        pass  # listing is best-effort; the connect() result is what matters

    if not connected:
        raise SystemExit(1)
    click.echo("✅ Connection OK — you're ready to sync.")


@main.command(name="servers")
def servers_cmd():
    """List saved servers (the active one is marked *)"""
    config = Config()
    servers = config.list_servers()
    active = config.get_active_server_name()
    if not servers:
        click.echo("No servers configured yet. Run 'nasync setup'.")
        return
    for s in servers:
        mark = "* " if s["name"] == active else "  "
        click.echo(f"{mark}{s['name']}  ({s['ip']}:{s.get('port', 445)}/{s['share']}, user {s['username']})")


@main.command()
@click.argument("name")
def use(name):
    """Switch the active server"""
    config = Config()
    if config.set_active_server(name):
        click.echo(f"✅ Active server: {name}")
    else:
        click.echo(f"❌ No server named '{name}'. See 'nasync servers'.", err=True)
        raise SystemExit(1)


@main.command()
def status():
    """Show sync configuration status"""
    config = Config()
    servers = config.list_servers()
    active = config.get_active_server_name()
    pairs = config.get_pairs()

    if not servers:
        click.echo("❌ No servers configured. Run 'nasync setup' first.")
        raise SystemExit(1)

    click.echo("\n📊 nasync status")
    click.echo("  Servers:")
    for s in servers:
        mark = "●" if s["name"] == active else "○"
        click.echo(f"    {mark} {s['name']}  ({s['ip']}:{s.get('port', 445)}/{s['share']}, user {s['username']})")

    click.echo(f"\n  Sync pairs ({len(pairs)})  →  active server '{active}'")
    if pairs:
        for p in pairs:
            click.echo(f"    • {p['name']}: {p['local']} → {p['server']}")
    else:
        click.echo("    ⚠️  none yet")


if __name__ == "__main__":
    main()
