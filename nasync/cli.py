import os
import sys
import click
from getpass import getpass
from nasync import __version__
from nasync.core import activity
from nasync.core.config import (SYNC_MODES, Config, join_remote,
                                normalize_remote)
from nasync.core.samba import SAMBAClient
from nasync.core.sync import Syncer
from nasync.format import format_bytes, format_volume
from nasync.system import notify, service


#: Said at the two points a server is created. Deliberately concrete about the
#: consequence rather than abstract about the concept: "scope" means nothing at
#: 11pm, "the folders beside it can never be deleted" means something.
ROOT_EXPLANATION = """  A sync root is the one folder on the share that NASync is allowed to
  touch. Every folder you sync goes inside it, and NASync can never
  reach anything beside it — so a photo library or a video sitting next
  to the root stays there no matter what you sync, or how you set the
  sync mode later.

  This matters because mirror mode deletes: it makes the server match
  your computer, so anything in range that you don't have locally goes.
  The root is what puts things out of range permanently, instead of
  relying on nobody ever pointing a folder at them by mistake."""


def _prompt_root(default: str = "nasync") -> str:
    """Ask for the sync root, explaining what it protects."""
    click.echo("\n  Sync root")
    click.echo(ROOT_EXPLANATION)
    while True:
        raw = click.prompt("\n  Folder on the share to sync inside",
                           default=default)
        try:
            root = normalize_remote(raw)
        except ValueError as e:
            click.echo(f"    ✗ {e}")
            continue
        if not root:
            click.echo(
                "    ⚠️  An empty root means the whole share is in range, "
                "which is the situation the root exists to avoid.")
            if not click.confirm("    Use the whole share anyway?",
                                 default=False):
                continue
        return root


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

        root = config.get_root()
        inside = f"{share}:/{root}" if root else f"{share}:/"
        dest = click.prompt(f"  Destination inside {inside} (e.g. Docs)")
        first = dest.strip("/").split("/")[0] if dest.strip("/") else ""
        if first == share:
            click.echo(
                f"    ⚠️  That starts with the share name '{share}'. Paths are "
                f"relative to the sync root, so this would nest under "
                f"'{inside}/{share}/…' — drop the leading '{share}/' unless you mean it."
            )

        default_name = os.path.basename(local_abs.rstrip("/")) or "root"
        name = click.prompt("  Name for this pair", default=default_name)
        try:
            saved = config.add_pair(local_abs, dest, name)
            click.echo(f"    ✓ added '{saved}':  {local_abs} → "
                       f"{share}:/{config.resolve_remote(dest)}")
        except ValueError as e:
            click.echo(f"    ✗ {e}")

        if not click.confirm("  Add another pair?", default=False):
            break


@click.group()
@click.version_option(version=__version__, prog_name="nasync")
def main():
    """NASync — sync your local folders to a SAMBA home NAS, with class."""
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
    encrypt = click.confirm(
        "Encrypt the connection? (needs SMB 3; slower on older NAS hardware)",
        default=False)
    root = _prompt_root()

    config = Config()
    config.add_or_update_server(name, ip, username, password, share, port,
                                encrypt, root=root)
    config.set_active_server(name)
    click.echo(f"✅ Saved server '{name}' ({ip}:{port}) and set it active")
    click.echo(f"   Syncing inside '{share}:/{root}' — "
               f"nothing beside it can be touched." if root else
               "   ⚠️  No sync root: the whole share is in range.")


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

    root = _prompt_root()

    default_name = share or ip
    name = click.prompt("\n  Name for this server", default=default_name)
    # Re-running setup against a server that already exists must not turn its
    # encryption off. `add_or_update_server` defaults the flag to False, so
    # omitting it here would silently downgrade a configured NAS to plaintext
    # transfers — the opposite of what `nasync encryption` promises.
    existing = config.get_server(name)
    config.add_or_update_server(name, ip, username, password, share, port,
                                encrypt=bool(existing and existing["encrypt"]),
                                root=root)
    config.set_active_server(name)
    click.echo(f"  Saved server '{name}' ({ip}:{port}, share '{share}').")
    if root:
        click.echo(f"  Everything syncs inside '{share}:/{root}'. Anything "
                   f"else on this share is out of NASync's reach.")

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
@click.option("--server", default=None,
              help="Attach to this server instead of the active one")
def add(local_path, server_path, name, server):
    """Add a sync pair to the active server (or --server)"""
    config = Config()
    try:
        pair_name = config.add_pair(local_path, server_path, name, server=server)
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    owner = server or config.get_active_server_name()
    click.echo(f"✅ Added sync pair '{pair_name}' to server '{owner}'")
    if not os.path.isdir(os.path.expanduser(local_path)):
        click.echo(f"⚠️  Note: local path does not exist yet: {local_path}")


