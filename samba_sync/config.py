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

    def set_server(self, ip: str, username: str, password: str, share: str,
                   port: int = 445):
        self.data["server"] = {
            "ip": ip,
            "username": username,
            "share": share,
            "port": int(port),
        }
        with open(self.creds_file, "w") as f:
            f.write(password)
        os.chmod(self.creds_file, 0o600)
        self.save()

    def get_server_config(self):
        server = self.data.get("server", {})
        password = os.environ.get("SAMBA_PASSWORD", "")
        if not password and self.creds_file.exists():
            with open(self.creds_file) as f:
                password = f.read().strip()
        return {
            "ip": server.get("ip"),
            "username": server.get("username"),
            "password": password,
            "share": server.get("share"),
            "port": server.get("port", 445),
        }

    def add_pair(self, local_path: str, server_path: str, name: str = None):
        local_path = os.path.abspath(os.path.expanduser(local_path))
        if name is None:
            name = os.path.basename(local_path.rstrip("/")) or "root"
        if self.get_pair(name):
            raise ValueError(
                f"A pair named '{name}' already exists. "
                f"Use --name to choose a different name, or remove it first."
            )
        self.data["pairs"].append({
            "name": name,
            "local": local_path,
            "server": server_path,
        })
        self.save()
        return name

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
