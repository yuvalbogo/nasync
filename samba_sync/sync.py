from samba_sync.config import Config
from samba_sync.samba import SAMBAClient


class Syncer:
    def __init__(self):
        self.config = Config()

    def _prepare(self, pair_name):
        """Validate config and return (client, pairs) ready to transfer."""
        server_config = self.config.get_server_config()
        # Password may legitimately be blank (guest shares); the rest is required.
        if not all(server_config.get(k) for k in ("ip", "username", "share")):
            raise Exception("Server not configured. Run 'nasync init' first.")

        pairs = self.config.get_pairs()
        if not pairs:
            raise Exception("No sync pairs configured. Run 'nasync add' first.")

        if pair_name:
            pairs = [p for p in pairs if p["name"] == pair_name]
            if not pairs:
                raise Exception(f"Pair '{pair_name}' not found.")

        client = SAMBAClient(
            server_config["ip"],
            server_config["username"],
            server_config["password"],
            server_config["share"],
            port=server_config.get("port", 445),
        )
        return client, pairs

    def sync_pair(self, pair_name: str = None, dry_run: bool = False):
        self._run(pair_name, direction="push", dry_run=dry_run)

    def restore_pair(self, pair_name: str = None, dry_run: bool = False):
        self._run(pair_name, direction="pull", dry_run=dry_run)

    def _run(self, pair_name, direction, dry_run):
        client, pairs = self._prepare(pair_name)
        verb, icon = ("Syncing", "📤") if direction == "push" else ("Restoring", "📥")

        ok = 0
        failed = 0
        try:
            # A push dry-run only reads local disk, so don't require the server.
            if not dry_run:
                client.connect()
            for pair in pairs:
                print(f"\n{icon} {verb}: {pair['name']}")
                if direction == "push":
                    print(f"  Local:  {pair['local']}")
                    print(f"  Server: {pair['server']}")
                else:
                    print(f"  From Server: {pair['server']}")
                    print(f"  To Local:    {pair['local']}")
                try:
                    client.sync_directory(
                        pair["local"], pair["server"],
                        direction=direction, dry_run=dry_run,
                    )
                    print(f"✅ {pair['name']} done")
                    ok += 1
                except Exception as e:
                    print(f"❌ Failed on {pair['name']}: {e}")
                    failed += 1
        finally:
            client.disconnect()

        if failed:
            print(f"\n⚠️  Completed with {failed} error(s)")
            if ok == 0:
                raise SystemExit(1)
