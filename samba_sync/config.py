import json
import os
from pathlib import Path
import appdirs


class Config:
    def __init__(self):
        self.config_dir = Path(appdirs.user_config_dir("samba-sync"))
        self.config_file = self.config_dir / "config.json"
        self.creds_file = self.config_dir / ".creds"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if self.config_file.exists():
            with open(self.config_file) as f:
                self.data = json.load(f)
        else:
            self.data = {"server": {}, "pairs": []}

    def save(self):
        with open(self.config_file, "w") as f:
            json.dump(self.data, f, indent=2)

    def set_server(self, ip: str, username: str, password: str, share: str):
        self.data["server"] = {
            "ip": ip,
            "username": username,
            "share": share,
        }
        with open(self.creds_file, "w") as f:
            f.write(password)
        os.chmod(self.creds_file, 0o600)
        self.save()

    def get_server_config(self):
        server = self.data.get("server", {})
        password = ""
        if self.creds_file.exists():
            with open(self.creds_file) as f:
                password = f.read().strip()
        return {
            "ip": server.get("ip"),
            "username": server.get("username"),
            "password": password,
            "share": server.get("share"),
        }

    def add_pair(self, local_path: str, server_path: str, name: str = None):
        local_path = os.path.expanduser(local_path)
        if name is None:
            name = os.path.basename(local_path)
        self.data["pairs"].append({
            "name": name,
            "local": local_path,
            "server": server_path,
        })
        self.save()

    def remove_pair(self, name: str):
        self.data["pairs"] = [p for p in self.data["pairs"] if p["name"] != name]
        self.save()

    def get_pairs(self):
        return self.data.get("pairs", [])

    def get_pair(self, name: str):
        for pair in self.data["pairs"]:
            if pair["name"] == name:
                return pair
        return None