@main.command(name="list")
@click.option("--pair", default=None, help="Specific pair name to list")
@click.option("--server", default=None, help="List another server's pairs")
@click.option("--all", "show_all", is_flag=True, help="List every server's pairs")
def list_pairs(pair, server, show_all):
    """List sync pairs for the active server (each server has its own)"""
    config = Config()
    targets = ([s["name"] for s in config.list_servers()] if show_all
               else [server or config.get_active_server_name()])
    if not any(targets):
        click.echo("No servers configured yet. Run 'nasync setup'.")
        return

    for name in targets:
        pairs = config.get_pairs(name)
        details = config.get_server(name, with_password=False)
        location = f"{details['ip']} / {details['share']}" if details else "?"
        click.echo(f"\n📋 {name}  ({location})")
        root = config.get_root(name)
        share = details["share"] if details else ""
        click.echo(f"  Sync root: {share}:/{root}" if root
                   else "  Sync root: none — the whole share is in range")
        if not pairs:
            click.echo("  No sync pairs yet.")
            continue
        clashes = config.overlapping_pairs(name)
        for p in pairs:
            click.echo(f"  {p['name']}")
            click.echo(f"    Local:  {p['local']}")
            # The resolved path, not the stored one: where files actually land
            # is the thing worth checking before a mirror sync.
            click.echo(f"    Server: {share}:/{config.resolve_remote(p['server'], name)}")
            if p["name"] in clashes:
                click.echo(f"    ⚠️  Shares its server folder with "
                           f"{', '.join(clashes[p['name']])} — skipped by sync "
                           f"until each has its own (nasync remove/add).")


@main.command()
@click.argument("name")
@click.option("--server", default=None,
              help="Remove from this server instead of the active one")
def remove(name, server):
    """Remove a sync pair from the active server (or --server)"""
    config = Config()
    owner = server or config.get_active_server_name()
    if not config.get_pair(name, owner):
        click.echo(f"❌ Pair '{name}' not found on server '{owner}'.", err=True)
        raise SystemExit(1)
    config.remove_pair(name, server=owner)
    click.echo(f"✅ Removed sync pair '{name}' from '{owner}'")


def _finished(result: dict, verb: str) -> None:
    """Say how a run ended — and not "completed!" over files that failed."""
    problems = result.get("failed", 0) + result.get("errors", 0)
    if result.get("cancelled"):
        return
    if problems:
        click.echo(f"\n⚠️  {verb} finished with {problems} problem(s) — see above.")
    else:
        click.echo(f"\n✅ {verb} completed!")


@main.command()
@click.option("--pair", default=None, help="Specific pair name to sync")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without doing it")
@click.option("--mode", type=click.Choice(SYNC_MODES), default=None,
              help="Override the saved sync mode for this run")
@click.option("--no-delete", is_flag=True, help="Alias for --mode update")
@click.option("--allow-mass-delete", is_flag=True,
              help="Let a mirror run delete more than half of a folder's "
                   "server files without asking")
def sync(pair, dry_run, mode, no_delete, allow_mass_delete):
    """Sync local directories to the server (incremental)

    Only new or changed files are uploaded and unchanged ones are skipped, in
    both modes. What differs is server-only files: 'mirror' deletes them so the
    server matches your folders exactly, 'update' leaves them in place. The
    saved default comes from 'nasync mode'.

    A mirror run that would delete more than half of a folder's files on the
    server (50 or more) stops and asks first; one whose local folder is empty
    is never run at all.
    """
    if dry_run:
        click.echo("🔍 Dry-run mode (no files will be transferred)")

    # --mode wins over the older --no-delete when both are given.
    mirror = None
    if mode is not None:
        mirror = mode == "mirror"
    elif no_delete:
        mirror = False

    try:
        result = Syncer(source="cli").sync_pair(
            pair, dry_run=dry_run, mirror=mirror,
            allow_mass_delete=allow_mass_delete)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)

    # A held-back mirror run is a question, so ask it — then run just those
    # folders again with the answer.
    held = [r for r in result.get("refused", [])
            if r.get("confirmable") and not r.get("dry_run")]
    if held and not dry_run:
        click.echo("")
        for r in held:
            click.echo(f"🛑 '{r['pair']}' would delete {r['deletes']:,} of "
                       f"{r['total']:,} file(s) on the server.")
        if not sys.stdin.isatty():
            click.echo("   Not asking without a terminal. Check with "
                       "'nasync sync --dry-run', then run again with "
                       "--allow-mass-delete.")
        elif click.confirm("   Delete them? Check with --dry-run first if "
                           "unsure", default=False):
            for r in held:
                try:
                    again = Syncer(source="cli").sync_pair(
                        r["pair"], mirror=mirror, allow_mass_delete=True)
                except Exception as e:
                    click.echo(f"❌ Error: {e}", err=True)
                    raise SystemExit(1)
                result["ok"] += again["ok"]
                result["failed"] += again["failed"] - 1
                result["errors"] += again["errors"]
    if result["failed"] and not result["ok"]:
        _finished(result, "Sync")
        raise SystemExit(1)
    _finished(result, "Sync")


@main.command()
@click.option("--pair", default=None, help="Specific pair name to restore")
@click.option("--dry-run", is_flag=True, help="Show what would be restored without doing it")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def restore(pair, dry_run, yes):
    """Restore local directories from server backup

    Adds and overwrites local files only — local files the server doesn't have
    are never deleted, whichever sync mode is set.
    """
    if dry_run:
        click.echo("🔍 Dry-run mode (no files will be transferred)")
    elif not yes:
        click.echo("⚠️  This will overwrite local files with server versions")
        if not click.confirm("Continue?"):
            click.echo("Cancelled.")
            return

    try:
        result = Syncer(source="cli").restore_pair(pair, dry_run=dry_run)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)
    if result["failed"] and not result["ok"]:
        _finished(result, "Restore")
        raise SystemExit(1)
    _finished(result, "Restore")


