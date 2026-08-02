import json
import os
from pathlib import Path
import appdirs


class Config:
    """Persistent config: multiple *named* servers (one active) + sync pairs.

    Passwords live in a separate ``.creds`` file (mode 0600) as a JSON map of
    server-name -> password, so they never sit in ``config.json``. The
    ``SAMBA_PASSWORD`` env var, if set, overrides the active server's password.

    Sync pairs are global — they always target whichever server is *active*.
    """

    def __init__(self):
        self.config_dir = Path(appdirs.user_config_dir("nasync"))
        self.config_file = self.config_dir / "config.json"
        self.creds_file = self.config_dir / ".creds"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        self._migrate_legacy()

    # ------------------------------------------------------------------ #
    # Load / save
    # ------------------------------------------------------------------ #
    def _load(self):
        if self.config_file.exists():
            with open(self.config_file) as f:
                self.data = json.load(f)
        else:
            self.data = {}
        self.data.setdefault("servers", [])
        self.data.setdefault("active_server", None)
        self.data.setdefault("pairs", [])

    def save(self):
        with open(self.config_file, "w") as f:
            json.dump(self.data, f, indent=2)

    def _migrate_legacy(self):
        """Convert the old single-server format to the named-servers model."""
        legacy = self.data.pop("server", None)
        if legacy and legacy.get("ip"):
            password = self._read_legacy_password()
            name = legacy.get("name") or legacy.get("share") or "default"
            self.add_or_update_server(
                name, legacy["ip"], legacy.get("username", ""), password,
                legacy.get("share", ""), legacy.get("port", 445),
            )
            self.data["active_server"] = name
            self.save()

    # ------------------------------------------------------------------ #
    # Credentials (name -> password), stored 0600
    # ------------------------------------------------------------------ #
    def _read_creds(self) -> dict:
        if not self.creds_file.exists():
            return {}
        text = self.creds_file.read_text()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}  # legacy plaintext (single password) — handled on migrate

    def _read_legacy_password(self) -> str:
        if not self.creds_file.exists():
            return ""
        text = self.creds_file.read_text()
        try:
            json.loads(text)
            return ""  # already the new JSON format
        except ValueError:
            return text.strip()  # old plaintext single password

    def _write_creds(self, creds: dict):
        with open(self.creds_file, "w") as f:
            json.dump(creds, f)
        os.chmod(self.creds_file, 0o600)

    # ------------------------------------------------------------------ #
    # Servers
    # ------------------------------------------------------------------ #
    def add_or_update_server(self, name: str, ip: str, username: str,
                             password: str, share: str, port: int = 445) -> str:
        entry = {
            "name": name,
            "ip": ip,
            "username": username,
            "share": share,
            "port": int(port),
        }
        self.data["servers"] = [s for s in self.data["servers"] if s["name"] != name]
        self.data["servers"].append(entry)

        creds = self._read_creds()
        creds[name] = password or ""
        self._write_creds(creds)

        if not self.data.get("active_server"):
            self.data["active_server"] = name
        self.save()
        return name

    # Backwards-compatible helper used by the CLI: name defaults from the share.
    def set_server(self, ip: str, username: str, password: str, share: str,
                   port: int = 445, name: str = None) -> str:
        name = name or share or ip or "default"
        return self.add_or_update_server(name, ip, username, password, share, port)

    def rename_server(self, old: str, new: str):
        server = self.get_server(old, with_password=False)
        if not server:
            return
        creds = self._read_creds()
        password = creds.get(old, "")
        self.remove_server(old)
        self.add_or_update_server(new, server["ip"], server["username"],
                                  password, server["share"], server["port"])
        self.set_active_server(new)

    def remove_server(self, name: str):
        self.data["servers"] = [s for s in self.data["servers"] if s["name"] != name]
        creds = self._read_creds()
        creds.pop(name, None)
        self._write_creds(creds)
        if self.data.get("active_server") == name:
            self.data["active_server"] = (
                self.data["servers"][0]["name"] if self.data["servers"] else None
            )
        self.save()

    def list_servers(self) -> list:
        return self.data.get("servers", [])

    def get_active_server_name(self):
        return self.data.get("active_server")

    def set_active_server(self, name: str) -> bool:
        if any(s["name"] == name for s in self.data["servers"]):
            self.data["active_server"] = name
            self.save()
            return True
        return False

    def get_server(self, name: str, with_password: bool = True):
        for s in self.data["servers"]:
            if s["name"] == name:
                result = dict(s)
                if with_password:
                    result["password"] = (
                        os.environ.get("SAMBA_PASSWORD")
                        or self._read_creds().get(name, "")
                    )
                return result
        return None

    def get_server_config(self) -> dict:
        """The *active* server's config (or empty fields if none configured)."""
        name = self.get_active_server_name()
        server = self.get_server(name) if name else None
        if not server:
            return {"ip": None, "username": None, "password": "",
                    "share": None, "port": 445}
        return {
            "ip": server.get("ip"),
            "username": server.get("username"),
            "password": server.get("password", ""),
            "share": server.get("share"),
            "port": server.get("port", 445),
        }

    # ------------------------------------------------------------------ #
    # Automation
    # ------------------------------------------------------------------ #
    def get_automation(self) -> dict:
        a = self.data.get("automation", {})
        return {
            "enabled": a.get("enabled", False),
            "on_change": a.get("on_change", True),
            "interval_minutes": int(a.get("interval_minutes", 0)),
        }

    def set_automation(self, enabled=None, on_change=None, interval_minutes=None) -> dict:
        a = self.get_automation()
        if enabled is not None:
            a["enabled"] = bool(enabled)
        if on_change is not None:
            a["on_change"] = bool(on_change)
        if interval_minutes is not None:
            a["interval_minutes"] = int(interval_minutes)
        self.data["automation"] = a
        self.save()
        return a

    # ------------------------------------------------------------------ #
    # Sync pairs (global — target the active server)
    # ------------------------------------------------------------------ #
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

    def update_pair(self, orig_name: str, local_path: str, server_path: str,
                    name: str):
        local_path = os.path.abspath(os.path.expanduser(local_path))
        if name != orig_name and self.get_pair(name):
            raise ValueError(f"A pair named '{name}' already exists.")
        for pair in self.data["pairs"]:
            if pair["name"] == orig_name:
                pair["name"] = name
                pair["local"] = local_path
                pair["server"] = server_path
                self.save()
                return name
        raise ValueError(f"No pair named '{orig_name}'.")

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
