import os
import click
from getpass import getpass
from samba_sync import __version__
from samba_sync.config import Config
from samba_sync.samba import SAMBAClient
from samba_sync.sync import Syncer
from samba_sync.watch import Watcher


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
    ip = click.prompt("Server IP address (e.g. 192.168.1.100)")
    username = click.prompt("Username")
    password = getpass("Password: ")
    share = click.prompt("Share name", default="backup")
    port = click.prompt("SMB port", default=445, type=int)

    config = Config()
    config.set_server(ip, username, password, share, port)
    click.echo(f"✅ Server configured: {ip}:{port}")


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

    config.set_server(ip, username, password, share, port)
    click.echo(f"  Saved server {ip}:{port}, share '{share}'.")

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
def sync(pair, dry_run):
    """Manually sync local directories to server"""
    if dry_run:
        click.echo("🔍 Dry-run mode (no files will be transferred)")

    syncer = Syncer()
    try:
        syncer.sync_pair(pair, dry_run=dry_run)
        click.echo("\n✅ Sync completed!")
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


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
        syncer.restore_pair(pair, dry_run=dry_run)
        click.echo("\n✅ Restore completed!")
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


@main.command()
@click.option("--pair", default=None, help="Specific pair to watch")
def watch(pair):
    """Watch local directories and auto-sync on changes"""
    watcher = Watcher()
    try:
        watcher.watch_pairs(pair)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
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


@main.command()
def status():
    """Show sync configuration status"""
    config = Config()
    server_config = config.get_server_config()
    pairs = config.get_pairs()

    if not server_config["ip"]:
        click.echo("❌ Server not configured. Run 'nasync init' first.")
        raise SystemExit(1)

    click.echo(f"\n📊 SAMBA Sync Status")
    click.echo(f"  Server: {server_config['ip']}:{server_config['port']}/{server_config['share']}")
    click.echo(f"  User: {server_config['username']}")
    click.echo(f"  Pairs: {len(pairs)}")

    if pairs:
        click.echo("\n  Configured pairs:")
        for p in pairs:
            click.echo(f"    • {p['name']}: {p['local']} → {p['server']}")
    else:
        click.echo("\n  ⚠️  No sync pairs configured yet.")


if __name__ == "__main__":
    main()