@main.command()
def cancel():
    """Stop the sync that is running — in the background or anywhere else"""
    config = Config()
    status_now = activity.read_status(config.config_dir)
    if status_now is None:
        click.echo("Nothing is syncing right now.")
        return
    if activity.request_cancel(config.config_dir):
        click.echo(f"⏹️  Asked it to stop: {activity.describe(status_now)}.")
        click.echo("   It finishes the file it is on, then stops.")
    else:
        click.echo("❌ Couldn't reach the running sync.", err=True)
        raise SystemExit(1)


_MODE_HELP = {
    "mirror": "the server is made to match your local folders exactly; "
              "server-only files are deleted",
    "update": "new and changed files are copied; nothing is deleted on the server",
}


@main.command()
@click.option("--reset", is_flag=True,
              help="Discard the saved passwords so they can be entered again")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def credentials(reset, yes):
    """Show how saved passwords are protected, or discard them

    --reset is the way out of a `.creds` that can't be opened — a keyring whose
    key was deleted, or a file carried over from a different install. NASync
    refuses to save over passwords it can't read, so without this the only
    recovery would itself be blocked.
    """
    config = Config()
    click.echo(f"Saved passwords: {config.credentials_status()}")
    problem = config.check_credentials()
    if problem:
        click.echo(f"🔒 {problem}")

    if not reset:
        if problem:
            click.echo("\n   Unlock your keyring and try again, or discard them "
                       "with: nasync credentials --reset")
        return

    count = len(config.list_servers())
    if not yes and not click.confirm(
            f"\nDiscard the saved password for {count} server(s)? "
            f"You'll need to enter them again", default=False):
        click.echo("Nothing was changed.")
        return

    config.reset_credentials()
    click.echo("✅ Saved passwords discarded. Re-enter them with "
               "'nasync init' or in the desktop app.")


def _connected_client(server_name=None):
    """A connected client for the named server, or exit with a clear message."""
    config = Config()
    name = server_name or config.get_active_server_name()
    server = config.get_server(name) if name else None
    if not server or not server.get("ip") or not server.get("share"):
        click.echo("❌ No server configured. Run 'nasync setup' first.", err=True)
        raise SystemExit(1)
    client = SAMBAClient(server["ip"], server["username"],
                         server.get("password", ""), server["share"],
                         port=server.get("port", 445),
                         encrypt=server.get("encrypt", False))
    try:
        client.connect()
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    return client, server


def _scan_ticker():
    """A progress callback that overwrites one line while a share is walked.

    Only on a TTY: piped into a file or another command, a carriage return
    every directory produces one enormous unreadable line.
    """
    if not sys.stderr.isatty():
        return None

    def tick(directories, files):
        click.echo(f"\r   scanning… {directories} folders, {files} files",
                   nl=False, err=True)
    return tick


def _clear_ticker():
    if sys.stderr.isatty():
        click.echo("\r" + " " * 50 + "\r", nl=False, err=True)


@main.command(name="ls")
@click.argument("path", default="", required=False)
@click.option("--server", "server_name", default=None,
              help="Which saved server to browse (default: the active one)")
def ls(path, server_name):
    """List a directory on the server

    Browses the share so you can find a destination without guessing at it.
    PATH is relative to the share root; with no PATH, lists the root itself.

    Reads one directory rather than indexing the share, so it stays fast on a
    NAS with a lot on it.
    """
    client, server = _connected_client(server_name)
    try:
        entries = client.list_directory(path)
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    finally:
        # release, not disconnect: `delete_session` closes the pooled
        # connection for the whole process. Harmless in a one-shot CLI,
        # wrong the moment this code is reused anywhere long-lived.
        client.release()

    click.echo(f"📂 {server['share']}:/{path.strip('/')}")
    if not entries:
        click.echo("   (empty)")
        return
    # Sized to the longest name so the sizes line up in a column, but capped:
    # one pathological filename shouldn't push every size off the screen.
    names = {e["name"] + ("/" if e["is_dir"] else "") for e in entries}
    width = min(max(max(len(n) for n in names), 12), 48)
    for entry in entries:
        if entry["is_dir"]:
            mark, size = "📁", ""
        elif entry["is_symlink"]:
            mark, size = "🔗", ""
        else:
            mark, size = "  ", format_bytes(entry["size"])
        name = entry["name"] + ("/" if entry["is_dir"] else "")
        click.echo(f"  {mark} {name:<{width}}  {size:>9}".rstrip())

    dirs = sum(1 for e in entries if e["is_dir"])
    click.echo(f"\n   {dirs} folder(s), {len(entries) - dirs} file(s)")


@main.command()
@click.argument("path", default="", required=False)
@click.option("--server", "server_name", default=None,
              help="Which saved server to inspect (default: the active one)")
@click.option("--top", default=15, show_default=True,
              help="How many entries to list")
