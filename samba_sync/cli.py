import click
from getpass import getpass
from samba_sync.config import Config
from samba_sync.sync import Syncer
from samba_sync.watch import Watcher


@click.group()
@click.version_option()
def main():
    """SAMBA Sync Auto - Automatic sync tool for SAMBA home NAS backup"""
    pass


@main.command()
def init():
    """Initialize SAMBA server configuration"""
    click.echo("🔧 SAMBA Server Setup")
    ip = click.prompt("Server IP address", default="10.100.102.200")
    username = click.prompt("Username")
    password = getpass("Password: ")
    share = click.prompt("Share name", default="backup")

    config = Config()
    config.set_server(ip, username, password, share)
    click.echo(f"✅ Server configured: {ip}")


@main.command()
@click.argument("local_path")
@click.argument("server_path")
@click.option("--name", default=None, help="Custom name for this pair")
def add(local_path, server_path, name):
    """Add a new sync pair"""
    config = Config()
    config.add_pair(local_path, server_path, name)
    pair_name = name or local_path.split("/")[-1]
    click.echo(f"✅ Added sync pair: {pair_name}")


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
        click.echo("🔍 Dry-run mode (no files will be synced)")

    syncer = Syncer()
    try:
        syncer.sync_pair(pair)
        click.echo("\n✅ Sync completed!")
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


@main.command()
@click.option("--pair", default=None, help="Specific pair name to restore")
def restore(pair):
    """Restore local directories from server backup"""
    click.echo("⚠️  This will overwrite local files with server versions")
    if not click.confirm("Continue?"):
        click.echo("Cancelled.")
        return

    syncer = Syncer()
    try:
        syncer.restore_pair(pair)
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
    click.echo(f"  Server: {server_config['ip']}:{server_config['share']}")
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
