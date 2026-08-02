import sys
from samba_sync.config import Config
from samba_sync.samba import SAMBAClient


class Syncer:
    def __init__(self):
        self.config = Config()

    def sync_pair(self, pair_name: str = None):
        server_config = self.config.get_server_config()
        if not all(server_config.values()):
            raise Exception("Server not configured. Run 'samba-sync init' first.")

        pairs = self.config.get_pairs()
        if not pairs:
            raise Exception("No sync pairs configured. Run 'samba-sync add' first.")

        if pair_name:
            pairs = [p for p in pairs if p["name"] == pair_name]
            if not pairs:
                raise Exception(f"Pair '{pair_name}' not found.")

        client = SAMBAClient(
            server_config["ip"],
            server_config["username"],
            server_config["password"],
            server_config["share"],
        )

        synced_count = 0
        failed_count = 0

        try:
            client.connect()
            for pair in pairs:
                print(f"\n📤 Syncing: {pair['name']}")
                print(f"  Local:  {pair['local']}")
                print(f"  Server: {pair['server']}")
                try:
                    client.sync_directory(pair["local"], pair["server"], direction="push")
                    print(f"✅ {pair['name']} synced successfully")
                    synced_count += 1
                except Exception as e:
                    print(f"❌ Failed to sync {pair['name']}: {e}")
                    failed_count += 1
        finally:
            client.disconnect()

        if failed_count > 0:
            print(f"\n⚠️  Sync completed with {failed_count} error(s)")
            if synced_count == 0:
                raise SystemExit(1)

    def restore_pair(self, pair_name: str = None):
        server_config = self.config.get_server_config()
        if not all(server_config.values()):
            raise Exception("Server not configured. Run 'samba-sync init' first.")

        pairs = self.config.get_pairs()
        if not pairs:
            raise Exception("No sync pairs configured. Run 'samba-sync add' first.")

        if pair_name:
            pairs = [p for p in pairs if p["name"] == pair_name]
            if not pairs:
                raise Exception(f"Pair '{pair_name}' not found.")

        client = SAMBAClient(
            server_config["ip"],
            server_config["username"],
            server_config["password"],
            server_config["share"],
        )

        restored_count = 0
        failed_count = 0

        try:
            client.connect()
            for pair in pairs:
                print(f"\n📥 Restoring: {pair['name']}")
                print(f"  From Server: {pair['server']}")
                print(f"  To Local:    {pair['local']}")
                try:
                    client.sync_directory(pair["local"], pair["server"], direction="pull")
                    print(f"✅ {pair['name']} restored successfully")
                    restored_count += 1
                except Exception as e:
                    print(f"❌ Failed to restore {pair['name']}: {e}")
                    failed_count += 1
        finally:
            client.disconnect()

        if failed_count > 0:
            print(f"\n⚠️  Restore completed with {failed_count} error(s)")
            if restored_count == 0:
                raise SystemExit(1)