def usage(path, server_name, top):
    """Show what is using space on the server

    Ranks each folder by everything *beneath* it, so a big file buried three
    levels down is attributed to the folder you would actually delete.

    PATH is relative to the share root; pass a folder from the listing to drill
    into it.
    """
    client, server = _connected_client(server_name)
    try:
        volume = None
        try:
            volume = client.volume_info()
        except Exception:
            # Not every server answers this, and a usage report is still
            # useful without it. Silence beats an error about a side dish.
            pass
        report = client.usage_tree(path, on_scan=_scan_ticker())
        _clear_ticker()
    except Exception as e:
        _clear_ticker()
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    finally:
        # release, not disconnect: `delete_session` closes the pooled
        # connection for the whole process. Harmless in a one-shot CLI,
        # wrong the moment this code is reused anywhere long-lived.
        client.release()

    location = f"{server['share']}:/{report['path']}"
    click.echo(f"\n📊 {location} — {format_bytes(report['total'])} "
               f"in {report['files']:,} file(s)")
    summary, quota = format_volume(volume)
    if summary:
        click.echo(f"   Disk: {summary}")
        if quota:
            click.echo(f"   {quota}")

    children = report["children"]
    if not children:
        click.echo("\n   (nothing here)")
        return

    click.echo("")
    largest = children[0]["total"] or 1
    for child in children[:top]:
        filled = round(10 * child["total"] / largest)
        bar = "█" * filled + "░" * (10 - filled)
        name = child["name"] + ("/" if child["is_dir"] else "")
        click.echo(f"   {bar} {format_bytes(child['total']):>9}  {name}")

    if len(children) > top:
        rest = sum(c["total"] for c in children[top:])
        click.echo(f"   … and {len(children) - top} more, "
                   f"{format_bytes(rest)} in total")
    if any(c["is_dir"] for c in children):
        first = next(c for c in children if c["is_dir"])
        click.echo(f"\n   Drill in with: nasync usage {first['path']}")


@main.command()
@click.argument("path", default="", required=False)
@click.option("--server", "server_name", default=None,
              help="Which saved server to inspect (default: the active one)")
@click.option("--verify", is_flag=True,
              help="Confirm by content. Reads every candidate over the network.")
@click.option("--min-size", default=1024 * 1024, show_default=True,
              help="Ignore files smaller than this many bytes")
@click.option("--top", default=15, show_default=True,
              help="How many groups to list")
def duplicates(path, server_name, verify, min_size, top):
    """Find files that appear more than once on the server

    Grouping by size is free — every size is already known from the scan. Use
    --verify to confirm by content, which has to read each candidate over the
    network because SMB has no server-side hash.
    """
    client, server = _connected_client(server_name)
    try:
        groups = client.find_duplicates(path, min_size=min_size,
                                        on_scan=_scan_ticker())
        _clear_ticker()
        if verify and groups:
            cost = client.duplicate_scan_cost(groups)
            click.echo(f"🔍 Verifying by content — reading {format_bytes(cost)} "
                       f"from the server…")
            groups = client.find_duplicates(path, verify=True, min_size=min_size)
    except Exception as e:
        _clear_ticker()
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    finally:
        # release, not disconnect: `delete_session` closes the pooled
        # connection for the whole process. Harmless in a one-shot CLI,
        # wrong the moment this code is reused anywhere long-lived.
        client.release()

    location = f"{server['share']}:/{path.strip('/')}"
    if not groups:
        click.echo(f"✅ No duplicate candidates under {location} "
                   f"(files under {format_bytes(min_size)} were skipped).")
        return

    recoverable = sum(g["wasted"] for g in groups)
    click.echo(f"\n🔁 {len(groups)} group(s) under {location} — "
               f"{format_bytes(recoverable)} recoverable")
    if not verify:
        cost = client.duplicate_scan_cost(groups)
        click.echo(f"   Matched by size only. Confirm with --verify "
                   f"(reads {format_bytes(cost)}).")

    for group in groups[:top]:
        click.echo(f"\n   {format_bytes(group['wasted'])} wasted · "
                   f"{len(group['paths'])} × {format_bytes(group['size'])}"
                   f"{' · verified' if group['hash'] else ''}")
        for rel in group["paths"]:
            click.echo(f"      {rel}")

    if len(groups) > top:
        click.echo(f"\n   … and {len(groups) - top} more group(s)")
    click.echo("\n   NASync never deletes anything here — remove copies yourself.")


@main.command()
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@click.option("--server", "server_name", default=None,
              help="Which saved server to change (default: the active one)")
def encryption(state, server_name):
    """Show or set SMB 3 encryption for a server

    Encryption protects the *contents* of your transfers on the network. It
    needs SMB 3 or newer at both ends, and costs throughput — on a NAS without
    hardware AES it can halve transfer speed, which is why it is off unless you
    turn it on.

    Turning it on is a hard requirement, not a preference: if the server can't
    encrypt, NASync refuses the connection rather than quietly sending your
    files in the clear.
    """
    config = Config()
    name = server_name or config.get_active_server_name()
    if not name:
        click.echo("❌ No servers configured. Run 'nasync setup' first.", err=True)
        raise SystemExit(1)

    server = config.get_server(name)
    if not server:
        click.echo(f"❌ No server named '{name}'. See 'nasync servers'.", err=True)
        raise SystemExit(1)

    if state is None:
        current = "on" if server.get("encrypt") else "off"
        click.echo(f"Encryption for '{name}': {current}")
        click.echo(f"  Switch with: nasync encryption "
                   f"{'off' if current == 'on' else 'on'} --server {name}")
        return

    wanted = state == "on"
    config.save_server(name, server["ip"], server["username"],
                       server.get("password", ""), server["share"],
                       server.get("port", 445), encrypt=wanted)
    click.echo(f"✅ Encryption for '{name}': {state}")
    if wanted:
        # Said now rather than at the next sync: a scheduled run that starts
        # failing at 02:00 because the NAS is too old is a bad way to find out.
        click.echo("   Check it works now with: nasync test-connection")


@main.command()
@click.argument("path", required=False)
@click.option("--server", "server", default=None, metavar="NAME",
              help="Which server (default: the active one)")
