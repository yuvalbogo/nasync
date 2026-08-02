import click
from getpass import getpass
from samba_sync import __version__
from samba_sync.config import Config
from samba_sync.samba import SAMBAClient
from samba_sync.sync import Syncer
from samba_sync.watch import Watcher


@click.group()
@click.version_option(version=__version__, prog_name="samba-sync")
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


@main.command()
@click.option("--pair", default=None, help="Specific pair name to list")
def list(pair):
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
        click.echo("❌ Server not configured. Run 'samba-sync init' first.", err=True)
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
                    f"re-run 'samba-sync init' with one of the names above."
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
        click.echo("❌ Server not configured. Run 'samba-sync init' first.")
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
