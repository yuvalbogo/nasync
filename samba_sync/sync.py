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

        try:
            client.connect()
            for pair in pairs:
                print(f"\n📤 Syncing: {pair['name']}")
                print(f"  Local:  {pair['local']}")
                print(f"  Server: {pair['server']}")
                client.sync_directory(pair["local"], pair["server"], direction="push")
                print(f"✅ {pair['name']} synced successfully")
        finally:
            client.disconnect()

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

        try:
            client.connect()
            for pair in pairs:
                print(f"\n📥 Restoring: {pair['name']}")
                print(f"  From Server: {pair['server']}")
                print(f"  To Local:    {pair['local']}")
                client.sync_directory(pair["local"], pair["server"], direction="pull")
                print(f"✅ {pair['name']} restored successfully")
        finally:
            client.disconnect()