def root(path, server):
    """Show or set a server's sync root — the folder it may touch

    Every sync pair on the server lives inside this folder, and NASync can
    never reach anything beside it. That is what keeps a photo library or a
    video sitting next to the root safe from a mirror sync, permanently,
    rather than only for as long as no pair happens to point at it.
    """
    config = Config()
    server = _require_server(config, server)
    share = (config.get_server(server, with_password=False) or {}).get("share", "")

    if path is None:
        current = config.get_root(server)
        if current:
            click.echo(f"Sync root ({server}): {share}:/{current}")
            click.echo("  Everything beside it on this share is out of reach.")
        else:
            click.echo(f"Sync root ({server}): none — the whole share is in range")
            click.echo("  Set one with: nasync root <folder>")
        return

    pairs = config.get_pairs(server)
    try:
        new_root = normalize_remote(path)
    except ValueError as e:
        raise click.ClickException(str(e))

    # Pairs are stored relative to the root, so moving it moves all of them —
    # and in mirror mode the first sync afterwards would delete whatever is in
    # the new location and orphan everything in the old one.
    if pairs and new_root != config.get_root(server):
        click.echo(f"⚠️  '{server}' has {len(pairs)} sync pair(s). They are "
                   f"stored relative to the root, so they would move:")
        for pair in pairs[:5]:
            click.echo(f"     {share}:/{config.resolve_remote(pair['server'], server)}"
                       f"  →  {share}:/{join_remote(new_root, pair['server'])}")
        if len(pairs) > 5:
            click.echo(f"     … and {len(pairs) - 5} more")
        if config.get_sync_mode(server) == "mirror":
            click.echo("   This server is on mirror mode, so the next sync "
                       "would delete anything already in the new location "
                       "that isn't on this machine.")
        if not click.confirm("   Move them?", default=False):
            click.echo("Nothing changed.")
            return

    config.set_root(new_root, server=server)
    if new_root:
        click.echo(f"✅ Sync root ({server}): {share}:/{new_root}")
    else:
        click.echo(f"⚠️  Sync root ({server}) cleared — the whole share is now "
                   f"in range of a sync.")


@main.command()
@click.argument("mode", type=click.Choice(SYNC_MODES), required=False)
@click.option("--server", "server", default=None, metavar="NAME",
              help="Which server to set this for (default: the active one)")
def mode(mode, server):
    """Show or set a server's sync mode, used by sync, watch, and auto

    Per server since 0.18.0: a NAS you mirror and one you only ever add to are
    two different rules about deleting, not one.

    With no argument, prints the current mode. Restore is unaffected — it never
    deletes local files in either mode.
    """
    config = Config()
    server = _require_server(config, server)
    if mode:
        config.set_sync_mode(mode, server=server)
        click.echo(f"✅ Sync mode ({server}): {mode} — {_MODE_HELP[mode]}")
        return
    current = config.get_sync_mode(server)
    click.echo(f"Sync mode ({server}): {current} — {_MODE_HELP[current]}")
    other = next(m for m in SYNC_MODES if m != current)
    click.echo(f"  Switch with: nasync mode {other}")


def _describe_automation(config) -> list:
    """The automation settings as lines, shared by `status` and `auto --status`.

    Per server since 0.18.0, so this lists each one that has a trigger on
    rather than describing a single app-wide setting. Servers with nothing on
    are summarised in a line rather than each getting an "off" block, which on
    a machine with several NASes would be mostly noise.
    """
    servers = [s["name"] for s in config.list_servers()]
    automated = config.automated_servers()
    lines = []

    if not servers:
        return ["  (no servers configured)"]

    for name in automated:
        a = config.get_automation(name)
        lines.append(f"  {name}  ({config.get_sync_mode(name)} mode)")
        lines.append(
            f"    Live sync:      {'on' if a['live_sync'] else 'off'}"
            f"{'  (syncs seconds after a file changes, on that network)' if a['live_sync'] else ''}")
        lines.append(
            f"    Scheduled sync: "
            f"{'on at ' + a['scheduled_time'] if a['scheduled_sync'] else 'off'}")
        if a["interval_minutes"] > 0:
            lines.append(f"    Interval sync:  every {a['interval_minutes']} min")

    idle = [n for n in servers if n not in automated]
    if idle:
        lines.append(f"  Not automated:  {', '.join(idle)}")

    # Both of these are app-wide, so they are stated once rather than per
    # server — there is one background service and one notification setting.
    if not config.get_background():
        lines.append("  Background:     off — nothing runs the triggers above "
                     "until you turn it back on")
    elif automated:
        lines.append(f"  Background:     {service.status()['detail']}")
    lines.append(f"  Notifications:  {_describe_notifications(config)}")
    return lines


def _describe_activity(config) -> list:
    """What is syncing now, or what synced last — from any process."""
    running = activity.read_status(config.config_dir)
    if running:
        lines = [f"🔄 Syncing now — {activity.describe(running)}"]
        if running.get("files_total"):
            lines.append(f"   {running.get('verb', 'Working')} "
                         f"{running.get('files_done', 0)}/{running['files_total']}"
                         f" — {running.get('file', '')}")
        lines.append("   Stop it with: nasync cancel")
        return lines
    last = activity.read_last(config.config_dir)
    if not last:
        return ["Last sync: none recorded yet"]
    return [activity.describe_last(last)]


def _describe_notifications(config) -> str:
    """One line covering both the setting and whether the desktop can show them."""
    if not config.get_notifications():
        return "off"
    if not notify.available():
        return ("on, but this machine has no notification daemon "
                "(install libnotify or run a desktop session)")
    return "on — background syncs announce themselves"


def _require_server(config, server: str = None) -> str:
    """Resolve which server a per-server setting applies to, or refuse.

    Since 0.18.0 the sync mode and the automatic triggers live on a server, so
    every command that writes one has to know which — and say so when there
    isn't one, rather than failing further down in the config.
    """
    name = server or config.get_active_server_name()
    if not name:
        raise click.ClickException(
            "No server configured. Add one with 'nasync setup' first.")
    if not config.get_server(name, with_password=False):
        known = ", ".join(s["name"] for s in config.list_servers()) or "none"
        raise click.ClickException(
            f"No server named '{name}'. Configured servers: {known}.")
    return name


def _apply_service(config) -> None:
    """Match the background service to the settings, reporting what happened."""
    try:
        result = service.apply(config)
    except Exception as e:
        click.echo(f"⚠️  Settings saved, but the background service failed: {e}")
        return
    click.echo(f"   {result['detail']}")


@main.command(name="live-sync")
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@click.option("--server", "server", default=None, metavar="NAME",
              help="Which server to set this for (default: the active one)")
def live_sync(state, server):
    """Turn live sync on or off (sync moments after a file changes)

    Per server since 0.18.0: each NAS has its own triggers, so turning this on
    for your home server leaves a work server alone.

    Only syncs while this machine is on the same network as that server, so a
    laptop away from home waits and syncs on the way back in.
    """
    config = Config()
    server = _require_server(config, server)
    if state is None:
        a = config.get_automation(server)
        click.echo(f"Live sync ({server}): {'on' if a['live_sync'] else 'off'}")
        return
    config.set_automation(live_sync=(state == "on"), server=server)
    click.echo(f"✅ Live sync ({server}): {state}")
    _apply_service(config)


@main.command(name="scheduled-sync")
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@click.option("--at", default=None, metavar="HH:MM",
              help="Time of day to sync (default 02:00)")
@click.option("--server", "server", default=None, metavar="NAME",
              help="Which server to set this for (default: the active one)")
def scheduled_sync(state, at, server):
    """Turn the daily scheduled sync on or off, and set its time

    Per server since 0.18.0, times included — a home NAS at 02:00 and a work
    one at 18:00 are two schedules, not one.

    A run missed because the machine was asleep or away from home isn't lost —
    it happens at the first opportunity that day.
    """
    config = Config()
    server = _require_server(config, server)
    if state is None and at is None:
        a = config.get_automation(server)
        click.echo(f"Scheduled sync ({server}): "
                   f"{'on at ' + a['scheduled_time'] if a['scheduled_sync'] else 'off'}")
        return
    try:
        if at is not None:
            config.set_automation(scheduled_time=at, server=server)
        if state is not None:
            config.set_automation(scheduled_sync=(state == "on"), server=server)
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)

    a = config.get_automation(server)
    click.echo(f"✅ Scheduled sync ({server}): "
               f"{'on at ' + a['scheduled_time'] if a['scheduled_sync'] else 'off'}")
    _apply_service(config)


@main.command()
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@click.option("--test", "send_test", is_flag=True,
              help="Send a test notification to check the desktop shows them")
def notifications(state, send_test):
    """Turn desktop notifications for automatic syncs on or off

    A background sync announces when it starts and what it moved, and says so
    when a scheduled run couldn't happen at all — which is the failure worth
    knowing about, since nobody is watching the log.
    """
    config = Config()
    if send_test:
        if not notify.available():
            click.echo("❌ No notification daemon here (looked for gdbus and "
                       "notify-send).", err=True)
            raise SystemExit(1)
        notify.send("NASync", "Notifications are working.")
        click.echo(f"📨 Sent a test notification via {notify.backend()}.")
        return

    if state is None:
        click.echo(f"Notifications: {_describe_notifications(config)}")
        return

    config.set_notifications(state == "on")
    click.echo(f"✅ Notifications: {_describe_notifications(config)}")
    # The running service re-reads the config, so there's nothing to restart.


@main.command(name="service")
@click.argument("action",
                type=click.Choice(["status", "on", "off", "install", "uninstall",
                                   "restart"]),
                required=False, default="status")
def service_cmd(action):
    """Turn background syncing on or off, or inspect it

    'on'/'off' is the same switch as Settings ▸ Run in the background;
    'install'/'uninstall' are aliases for it.
    """
    config = Config()
    try:
        if action in ("install", "on"):
            # Record the intent first, so a later settings change doesn't
            # reconcile the service straight back off again.
            config.set_automation(background=True)
            if service.backend() == service.PORTAL:
                # The portal asks the user, and the answer may take as long as
                # they take to read it — so say what is about to happen rather
                # than leaving the terminal silent behind a dialog.
                click.echo("🔐 Asking your desktop for permission to run in "
                           "the background…")
            result = service.install()
            if isinstance(result, dict):
                click.echo("✅ Permission granted.")
            else:
                click.echo(f"📄 wrote {result}")
            click.echo("✅ Background syncing enabled.")
            hint = service.linger_hint()
            if hint:
                click.echo(hint)
        elif action in ("uninstall", "off"):
            config.set_automation(background=False)
            if service.uninstall():
                click.echo("✅ Background syncing turned off.")
            else:
                click.echo("Background syncing was already off.")
        elif action == "restart":
            service.restart()
            click.echo("✅ Restarted.")
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)

    state = service.status()
    click.echo(f"Background service: {state['detail']}")
    for line in _describe_automation(config):
        click.echo(line)


@main.command()
@click.option("--interval", type=int, default=None,
              help="Also sync every N minutes (0 turns the interval off)")
@click.option("--live/--no-live", "live", default=None,
              help="Sync when watched files change")
@click.option("--schedule/--no-schedule", "schedule", default=None,
              help="Sync once a day at a set time")
@click.option("--at", default=None, metavar="HH:MM",
              help="Time for the daily scheduled sync")
@click.option("--status", "show_status", is_flag=True,
              help="Show automation settings and exit")
@click.option("--sync-at-start/--no-sync-at-start", "sync_at_start", default=None,
              help="Sync once on startup (default: only when live sync is on)")
@click.option("--server", "server", default=None, metavar="NAME",
              help="Which server the setting flags apply to (default: active)")
def auto(interval, live, schedule, at, show_status, sync_at_start, server):
    """Run automatic syncing in the foreground (live and/or scheduled)

    This is what the background service runs, and since 0.18.0 it drives
    **every** server that has a trigger on, each with its own mode, schedule
    and network check. The setting flags below still apply to one server —
    the active one, or `--server`.

    Options are remembered, so a later plain `nasync auto` reuses them. Runs
    until Ctrl+C.
    """
    config = Config()
    if any(v is not None for v in (interval, live, schedule, at)):
        target = _require_server(config, server)
        try:
            if interval is not None:
                config.set_automation(interval_minutes=interval, server=target)
            if live is not None:
                config.set_automation(live_sync=live, server=target)
            if at is not None:
                config.set_automation(scheduled_time=at, server=target)
            if schedule is not None:
                config.set_automation(scheduled_sync=schedule, server=target)
        except ValueError as e:
            click.echo(f"❌ {e}", err=True)
            raise SystemExit(1)

    if show_status:
        click.echo("Automation:")
        for line in _describe_automation(config):
            click.echo(line)
        return

    from nasync.core.automation import run_all
    try:
        run_all(reporter=click.echo, config=config, sync_at_start=sync_at_start)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


@main.command()
@click.option("--pair", default=None, help="Specific pair to watch")
def watch(pair):
    """Watch local directories and sync on changes (one-off live sync)

    The same trigger as live sync, running in the foreground until Ctrl+C,
    without changing your saved settings.
    """
    from nasync.core.automation import Automator

    config = Config()
    if pair and not config.get_pair(pair):
        click.echo(f"❌ Pair '{pair}' not found.", err=True)
        raise SystemExit(1)
    try:
        Automator(reporter=click.echo, config=config).run(
            live_sync=True, scheduled_sync=False, interval_minutes=0,
            only_pair=pair)
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise SystemExit(1)


@main.command()
def gui():
    """Launch the desktop app (GTK4 + libadwaita)"""
    # Probe before importing: `nasync.gui` defers its own `gi` import into
    # run(), so catching ImportError around the import here would miss it and
    # the user would get a traceback instead of these instructions. This is
    # also the same check install-desktop uses, so the two can't disagree.
    from nasync.system.launcher import adw_version
    from nasync.gui import ADW_MIN

    found = adw_version()
    want = ".".join(str(n) for n in ADW_MIN)
    if found is None:
        click.echo(
            "The desktop app needs PyGObject with GTK4 and libadwaita "
            f"{want} or newer.\n"
            "These are system packages, not pip installs:\n"
            "  Fedora:        sudo dnf install python3-gobject gtk4 libadwaita\n"
            "  Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-4.0 "
            "gir1.2-adw-1\n"
            "If you installed nasync in a virtualenv, create it with\n"
            "'--system-site-packages' so it can see them.",
            err=True,
        )
        raise SystemExit(1)
    if found < ADW_MIN:
        click.echo(
            f"This libadwaita is {found[0]}.{found[1]}; the desktop app needs "
            f"{want} or newer.\n"
            "Ubuntu 22.04 ships 1.1 and can't run it — 24.04 or newer can.\n"
            "Install the Flatpak, which carries its own runtime, or use the "
            "'nasync' commands, which need none of this.",
            err=True,
        )
        raise SystemExit(1)

    from nasync.gui import run

    raise SystemExit(run())


@main.command(name="install-desktop")
@click.option("--uninstall", is_flag=True, help="Remove the launcher instead of adding it")
def install_desktop(uninstall):
    """Add a desktop launcher + icon to your app menu (Linux)"""
    from nasync.system import launcher as desktop
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
            click.echo("\n✅ Installed! Look for 'NASync' in your application menu.")
            click.echo("   Clicking it opens the desktop app.")
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)


@main.command(name="server-info")
@click.option("--server", "server_name", default=None,
              help="Which saved server to inspect (default: the active one)")
def server_info(server_name):
    """Show what this server is and what was negotiated with it

    Read-only, and everything here comes from the protocol rather than from a
    vendor API — so it works the same on a Synology, a TrueNAS box or a
    Raspberry Pi running Samba.
    """
    client, server = _connected_client(server_name)
    try:
        info = client.server_info()
    except Exception as e:
        click.echo(f"❌ {e}", err=True)
        raise SystemExit(1)
    finally:
        client.release()

    conn = info["connection"]
    click.echo(f"\n🖥️  {server['name']}  ({info['ip']}:{info['port']}, "
               f"user {info['username']})")

    click.echo("\n  Connection")
    click.echo(f"    Protocol:    {conn['dialect_name']}")
    # Encryption and signing are different guarantees, and a panel that showed
    # only one of them would imply the other.
    click.echo(f"    Encrypted:   {'yes' if conn['encrypted'] else 'no'}"
               + ("" if conn["encrypted"] or not conn["supports_encryption"]
                  else "  (this server supports it — 'nasync encryption on')"))
    click.echo(f"    Signed:      {'yes' if conn['signed'] else 'no'}"
               + ("  (encryption covers signing)" if conn["encrypted"] else ""))
    click.echo(f"    Max read:    {format_bytes(conn['max_read'])}")
    click.echo(f"    Max write:   {format_bytes(conn['max_write'])}")

    click.echo("\n  Storage")
    summary, quota = format_volume(info["volume"])
    if summary:
        click.echo(f"    Space:       {summary}")
        if quota:
            click.echo(f"    Quota:       {quota}")
    else:
        click.echo(f"    (not reported by this server"
                   f"{': ' + info['volume_error'] if info.get('volume_error') else ''})")

    click.echo("\n  Shares")
    shares = info["shares"]
    if shares is None:
        click.echo(f"    (couldn't be listed: {info.get('shares_error', 'unknown')})")
    else:
        for share in shares:
            mark = "●" if share["name"] == info["share"] else " "
            note = "  (administrative)" if share["is_special"] else ""
            comment = f"  — {share['comment']}" if share["comment"] else ""
            click.echo(f"    {mark} {share['name']:<16} {share['type']:<8}"
                       f"{note}{comment}")
    click.echo("")


@main.command(name="test-connection")
def test_connection():
    """Check server connectivity, credentials, and share access"""
    config = Config()
    sc = config.get_server_config()
    if not all(sc.get(k) for k in ("ip", "username", "share")):
        click.echo("❌ Server not configured. Run 'nasync init' first.", err=True)
        raise SystemExit(1)

    client = SAMBAClient(sc["ip"], sc["username"], sc["password"],
                         sc["share"], port=sc.get("port", 445),
                         encrypt=sc.get("encrypt", False))
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
        lock = " 🔒" if s.get("encrypt") else ""
        click.echo(f"{mark}{s['name']}  ({s['ip']}:{s.get('port', 445)}/{s['share']}, user {s['username']}){lock}")


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

    if config.imported_from_host:
        # Copying someone's saved passwords into a new location shouldn't
        # happen silently, even when it's the helpful thing to do.
        click.echo("📥 Imported your existing settings (servers, folders and "
                   "saved passwords) from ~/.config/nasync. The original is "
                   "untouched.")

    problem = config.check_credentials()
    if problem:
        # Loud, and before anything else: every symptom of this shows up later
        # as the server rejecting a login, which sends people to re-check a
        # password that is in fact perfectly correct.
        click.echo(f"🔒 Saved passwords couldn't be read — {problem}")

    if not servers:
        click.echo("❌ No servers configured. Run 'nasync setup' first.")
        raise SystemExit(1)

    click.echo("\n📊 nasync status")
    click.echo("  Servers:")
    for s in servers:
        mark = "●" if s["name"] == active else "○"
        wire = "encrypted" if s.get("encrypt") else "not encrypted"
        click.echo(f"    {mark} {s['name']}  ({s['ip']}:{s.get('port', 445)}/{s['share']}, user {s['username']})")
        click.echo(f"        connection: {wire}")
        # Stated per server rather than once for the app: since 0.18.0 these
        # can differ, and "sync mode: mirror" over a list of servers would be
        # a claim about all of them.
        server_root = config.get_root(s["name"])
        click.echo(f"        sync root:  "
                   + (f"{s['share']}:/{server_root}  (nothing beside it is in range)"
                      if server_root else
                      "none — the whole share is in range of a sync"))
        server_mode = config.get_sync_mode(s["name"])
        click.echo(f"        sync mode:  {server_mode} — {_MODE_HELP[server_mode]}")

    click.echo(f"\n  Saved passwords: {config.credentials_status()}")

    click.echo("\n  Automatic syncing:")
    for line in _describe_automation(config):
        click.echo(f"  {line}")

    click.echo("\n  Sync pairs (each server has its own):")
    for s in servers:
        owned = config.get_pairs(s["name"])
        clashes = config.overlapping_pairs(s["name"])
        mark = "●" if s["name"] == active else "○"
        click.echo(f"    {mark} {s['name']} ({len(owned)})")
        for p in owned:
            click.echo(f"        • {p['name']}: {p['local']} → "
                       f"{s['share']}:/{config.resolve_remote(p['server'], s['name'])}")
            if p["name"] in clashes:
                click.echo(f"          ⚠️  shares its server folder with "
                           f"{', '.join(clashes[p['name']])} — skipped by sync")
        if not owned:
            click.echo("        ⚠️  none yet")

    click.echo("")
    for line in _describe_activity(config):
        click.echo(f"  {line}")

    from nasync.system import launcher
    broken = launcher.problem()
    if broken:
        click.echo(f"\n  ⚠️  The app launcher won't open NASync: {broken}.")
        click.echo("     Repair it with: nasync install-desktop")


if __name__ == "__main__":
    main()
